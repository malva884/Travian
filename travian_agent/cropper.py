"""Cropper Finder & Human-in-the-Loop Expansion.

Flusso:
    [ MapScanner ] -> [ store pending ] -> [ Dashboard: approve/reject ]
        -> [ ExpansionManager fonda solo i target `approved` ]

Persistenza: ``state/cropper_candidates.json`` keyed per account_id,
secondo il formato della specifica:

    {"server_11": [{"id": "c_15_minus15_72", "x": -15, "y": 72,
                    "type": "15c", "bonus_crop_percent": 125,
                    "distance_hours": 3.4, "nearest_village": "Hipponion",
                    "oases": [{"x": -14, "y": 72, "type": "crop",
                               "bonus": 25}],
                    "status": "pending"}]}

Stati ammessi: pending, approved, rejected, settling, settled.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BASE_DIR
from . import db
from .models import Village

if TYPE_CHECKING:
    from .sensors import GameObserver

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"
CANDIDATES_PATH = STATE_DIR / "cropper_candidates.json"

# Stati ammessi dal workflow human-in-the-loop
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_SETTLING = "settling"
STATUS_SETTLED = "settled"
VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_SETTLING,
    STATUS_SETTLED,
)

# Campi grano che qualificano un tile come "cropper"
CROPPER_FIELD_COUNTS = (15, 9)
# Velocita' media coloni (campi/ora, server x1): ~5 per tutte le tribu'
SETTLER_SPEED_FIELDS_PER_HOUR = 5.0
# Le oasi influenzano la produzione entro 3 campi dal villaggio
OASIS_INFLUENCE_RADIUS = 3.0
# Intervallo minimo tra due scansioni complete della mappa (anti-bot)
SCAN_COOLDOWN_SECONDS = 3 * 3600.0
# Quanti tile liberi dettagliare (read_map_tile) per singola scansione
MAX_DETAIL_CHECKS_PER_SCAN = 24
# TTL cache tile ispezionati (map_tile_cache): i tile scartati (es.
# 4-4-4-6 o occupati) non vengono ri-controllati per 48h, cosi' ogni
# scansione avanza oltre i primi MAX_DETAIL_CHECKS_PER_SCAN tile.
TILE_CACHE_TTL_SECONDS = 48 * 3600.0
# Navigazione multi-vista: la griglia karte.php mostra ~17x17 tile;
# step 15 copre il raggio impostato senza sovrapposizioni eccessive.
MAP_VIEW_STEP = 15
# Tetto di pagine mappa caricate per ciclo (richieste HTTP extra).
MAX_MAP_VIEWS_PER_SCAN = 10


def make_candidate_id(crop_fields: int, x: int, y: int) -> str:
    """Id stabile: 'c_15_minus15_72' per 15c a (-15|72)."""

    def enc(v: int) -> str:
        return ("minus" if v < 0 else "") + str(abs(v))

    return f"c_{crop_fields}_{enc(x)}_{enc(y)}"


@dataclass
class CropperCandidate:
    """Un tile cropper (9c/15c) libero candidato alla fondazione."""

    id: str
    x: int
    y: int
    type: str                       # "15c" | "9c"
    bonus_crop_percent: int = 0     # 100 base + bonus oasi grano vicine
    distance_hours: float = 0.0
    nearest_village: str = ""
    oases: list[dict] = field(default_factory=list)
    status: str = STATUS_PENDING
    manual: bool = False            # inserito a mano dalla dashboard
    updated_ts: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "CropperCandidate | None":
        try:
            type_str = str(data.get("type") or "")
            crop_fields = int(type_str.rstrip("c") or 0)
            return cls(
                id=str(data.get("id") or make_candidate_id(
                    crop_fields, int(data.get("x", 0)), int(data.get("y", 0)),
                )),
                x=int(data.get("x", 0)),
                y=int(data.get("y", 0)),
                type=str(data.get("type") or ""),
                bonus_crop_percent=int(data.get("bonus_crop_percent") or 0),
                distance_hours=float(data.get("distance_hours") or 0.0),
                nearest_village=str(data.get("nearest_village") or ""),
                oases=[o for o in (data.get("oases") or []) if isinstance(o, dict)],
                status=data.get("status")
                if data.get("status") in VALID_STATUSES
                else STATUS_PENDING,
                manual=bool(data.get("manual")),
                updated_ts=float(data.get("updated_ts") or time.time()),
            )
        except Exception:
            return None


class CropperStore:
    """Persistenza dei candidati su state/cropper_candidates.json."""

    def __init__(self, path: Path | None = None) -> None:
        # path esplicito -> file JSON (test/override); default -> tabella DB
        self._db_mode = path is None
        self.path = path or CANDIDATES_PATH
        self._data: dict[str, list[dict]] = {}
        self._load()

    def _load(self) -> None:
        if self._db_mode:
            self._data = db.cropper_load()
            return
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, dict):
                    self._data = {
                        str(k): [c for c in v if isinstance(c, dict)]
                        for k, v in raw.items()
                        if isinstance(v, list)
                    }
        except Exception:
            logger.debug("Lettura cropper_candidates.json fallita", exc_info=True)
            self._data = {}

    def _save(self) -> None:
        if self._db_mode:
            db.cropper_save(self._data)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Salvataggio cropper_candidates.json fallito", exc_info=True)

    # --------------------------------------------------------------
    # API di lettura (GET /api/expansion/candidates equivalente)
    # --------------------------------------------------------------
    def list_candidates(self, account_id: str) -> list[CropperCandidate]:
        """Tutti i candidati dell'account, ordinati per tipo e distanza."""
        out: list[CropperCandidate] = []
        for raw in self._data.get(account_id) or []:
            c = CropperCandidate.from_dict(raw)
            if c is not None:
                out.append(c)
        out.sort(key=lambda c: (c.type != "15c", c.distance_hours))
        return out

    def find(self, account_id: str, candidate_id: str) -> CropperCandidate | None:
        for raw in self._data.get(account_id) or []:
            if raw.get("id") == candidate_id:
                return CropperCandidate.from_dict(raw)
        return None

    def status_at(self, account_id: str, x: int, y: int) -> str | None:
        """Stato del record associato alle coordinate, o None se assente."""
        for raw in self._data.get(account_id) or []:
            if int(raw.get("x", 0)) == x and int(raw.get("y", 0)) == y:
                return str(raw.get("status") or STATUS_PENDING)
        return None

    def approved_targets(self, account_id: str) -> list[CropperCandidate]:
        """Solo i candidati approved: unici target leciti per il manager."""
        return [
            c for c in self.list_candidates(account_id)
            if c.status == STATUS_APPROVED
        ]

    # --------------------------------------------------------------
    # API di scrittura (POST /api/expansion/approve equivalente)
    # --------------------------------------------------------------
    def upsert(self, account_id: str, candidate: CropperCandidate) -> None:
        rows = self._data.setdefault(account_id, [])
        for i, raw in enumerate(rows):
            if raw.get("id") == candidate.id:
                # Non sovrascrivere uno stato deciso dall'utente/coloni in viaggio
                old = str(raw.get("status") or STATUS_PENDING)
                if old in (STATUS_PENDING,):
                    raw.update(asdict(candidate))
                else:
                    keep = asdict(candidate)
                    keep["status"] = old
                    rows[i] = keep
                self._save()
                return
        rows.append(asdict(candidate))
        self._save()

    def set_status(
        self, account_id: str, candidate_id: str, status: str
    ) -> bool:
        if status not in VALID_STATUSES:
            return False
        for raw in self._data.get(account_id) or []:
            if raw.get("id") == candidate_id:
                raw["status"] = status
                raw["updated_ts"] = time.time()
                self._save()
                return True
        return False

    def set_status_at(self, account_id: str, x: int, y: int, status: str) -> bool:
        """Imposta lo stato del record alle coordinate (se esiste)."""
        if status not in VALID_STATUSES:
            return False
        for raw in self._data.get(account_id) or []:
            if int(raw.get("x", 0)) == x and int(raw.get("y", 0)) == y:
                raw["status"] = status
                raw["updated_ts"] = time.time()
                self._save()
                return True
        return False

    def approve(self, account_id: str, candidate_id: str) -> bool:
        return self.set_status(account_id, candidate_id, STATUS_APPROVED)

    def reject(self, account_id: str, candidate_id: str) -> bool:
        return self.set_status(account_id, candidate_id, STATUS_REJECTED)

    def add_manual_candidate(
        self,
        account_id: str,
        x: int,
        y: int,
        crop_type: str,
        nearest_village: str = "",
        distance_hours: float = 0.0,
    ) -> CropperCandidate | None:
        """Inserimento manuale di un candidato dalla dashboard.

        Ritorna None se le coordinate sono gia' catalogate (qualunque
        stato): niente duplicati. Altrimenti il record entra in testa
        alla lista dell'account con status 'pending' e flag manual,
        visibile subito in cima alla watchlist della dashboard.
        """
        if self.status_at(account_id, x, y) is not None:
            return None
        try:
            crop_fields = int(str(crop_type).rstrip("c"))
        except (TypeError, ValueError):
            crop_fields = 0
        cand = CropperCandidate(
            id=make_candidate_id(crop_fields, x, y),
            x=x,
            y=y,
            type=str(crop_type),
            distance_hours=float(distance_hours or 0.0),
            nearest_village=str(nearest_village or ""),
            status=STATUS_PENDING,
            manual=True,
        )
        # insert(0): l'inserimento manuale va in cima alla watchlist
        self._data.setdefault(account_id, []).insert(0, asdict(cand))
        self._save()
        return cand


