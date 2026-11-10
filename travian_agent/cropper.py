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
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BASE_DIR
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
                updated_ts=float(data.get("updated_ts") or time.time()),
            )
        except Exception:
            return None


class CropperStore:
    """Persistenza dei candidati su state/cropper_candidates.json."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or CANDIDATES_PATH
        self._data: dict[str, list[dict]] = {}
        self._load()

    def _load(self) -> None:
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


class MapScanner:
    """Scanner di mappa: trova tile cropper (9c/15c) liberi attorno ai villaggi.

    Per ogni villaggio con coordinate note naviga karte.php centrato sul
    villaggio, estrae la griglia di tile visibili e marca i tile liberi;
    i tile liberi entro `radius` vengono dettagliati via
    ``GameObserver.read_map_tile`` per rilevare la distribuzione dei campi
    (3-3-3-9 -> 9c, 1-1-1-15 -> 15c). Le oasi rilevate sulla griglia
    vengono associate al candidato entro OASIS_INFLUENCE_RADIUS campi.
    """

    def __init__(self, store: CropperStore | None = None) -> None:
        self.store = store or CropperStore()

    async def scan(
        self,
        *,
        observer: "GameObserver",
        account_id: str,
        villages: list[Village],
        radius: int = 15,
        prefix: str = "",
        max_detail_checks: int = MAX_DETAIL_CHECKS_PER_SCAN,
    ) -> int:
        """Scansiona la mappa e registra i cropper liberi come pending.

        Ritorna il numero di candidati nuovi/aggiornati.
        """
        centers = [
            v for v in villages
            if v.x is not None and v.y is not None
        ]
        if not centers:
            logger.debug("%s CropperScanner: nessun villaggio con coordinate.", prefix)
            return 0

        new_found = 0
        details_done = 0
        seen_tiles: set[tuple[int, int]] = set()
        oases_tiles: list[dict] = []

        for center in centers:
            tiles = await self._scan_grid(
                observer, center.x or 0, center.y or 0, radius
            )
            oases_tiles.extend(t for t in tiles if t.get("is_oasis"))
            free_tiles = [
                t for t in tiles
                if not t.get("is_oasis") and not t.get("occupied")
            ]

            for tile in free_tiles:
                if details_done >= max_detail_checks:
                    break
                tx, ty = int(tile["x"]), int(tile["y"])
                if (tx, ty) in seen_tiles:
                    continue
                seen_tiles.add((tx, ty))
                # Gia' catalogato (qualunque stato): non ri-dettagliare
                if self.store.status_at(account_id, tx, ty) is not None:
                    continue

                info = await observer.read_map_tile(tx, ty)
                details_done += 1
                if info.get("occupied"):
                    continue
                crop = int(info.get("crop_fields") or 0)
                if crop not in CROPPER_FIELD_COUNTS:
                    continue

                candidate = self._build_candidate(
                    tx, ty, crop, centers, oases_tiles
                )
                self.store.upsert(account_id, candidate)
                new_found += 1
                logger.info(
                    "%s CropperScanner: trovato %s a (%d|%d), bonus %d%%, "
                    "distanza %.1fh da '%s'.",
                    prefix, candidate.type, tx, ty,
                    candidate.bonus_crop_percent, candidate.distance_hours,
                    candidate.nearest_village,
                )

        return new_found

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
            out.append(
                {
                    "x": x,
                    "y": y,
                    "occupied": not is_oasis and bool(t.get("population")),
                    "is_oasis": is_oasis,
                    "oasis_bonus": bonus,
                    "oasis_type": oasis_type,
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