class MapScanner:
    """Scanner di mappa: trova tile cropper (9c/15c) liberi attorno ai villaggi.

    Per ogni villaggio con coordinate note naviga karte.php centrato sul
    villaggio, estrae la griglia di tile visibili e marca i tile liberi;
    i tile liberi entro `radius` vengono dettagliati via
    ``GameObserver.read_map_tile`` per rilevare la distribuzione dei campi
    (3-3-3-9 -> 9c, 1-1-1-15 -> 15c). Le oasi rilevate sulla griglia
    vengono associate al candidato entro OASIS_INFLUENCE_RADIUS campi.
    """

    def __init__(
        self,
        store: CropperStore | None = None,
        max_views: int = MAX_MAP_VIEWS_PER_SCAN,
    ) -> None:
        self.store = store or CropperStore()
        # Tetto viste mappa per scansione (anti-bot: pagine karte.php).
        self.max_views = max(1, int(max_views or 1))
        # Metriche dell'ultima scansione: persistite dall'ExpansionManager
        # (campo scan_stats del doc kv) ed esposte a /api/ask.
        self.last_stats: dict = {}

    @staticmethod
    def _view_centers(cx: int, cy: int, radius: int) -> list[tuple[int, int]]:
        """Centri delle viste mappa per coprire il raggio impostato.

        La griglia karte.php mostra ~17x17 tile: un'unica vista centrata
        sul villaggio non copre un raggio di 30-45 campi. La copertura
        avviene su una griglia di viste a passo ``MAP_VIEW_STEP``,
        ordinate per distanza dal centro (le piu' interne prima).
        """
        step = MAP_VIEW_STEP
        span = max(0, int(radius))
        n = span // step
        out = [(cx, cy)]
        for i in range(-n, n + 1):
            for j in range(-n, n + 1):
                if i == 0 and j == 0:
                    continue
                vx, vy = cx + i * step, cy + j * step
                if math.hypot(vx - cx, vy - cy) <= span:
                    out.append((vx, vy))
        out.sort(key=lambda p: math.hypot(p[0] - cx, p[1] - cy))
        return out

    async def scan(
        self,
        *,
        observer: "GameObserver",
        account_id: str,
        villages: list[Village],
        radius: int = 15,
        prefix: str = "",
        max_detail_checks: int = MAX_DETAIL_CHECKS_PER_SCAN,
    ) -> tuple[int, int]:
        """Scansiona la mappa e registra i cropper liberi come pending.

        Ritorna ``(nuovi_candidati, tile_letti)``: ``tile_letti == 0``
        indica che la griglia mappa non ha prodotto dati (layout non
        riconosciuto o pagina non caricata), non che non ci siano cropper.
        """
        centers = [
            v for v in villages
            if v.x is not None and v.y is not None
        ]
        skipped = [v.name for v in villages if v.x is None or v.y is None]
        if skipped:
            logger.info(
                "%s[Cropper Finder] %d villaggi senza coordinate "
                "(x/y None) saltati: %s",
                prefix, len(skipped), ", ".join(skipped),
            )
        if not centers:
            logger.info(
                "%s CropperScanner: nessun villaggio con coordinate, "
                "scansione saltata.",
                prefix,
            )
            return 0, 0

        centers_desc = ", ".join(
            f"{c.name}({c.x}|{c.y})" for c in centers
        )
        logger.info(
            "%s CropperScanner: avvio scansione — centri: %s, "
            "raggio: %d campi, filtri: cropper %s, raggio oasi: %.0f, "
            "max dettagli per scansione: %d, max viste mappa: %d",
            prefix, centers_desc, radius,
            "/".join(f"{n}c" for n in CROPPER_FIELD_COUNTS),
            OASIS_INFLUENCE_RADIUS, max_detail_checks, self.max_views,
        )

        # Cache tile ispezionati (map_tile_cache): attiva solo quando lo
        # store dei candidati usa il DB condiviso — i file-store dei test
        # non toccano la cache. Evita di ri-dettagliare tile scartati.
        use_cache = bool(getattr(self.store, "_db_mode", True))
        tile_cache: dict[tuple[int, int], dict] = (
            db.tile_cache_get(account_id, TILE_CACHE_TTL_SECONDS)
            if use_cache
            else {}
        )
        cache_batch: list[dict] = []

        # Rotazione delle viste: con piu' viste del tetto, ogni scansione
        # (cooldown 3h) parte da un offset diverso e copre tutto il
        # raggio nel giro di qualche ciclo.
        rot = int(time.time() // SCAN_COOLDOWN_SECONDS)

        new_found = 0
        details_done = 0
        cache_hits = 0
        views_used = 0
        free_checked = 0
        free_with_fields = 0
        fields_from_map = 0
        deferred = 0
        seen_tiles: set[tuple[int, int]] = set()   # tile dettagliati/skippati
        seen_all: set[tuple[int, int]] = set()     # dedup griglia tra viste
        oases_tiles: list[dict] = []

        for center in centers:
            cx, cy = int(center.x or 0), int(center.y or 0)
            views = self._view_centers(cx, cy, radius)
            # Il centro del villaggio e' sempre la prima vista; la
            # rotazione cicla solo le viste esterne (copertura completa
            # del raggio nel giro di poche scansioni).
            rest = [v for v in views if v != (cx, cy)]
            if len(rest) > self.max_views - 1:
                offset = rot % len(rest)
                rest = rest[offset:] + rest[:offset]
            views = [(cx, cy)] + rest[: self.max_views - 1]
            center_coords: set[tuple[int, int]] = set()
            center_free: set[tuple[int, int]] = set()
            center_oases: set[tuple[int, int]] = set()
            for vx, vy in views:
                tiles = await self._scan_grid(observer, vx, vy, radius)
                views_used += 1
                new_tiles = []
                for t in tiles:
                    try:
                        key = (int(t["x"]), int(t["y"]))
                    except (KeyError, TypeError, ValueError):
                        continue
                    if key in seen_all:
                        continue
                    seen_all.add(key)
                    center_coords.add(key)
                    new_tiles.append(t)
                oases_new = [t for t in new_tiles if t.get("is_oasis")]
                for t in oases_new:
                    center_oases.add((int(t["x"]), int(t["y"])))
                oases_tiles.extend(oases_new)
                free_new = [
                    t for t in new_tiles
                    if not t.get("is_oasis") and not t.get("occupied")
                ]
                for t in free_new:
                    center_free.add((int(t["x"]), int(t["y"])))

                # Dettaglio immediato dei tile liberi in raggio: il
                # budget di letture si spende su tile nuovi (catalogo e
                # cache gia' filtrati) fino al limite per scansione.
                for tile in free_new:
                    try:
                        tx, ty = int(tile["x"]), int(tile["y"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if (tx, ty) in seen_tiles:
                        continue
                    if math.hypot(tx - cx, ty - cy) > radius:
                        # Fuori dal raggio del centro: il tile resta
                        # candidabile solo se entra nel raggio di un
                        # altro centro in una vista successiva.
                        continue
                    if details_done >= max_detail_checks:
                        deferred += 1
                        continue
                    seen_tiles.add((tx, ty))

                    # Gia' catalogato (qualunque stato): non ri-dettagliare
                    known_status = self.store.status_at(account_id, tx, ty)
                    if known_status is not None:
                        logger.debug(
                            "%s CropperScanner: tile (%d|%d) gia' "
                            "catalogato (status=%s), salto.",
                            prefix, tx, ty, known_status,
                        )
                        continue

                    # Gia' ispezionato di recente (cache DB)
                    cached = tile_cache.get((tx, ty))
                    if cached is not None:
                        cache_hits += 1
                        logger.debug(
                            "%s CropperScanner: tile (%d|%d) in cache "
                            "(%s), salto.",
                            prefix, tx, ty,
                            "occupato" if cached.get("occupied")
                            else f"{int(cached.get('crop_fields') or 0)}c",
                        )
                        continue

                    # Su Legends la distribuzione campi puo' gia' essere
                    # nel payload mappa: niente position_details.
                    grid_crop = int(tile.get("crop_fields") or 0)
                    if grid_crop:
                        info = {"occupied": False, "crop_fields": grid_crop,
                                "text": "map-json"}
                        fields_from_map += 1
                    else:
                        logger.debug(
                            "%s CropperScanner [%s]: dettaglio tile %d "
                            "(limite %d) a (%d|%d)...",
                            prefix, center.name, details_done + 1,
                            max_detail_checks, tx, ty,
                        )
                        info = await observer.read_map_tile(tx, ty)
                        details_done += 1
                    if info.get("occupied"):
                        logger.debug(
                            "%s CropperScanner: tile (%d|%d) occupato, "
                            "scarto.",
                            prefix, tx, ty,
                        )
                        # occupied=True senza testo = lettura fallita:
                        # non si mette in cache un dato non osservato.
                        if info.get("text"):
                            cache_batch.append(
                                {"x": tx, "y": ty, "occupied": True,
                                 "crop_fields": 0}
                            )
                        continue
                    crop = int(info.get("crop_fields") or 0)
                    free_checked += 1
                    if crop:
                        free_with_fields += 1
                    cache_batch.append(
                        {"x": tx, "y": ty, "occupied": False,
                         "crop_fields": crop}
                    )
                    if crop not in CROPPER_FIELD_COUNTS:
                        logger.debug(
                            "%s CropperScanner: tile (%d|%d) ha %dc, non "
                            "e' un cropper %s, scarto.",
                            prefix, tx, ty, crop,
                            "/".join(
                                f"{n}c" for n in CROPPER_FIELD_COUNTS
                            ),
                        )
                        continue

                    candidate = self._build_candidate(
                        tx, ty, crop, centers, oases_tiles
                    )
                    self.store.upsert(account_id, candidate)
                    new_found += 1
                    logger.info(
                        "%s[Cropper Finder] Trovato %s a (%d|%d) | "
                        "Bonus Oasi: +%d%% | Stato: Pending",
                        prefix, candidate.type, tx, ty,
                        max(0, candidate.bonus_crop_percent - 100),
                    )
                    logger.debug(
                        "%s[Cropper Finder]   distanza %.1fh da '%s'.",
                        prefix, candidate.distance_hours,
                        candidate.nearest_village,
                    )
                if details_done >= max_detail_checks:
                    logger.debug(
                        "%s CropperScanner: raggiunto limite dettagli "
                        "(%d), ~%d tile liberi non verificati.",
                        prefix, max_detail_checks, deferred,
                    )
                    break  # altre viste = richieste mappa senza dettagli

            tiles_seen = len(seen_all)
            logger.info(
                "%s CropperScanner: '%s' -> %d tile letti, %d liberi, "
                "%d oasi (%d viste).",
                prefix, center.name, tiles_seen, len(center_free),
                len(center_oases), views_used,
            )
            if details_done >= max_detail_checks:
                break

        tiles_seen = len(seen_all)
        # details>0 su tile liberi ma nessun campo grano letto: quasi
        # sicuramente il parser della pagina dettagli non riconosce il
        # markup di questo server (es. layout Legends) — il warning resta
        # nel log agente e in last_stats.parser_suspect per /api/ask.
        parser_suspect = bool(free_checked >= 4 and free_with_fields == 0)
        if parser_suspect:
            logger.warning(
                "%s CropperScanner: %d tile liberi dettagliati ma "
                "crop_fields=0 su tutti — parser position_details "
                "probabilmente non allineato al markup del server.",
                prefix, free_checked,
            )
        if use_cache and cache_batch:
            db.tile_cache_put(account_id, cache_batch)
        self.last_stats = {
            "last_scan_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "tiles_read": tiles_seen,
            "map_views": views_used,
            "details_checked": details_done,
            "cache_hits": cache_hits,
            "fields_from_map": fields_from_map,
            "candidates_found": new_found,
            "free_deferred": deferred,
            "parser_suspect": parser_suspect,
        }
        logger.info(
            "%s CropperScanner: scansione completata — tile letti: %d, "
            "viste mappa: %d, dettagli effettuati: %d, hit cache: %d, "
            "nuovi candidati: %d%s.",
            prefix, tiles_seen, views_used, details_done, cache_hits,
            new_found,
            " [PARSER SOSPETTO]" if parser_suspect else "",
        )
        return new_found, tiles_seen

    async def _scan_grid(
        self, observer: "GameObserver", cx: int, cy: int, radius: int
    ) -> list[dict]:
        """Tile della griglia mappa centrata su (cx, cy).

        Riutilizza scan_map_area per l'estrazione grezza e normalizza i
        campi: {x, y, occupied, is_oasis, oasis_bonus, oasis_type}.
        """
        tiles = await observer.scan_map_area(cx, cy, radius)
        out: list[dict] = []
        for t in tiles:
            try:
                x, y = int(t["x"]), int(t["y"])
            except (KeyError, TypeError, ValueError):
                continue
            is_oasis = bool(t.get("is_oasis"))
            name = str(t.get("village_name") or "")
            bonus, oasis_type = _parse_oasis_bonus(name)
            # occupied: i tile dal JSON mappa (Legends) lo portano gia';
            # dal DOM si deduce dalla popolazione nel tooltip.
            occupied = bool(t.get("occupied")) or (
                not is_oasis and bool(t.get("population"))
            )
            out.append(
                {
                    "x": x,
                    "y": y,
                    "occupied": occupied,
                    "is_oasis": is_oasis,
                    "oasis_bonus": bonus,
                    "oasis_type": str(t.get("oasis_type") or oasis_type),
                    "crop_fields": int(t.get("crop_fields") or 0),
                }
            )
        return out

    def _build_candidate(
        self,
        x: int,
        y: int,
        crop_fields: int,
        villages: list[Village],
        oases_tiles: list[dict],
    ) -> CropperCandidate:
        """Calcola distanza, villaggio piu' vicino e oasi di influenza."""
        nearest: Village | None = None
        best = math.inf
        for v in villages:
            if v.x is None or v.y is None:
                continue
            d = math.hypot(x - v.x, y - v.y)
            if d < best:
                best = d
                nearest = v

        distance_hours = (
            round(best / SETTLER_SPEED_FIELDS_PER_HOUR, 1)
            if best is not math.inf
            else 0.0
        )

        oases: list[dict] = []
        crop_bonus = 0
        for o in oases_tiles:
            d = math.hypot(x - int(o.get("x", 0)), y - int(o.get("y", 0)))
            if d > OASIS_INFLUENCE_RADIUS:
                continue
            bonus = int(o.get("oasis_bonus") or 25)
            otype = str(o.get("oasis_type") or "crop")
            oases.append(
                {"x": int(o.get("x", 0)), "y": int(o.get("y", 0)),
                 "type": otype, "bonus": bonus}
            )
            if otype == "crop":
                crop_bonus += bonus

        return CropperCandidate(
            id=make_candidate_id(crop_fields, x, y),
            x=x,
            y=y,
            type=f"{crop_fields}c",
            bonus_crop_percent=100 + crop_bonus,
            distance_hours=distance_hours,
            nearest_village=nearest.name if nearest else "",
            oases=oases,
            status=STATUS_PENDING,
        )


def _parse_oasis_bonus(name: str) -> tuple[int, str]:
    """Estrae (bonus%, tipo) dal titolo di un tile oasi.

    Es. 'Oasi Grano +25%' -> (25, 'crop'); 'Oasi Legno +25%' -> (25, 'wood').
    """
    import re

    bonus = 25
    m = re.search(r"[+\s](\d{1,3})\s*%", name or "")
    if m:
        bonus = int(m.group(1))
    low = (name or "").lower()
    for key, words in (
        ("crop", ("grano", "grain", "wheat", "crop", "getreide", "cereal")),
        ("wood", ("legno", "wood", "holz", "lumber")),
        ("clay", ("argilla", "clay", "lehm")),
        ("iron", ("ferro", "iron", "eisen")),
    ):
        if any(w in low for w in words):
            return bonus, key
    return bonus, "crop"
