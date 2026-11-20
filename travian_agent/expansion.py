"""Modulo Espansione: fondazione autonoma di nuovi villaggi.

Flusso (eseguito una volta per ciclo account, dopo il giro dei villaggi):

1. Individua il villaggio fondatore con ricerca dinamica: prima il
   villaggio con ruolo 'founder' (early_cropper_founder), poi
   `expansion.founder_village` se configurato, infine il primo
   villaggio con Residenza/Palazzo (gid 25/26) >= 10. I villaggi
   con ruolo hammer o feeder sono esclusi a prescindere.
2. Legge i Punti Cultura sulla pagina della residenza
   (read_culture_points): se non bastano, esce.
3. Conta i coloni pronti (sensore truppe sulle icone u10/u20/u30).
   - < 3 coloni -> emette TRAIN_TROOPS sullo slot residenza con
     priority_units coloni/settler e ATTIVA la richiesta feeder:
     durante il ciclo successivo ogni villaggio non-fondatore con
     mercato invia il surplus al fondatore (SEND_RESOURCES).
   - >= 3 coloni -> scansiona i target_slots via read_map_tile e, al
     primo tile libero con >= crop_min campi grano, emette
     FOUND_VILLAGE (navigazione mappa + click 'Fonda').
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from . import db
from .config import BASE_DIR, ExpansionConfig, ExpansionTarget, Settings
from .cropper import (
    SCAN_COOLDOWN_SECONDS,
    CropperStore,
    MapScanner,
    STATUS_APPROVED,
    STATUS_SETTLED,
    STATUS_SETTLING,
)
from .building_prereqs import GID_TOWN_HALL
from .models import (
    Action,
    ActionType,
    Field,
    GameState,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_RESIDENCE,
    VillageRole,
)

if TYPE_CHECKING:
    from .actions import ActionExecutor
    from .browser import TravianBrowser
    from .sensors import GameObserver
    from .models import Village

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"
# Config runtime modificabile da dashboard (spec: state/expansion_config.json)
EXPANSION_CONFIG_PATH = STATE_DIR / "expansion_config.json"

# Coloni richiesti per la fondazione
SETTLERS_REQUIRED = 3
# Icone unit dei coloni per tribu': u10 (romani), u20 (teutoni), u30 (galli)
SETTLER_ICONS = ("u10", "u20", "u30")
# Cap conservativo per singola spedizione mercato (mercante ~500-1000)
FEED_CAP_PER_RESOURCE = 2000
# Quota di riempimento che il feeder mantiene per se' (sopra -> spedisce)
FEEDER_KEEP_RATIO = 0.55
# Cooldown tra tentativi di fondazione falliti (evita spam di navigazione)
FOUND_RETRY_SECONDS = 900.0
# Piccole Feste (Culture Points Booster): retry breve su click fallito,
# cooldown lungo dopo l'avvio (la festa impiega ore a completarsi).
CELEBRATION_RETRY_SECONDS = 900.0
CELEBRATION_COOLDOWN_SECONDS = 6.0 * 3600.0
# Costo Piccola Festa (T4.x): controllo di pagabilita' per AUTO_CELEBRATION
# quando la riserva dei 3 coloni non e' attiva (auto_expand off / villaggi
# non-founder — la festa non deve affamare l'economia del villaggio).
SMALL_CELEBRATION_COST = {
    "wood": 6400, "clay": 6650, "iron": 5940, "crop": 1340,
}
# Retry breve quando la scansione mappa non legge tile (layout/pagina ko):
# il cooldown pieno di 3h si applica solo a scansioni andate a buon fine.
MAP_SCAN_RETRY_SECONDS = 600.0

# Snapshot Punti Cultura per la dashboard: cooldown tra letture
# (valore quasi statico — niente navigazioni extra a ogni ciclo).
CP_SNAPSHOT_SECONDS = 1800.0
CP_SNAPSHOT_RETRY = 900.0

# Livello minimo Residenza/Palazzo per addestrare i 3 coloni.
SETTLER_RESIDENCE_MIN_LEVEL = 10
# Ruoli che non possono MAI ospitare la base di fondazione: un hammer
# o un feeder non addestra coloni ne' funge da feed_target, a
# prescindere dall'ordine nell'array dei villaggi o dalla config.
_FOUNDER_BLOCKED_ROLES = frozenset({VillageRole.HAMMER, VillageRole.FEEDER})

# Store condiviso dei candidati cropper (human-in-the-loop)
_cropper_stores: dict[str, CropperStore] = {}


def cropper_store(account_id: str) -> CropperStore:
    """CropperStore condiviso per account (dashboard + agente)."""
    if account_id not in _cropper_stores:
        _cropper_stores[account_id] = CropperStore()
    return _cropper_stores[account_id]


def find_residence(interior: list[Field], min_level: int = 1) -> Field | None:
    """Residenza/Palazzo >= min_level negli edifici interni."""
    best: Field | None = None
    for f in interior or []:
        if f.gid in (GID_RESIDENCE, GID_PALACE) and f.level >= min_level:
            if best is None or f.level > best.level:
                best = f
    return best


def find_market(interior: list[Field]) -> Field | None:
    for f in interior or []:
        if f.gid == GID_MARKETPLACE and f.level >= 1:
            return f
    return None


def find_town_hall(interior: list[Field]) -> Field | None:
    """Municipio (gid 24) >= 1 negli edifici interni: serve alle feste."""
    for f in interior or []:
        if f.gid == GID_TOWN_HALL and f.level >= 1:
            return f
    return None


def save_culture_points(
    account_id: str, current: int, required: int, daily: int
) -> None:
    """Snapshot Punti Cultura dell'account (kv ``account_<id>.json``).

    Merge con il contenuto esistente per non cancellare altre chiavi;
    letto dalla dashboard per la barra di progresso PC.
    """
    try:
        safe = "".join(
            c if c.isalnum() or c in "._-" else "_" for c in account_id
        )
        key = f"account_{safe}.json"
        data: dict = {}
        raw = db.kv_read(key)
        if raw:
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
        if not isinstance(data, dict):
            data = {}
        data.update(
            {
                "culture_points_current": int(current),
                "culture_points_required": int(required),
                "culture_points_daily_prod": int(daily),
                "culture_points_ts": time.time(),
            }
        )
        db.kv_write(key, json.dumps(data, indent=2))
    except Exception:  # noqa: BLE001
        logger.debug("save_culture_points fallito", exc_info=True)


def feeder_surplus(
    state: GameState,
    keep_ratio: float = FEEDER_KEEP_RATIO,
    crop_keep_ratio: float = 0.25,
) -> dict[str, int]:
    """Surplus spedibile da un feeder: quantita' sopra keep_ratio di capienza.

    Ritorna {wood, clay, iron, crop} con solo voci positive, cappate a
    FEED_CAP_PER_RESOURCE per spedizione.
    """
    res = state.resources
    out: dict[str, int] = {}
    for name in ("wood", "clay", "iron"):
        cap = res.warehouse_capacity
        if cap <= 0:
            continue
        excess = int(getattr(res, name) - cap * keep_ratio)
        if excess > 0:
            out[name] = min(excess, FEED_CAP_PER_RESOURCE)
    cap = res.granary_capacity
    if cap > 0 and (res.crop_balance is None or res.crop_balance >= 0):
        excess = int(res.crop - cap * crop_keep_ratio)
        if excess > 0:
            out["crop"] = min(excess, FEED_CAP_PER_RESOURCE)
    return out


def count_settlers(troops: dict[str, int]) -> int:
    """Conta i coloni a casa dalle icone unit u10/u20/u30."""
    return sum(int(troops.get(k, 0) or 0) for k in SETTLER_ICONS)


async def get_available_settlers(
    observer: "GameObserver", residence_slot: int, village_id: int
) -> int:
    """Coloni pronti nel villaggio (icone u10/u20/u30 dal box truppe)."""
    troops = await observer.read_troops_home(residence_slot, village_id)
    return count_settlers(troops)


def load_expansion_overrides(account_id: str) -> dict:
    """Regole di espansione runtime da state/expansion_config.json.

    Struttura attesa (spec):
        {"<account_id>": {
            "auto_expansion_enabled": true,
            "source_village": "Hipponion",
            "expansion_slot": 26,
            "target_coordinates": {"x": -15, "y": 72}
        }}
    """
    data: dict = {}
    try:
        raw = db.kv_read("expansion_config.json")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                data = parsed
    except Exception:
        logger.debug("Lettura expansion_config da DB fallita", exc_info=True)
    if not data:
        try:
            if EXPANSION_CONFIG_PATH.exists():
                parsed = json.loads(
                    EXPANSION_CONFIG_PATH.read_text(encoding="utf-8-sig")
                )
                if isinstance(parsed, dict):
                    data = parsed
        except Exception:
            logger.debug(
                "Lettura expansion_config.json fallita", exc_info=True
            )
    entry = data.get(account_id)
    return entry if isinstance(entry, dict) else {}


def effective_expansion_config(cfg: ExpansionConfig, account_id: str) -> ExpansionConfig:
    """Merge di accounts.json con gli override di state/expansion_config.json."""
    ov = load_expansion_overrides(account_id)
    if not ov:
        return cfg
    slots = list(cfg.target_slots)
    coords = ov.get("target_coordinates")
    if isinstance(coords, dict):
        tx, ty = int(coords.get("x") or 0), int(coords.get("y") or 0)
        if (tx or ty) and (tx, ty) not in {(t.x, t.y) for t in slots}:
            # Coordinate dashboard = target primario
            slots.insert(0, ExpansionTarget(x=tx, y=ty, note="dashboard"))
    return replace(
        cfg,
        auto_expand=bool(ov.get("auto_expansion_enabled", cfg.auto_expand)),
        founder_village=str(ov.get("source_village") or cfg.founder_village),
        expansion_slot=int(ov.get("expansion_slot") or cfg.expansion_slot),
        target_slots=slots,
    )


@dataclass
class ExpansionManager:
    """Planner di fondazione per un account; stato su state/expansion_<id>.json."""

    account_id: str
    state_path: Path | None = None
    founded_tiles: list[dict] = field(default_factory=list)
    feed_target: str = ""          # nome del villaggio che deve ricevere risorse
    # New Village Bootstrap (v2.0): {"x","y","village_id","name"} del
    # tile appena fondato — finche' attivo i villaggi maturi spediscono
    # risorse cappate allo spazio libero del nuovo insediamento.
    bootstrap_target: dict = field(default_factory=dict)
    next_found_attempt: float = 0.0  # cooldown anti-spam dopo fallimenti
    next_map_scan: float = 0.0       # cooldown tra scansioni cropper di mappa
    # Culture Points Booster: ultimo snapshot PC + cooldown feste
    cp_current: int = 0
    cp_required: int = 0
    cp_daily: int = 0
    cp_slots_used: int = 0
    cp_slots_total: int = 0
    cp_next_slot: str = ""          # ETA testuale prossimo slot (nuova UI)
    next_celebration_attempt: float = 0.0
    next_cp_read: float = 0.0        # cooldown snapshot CP dashboard
    # Metriche dell'ultima scansione cropper (last_scan_at, tiles_read,
    # map_views, details_checked, cache_hits, candidates_found,
    # free_deferred, parser_suspect): esposte a Diagnostica AI.
    scan_stats: dict = field(default_factory=dict)
    # Buffer Decision Audit Log: eventi decisionali del ciclo in corso,
    # svuotato dall'orchestrator via drain_audit() (non persistito).
    _audit: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._kv_key: str | None = None
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self._kv_key = f"expansion_{safe}.json"
            self.state_path = STATE_DIR / self._kv_key
        # Firma dell'ultimo TARGET_SWITCHED loggato: evita duplicati
        # identici a ogni ciclo finche' il tile resta occupato.
        self._last_switch_sig = ""
        self._load()

    # ------------------------------------------------------------------
    # Decision Audit Log: eventi consumati dall'orchestrator
    # ------------------------------------------------------------------
    def _audit_event(
        self, decision: str, outcome: str, reason: str, **context
    ) -> None:
        self._audit.append(
            {
                "decision": decision,
                "outcome": outcome,
                "reason": reason,
                "context": {
                    k: v for k, v in context.items() if v is not None
                },
            }
        )

    def drain_audit(self) -> list[dict]:
        """Svuota il buffer degli eventi decisionali del ciclo."""
        events, self._audit = self._audit, []
        return events

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data: dict | None = None
            if self._kv_key is not None:
                text = db.kv_read(self._kv_key)
                data = json.loads(text) if text else None
            elif self.state_path and self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data:
                self.founded_tiles = list(data.get("founded_tiles") or [])
                self.feed_target = str(data.get("feed_target") or "")
                bt = data.get("bootstrap_target")
                self.bootstrap_target = dict(bt) if isinstance(bt, dict) else {}
                self.next_found_attempt = float(data.get("next_found_attempt") or 0.0)
                self.next_map_scan = float(data.get("next_map_scan") or 0.0)
                self.cp_current = int(data.get("cp_current") or 0)
                self.cp_required = int(data.get("cp_required") or 0)
                self.cp_daily = int(data.get("cp_daily") or 0)
                self.cp_slots_used = int(data.get("cp_slots_used") or 0)
                self.cp_slots_total = int(data.get("cp_slots_total") or 0)
                self.cp_next_slot = str(data.get("cp_next_slot") or "")
                self.next_celebration_attempt = float(
                    data.get("next_celebration_attempt") or 0.0
                )
                self.next_cp_read = float(data.get("next_cp_read") or 0.0)
                ss = data.get("scan_stats")
                self.scan_stats = dict(ss) if isinstance(ss, dict) else {}
        except Exception:
            pass

    def _save(self) -> None:
        try:
            payload = json.dumps(
                {
                    "founded_tiles": self.founded_tiles,
                    "feed_target": self.feed_target,
                    "bootstrap_target": self.bootstrap_target,
                    "next_found_attempt": self.next_found_attempt,
                    "next_map_scan": self.next_map_scan,
                    "cp_current": self.cp_current,
                    "cp_required": self.cp_required,
                    "cp_daily": self.cp_daily,
                    "cp_slots_used": self.cp_slots_used,
                    "cp_slots_total": self.cp_slots_total,
                    "cp_next_slot": self.cp_next_slot,
                    "next_celebration_attempt": self.next_celebration_attempt,
                    "next_cp_read": self.next_cp_read,
                    "scan_stats": self.scan_stats,
                },
                indent=2,
            )
            if self._kv_key is not None:
                db.kv_write(self._kv_key, payload)
                return
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(payload, encoding="utf-8")
        except Exception:
            logger.debug("Salvataggio stato espansione fallito", exc_info=True)

    # ------------------------------------------------------------------
    # Selezione founder e target
    # ------------------------------------------------------------------
    def _pick_founder(
        self,
        cfg: ExpansionConfig,
        villages: list["Village"],
        interior_map: dict[str, list[Field]],
        exclude: set[str] | None = None,
    ) -> tuple["Village | None", Field | None]:
        """Villaggio fondatore e suo edificio Residenza/Palazzo.

        Priorita' di selezione (ricerca dinamica, spec correzione
        villaggio sorgente):
          0. `expansion.founder_village` esplicito (nome o newdid) —
             scelta utente: vince su ruolo e fallback, bypassa anche
             il filtro hammer/feeder; se il villaggio non esiste
             nell'account si rinuncia (nessuna rotazione);
          1. villaggio con ruolo 'founder' (early_cropper_founder) —
             e' la base designata anche senza residenza rilevata;
          2. fallback strutturale: Residenza/Palazzo >= 10 (con slot
             di espansione liberi: il check finale resta lato gioco);
          3. qualunque residenza >= 1 / slot designato.

        Esclusione rigida: i villaggi con ruolo hammer o feeder non
        sono MAI candidati nella selezione AUTOMATICA — l'override
        esplicito dell'utente (punto 0) resta sovrano.

        `exclude` contiene nomi villaggio gia' scartati dalla
        validazione pre-training (ricalcolo della base).
        """
        cfg_name = (cfg.founder_village or "").strip()
        slot = int(cfg.expansion_slot or 0)
        excluded = {n.strip().lower() for n in (exclude or set()) if n}
        vkey = lambda v: v.name or str(v.village_id)  # noqa: E731

        def _residence_for(v: "Village") -> Field | None:
            interior = interior_map.get(vkey(v), [])
            if slot > 0:
                for f in interior:
                    if f.slot_id == slot and (
                        f.gid in (GID_RESIDENCE, GID_PALACE) or f.gid < 0
                    ):
                        return f
                # Slot designato ma non rilevato: usalo comunque (URL by-slot)
                return Field(
                    slot_id=slot, name="Residenza/Palazzo", level=0,
                    gid=GID_RESIDENCE,
                )
            return find_residence(interior)

        def _blocked(v: "Village") -> bool:
            return (
                VillageRole.parse(getattr(v, "role", None))
                in _FOUNDER_BLOCKED_ROLES
            )

        candidates = [
            v for v in villages
            if not _blocked(v)
            and (v.name or "").strip().lower() not in excluded
        ]

        # 0. Override esplicito da config: founder_village valorizzato
        #    e' la scelta dell'utente — match su TUTTI i villaggi
        #    dell'account (il ruolo hammer/feeder non lo filtra) e
        #    short-circuit totale: niente fallback ne' rotazione.
        if cfg_name:
            for v in villages:
                if (v.name or "").strip().lower() in excluded:
                    continue
                if v.name == cfg_name or str(v.village_id) == cfg_name:
                    return v, _residence_for(v)
            logger.warning(
                "Espansione: founder_village '%s' non trovato tra i "
                "villaggi dell'account — nessun fallback automatico.",
                cfg_name,
            )
            return None, None

        # 1. Ricerca per ruolo: il founder designato vince sempre, anche
        #    senza residenza rilevata (feed_target lo finanzia comunque).
        for v in candidates:
            if (
                VillageRole.parse(getattr(v, "role", None))
                is VillageRole.EARLY_CROPPER_FOUNDER
            ):
                return v, _residence_for(v)

        # 2-3. Fallback strutturale: residenza >= 10, poi >= 1,
        #      infine qualunque residenza dallo slot designato.
        for min_level in (SETTLER_RESIDENCE_MIN_LEVEL, 1):
            for v in candidates:
                res = _residence_for(v)
                if res is not None and res.level >= min_level:
                    return v, res
        if slot > 0:
            for v in candidates:
                res = _residence_for(v)
                if res is not None:
                    return v, res
        return None, None

    async def _pick_target(
        self,
        observer: "GameObserver",
        cfg: ExpansionConfig,
        prefix: str,
        store: CropperStore,
    ) -> tuple[ExpansionTarget | None, int]:
        """Primo tile libero valido + numero campi grano rilevati.

        Regola di sicurezza (spec): se le coordinate esistono nel
        candidates store, si procede SOLO con status 'approved';
        pending/rejected/settling/settled vengono saltate.
        Ritorna (target, crop_fields): crop_fields serve a salvare il
        crop_type del tile fondato per l'auto-config post-fondazione.
        """
        done = {(t.get("x"), t.get("y")) for t in self.founded_tiles}
        # I candidati inseriti a mano dalla dashboard esulano dal filtro
        # crop_min: l'utente ha gia' verificato il tile fuori banda.
        manual_ok = {
            (c.x, c.y)
            for c in store.approved_targets(self.account_id)
            if getattr(c, "manual", False)
        }
        # Coda ordinata dei candidati (spec Corsa al 15c): prima il
        # target principale e le righe extra configurate (x,y != 0|0),
        # poi i risultati del Cropper Finder approvati dalla dashboard.
        approved = store.approved_targets(self.account_id)
        ordered: list[ExpansionTarget] = [
            t for t in cfg.target_slots if (t.x, t.y) != (0, 0)
        ]
        ordered += [
            ExpansionTarget(
                x=c.x, y=c.y,
                note=f"{c.type} +{c.bonus_crop_percent - 100}% oasi "
                     f"({c.distance_hours}h da {c.nearest_village or '?'}) [approved]",
            )
            for c in approved
            if (c.x, c.y) not in {(t.x, t.y) for t in ordered}
        ]
        occupied: list[ExpansionTarget] = []
        for target in ordered:
            if (target.x, target.y) in done:
                continue
            # Safety gate: record nello store solo se approved
            rec_status = store.status_at(self.account_id, target.x, target.y)
            if rec_status is not None and rec_status != STATUS_APPROVED:
                logger.debug(
                    "%s Espansione: target (%d, %d) bloccato (status=%s).",
                    prefix, target.x, target.y, rec_status,
                )
                continue
            info = await observer.read_map_tile(target.x, target.y)
            if info.get("occupied"):
                occupied.append(target)
                logger.info(
                    "%s Espansione: tile (%d, %d) occupato, salto. %s",
                    prefix, target.x, target.y, target.note,
                )
                continue
            crop = int(info.get("crop_fields") or 0)
            if (
                crop and crop < cfg.crop_min
                and (target.x, target.y) not in manual_ok
            ):
                logger.info(
                    "%s Espansione: tile (%d, %d) solo %dc, richiesti >= %dc.",
                    prefix, target.x, target.y, crop, cfg.crop_min,
                )
                continue
            if not (info.get("free") or info.get("settle_link") or crop):
                # Tile non riconosciuto come libero: salto per sicurezza
                logger.debug(
                    "%s Espansione: tile (%d, %d) non verificato come libero.",
                    prefix, target.x, target.y,
                )
                continue
            if occupied:
                # Ripiego riuscito: il target principale era occupato —
                # evento TARGET_SWITCHED nel Decision Audit Log.
                sig = f"{occupied[0].x},{occupied[0].y}->{target.x},{target.y}"
                if sig != self._last_switch_sig:
                    self._last_switch_sig = sig
                    self._audit_event(
                        "TARGET_SWITCHED",
                        "EXECUTED",
                        f"Tile principale ({occupied[0].x}|{occupied[0].y}) "
                        f"occupato: ripiego su candidato "
                        f"({target.x}|{target.y})",
                        skipped=[{"x": t.x, "y": t.y} for t in occupied],
                        new_target={"x": target.x, "y": target.y},
                    )
            return target, crop
        if occupied:
            sig = "none:" + ",".join(f"{t.x}|{t.y}" for t in occupied)
            if sig != self._last_switch_sig:
                self._last_switch_sig = sig
                self._audit_event(
                    "TARGET_SWITCHED",
                    "SKIPPED",
                    f"Tutti i {len(occupied)} candidati espansione "
                    "verificati sono occupati: nessun ripiego disponibile",
                    skipped=[{"x": t.x, "y": t.y} for t in occupied],
                )
        return None, 0

    async def _try_celebration(
        self,
        *,
        executor: "ActionExecutor",
        village: "Village | None" = None,
        founder: "Village | None" = None,
        interior_map: dict[str, list[Field]],
        states_by_village: dict[str, GameState],
        prefix: str,
        now: float,
        require_settler_reserve: bool = True,
    ) -> bool:
        """Avvia una Piccola Festa nel Municipio del villaggio (spec 2).

        Condizioni: Municipio >= 1, cooldown scaduto e risorse
        sufficienti. ``require_settler_reserve=True`` (percorso founder
        del ciclo espansione) esige la riserva dei 3 coloni; ``False``
        (AUTO_CELEBRATION, expansion.auto_celebrate) richiede solo il
        costo della Piccola Festa. ``founder`` resta come alias legacy
        del parametro ``village``.
        """
        target = village if village is not None else founder
        if target is None or now < self.next_celebration_attempt:
            return False
        vname = target.name or str(target.village_id)
        town_hall = find_town_hall(
            interior_map.get(target.name or "", [])
            or interior_map.get(vname, [])
        )
        if town_hall is None:
            return False
        fstate = states_by_village.get(target.name or "") or (
            states_by_village.get(vname)
        )
        if fstate is None:
            return False
        res = fstate.resources
        available = {
            "wood": res.wood, "clay": res.clay,
            "iron": res.iron, "crop": res.crop,
        }
        if require_settler_reserve:
            try:
                from .replenishment import settler_reserve  # no cicli
            except Exception:  # noqa: BLE001
                settler_reserve = None
            if settler_reserve is not None:
                reserve = settler_reserve()
                if any(
                    int(available.get(k, 0)) < int(reserve.get(k, 0))
                    for k in ("wood", "clay", "iron", "crop")
                ):
                    return False
        else:
            # AUTO_CELEBRATION: basta coprire il costo della festa
            # (la pagabilita' reale e' comunque riverificata nel click
            # del Municipio — link assente -> retry tra 15 min).
            if any(
                int(available.get(k, 0)) < cost
                for k, cost in SMALL_CELEBRATION_COST.items()
            ):
                return False
        action = Action(
            type=ActionType.START_CELEBRATION,
            params={"town_hall_slot": town_hall.slot_id, "kind": "small"},
            reason=(
                f"CP Booster: Piccola Festa in Municipio "
                f"(slot {town_hall.slot_id}) — CP "
                f"{self.cp_current}/{self.cp_required}"
            ),
        )
        result = await executor.execute(
            action, village_id=target.village_id
        )
        self.next_celebration_attempt = now + (
            CELEBRATION_COOLDOWN_SECONDS
            if result.success
            else CELEBRATION_RETRY_SECONDS
        )
        self._save()
        logger.info(
            "%s CP Booster: festa al Municipio di '%s' -> %s",
            prefix, vname, result.message,
        )
        return result.success

    # ------------------------------------------------------------------
    # Step principale (una volta per ciclo account)
    # ------------------------------------------------------------------
    async def step(
        self,
        *,
        browser: "TravianBrowser",
        observer: "GameObserver",
        executor: "ActionExecutor",
        settings: Settings,
        villages: list["Village"],
        interior_map: dict[str, list[Field]],
        prefix: str,
        now: float | None = None,
        states_by_village: dict[str, GameState] | None = None,
    ) -> None:
        """Valuta lo stato espansione e agisce: coloni -> feed/raid -> fonda."""
        cfg = effective_expansion_config(settings.expansion, self.account_id)
        store = cropper_store(self.account_id)
        now = now if now is not None else time.time()

        # 0. Cropper Finder: scansione mappa -> candidati pending (dashboard)
        if cfg.cropper_scan and now >= self.next_map_scan:
            pending = len(store.list_candidates(self.account_id))
            approved = len(store.approved_targets(self.account_id))
            logger.info(
                "%s CropperScanner: avvio — scan_radius=%d, target_slots=%d, "
                "candidati pending=%d, approved=%d, cooldown=%dh",
                prefix, cfg.scan_radius, len(cfg.target_slots), pending,
                approved, int(SCAN_COOLDOWN_SECONDS // 3600),
            )
            tiles_seen = 0
            scanner: MapScanner | None = None
            try:
                scanner = MapScanner(store)
                found, tiles_seen = await scanner.scan(
                    observer=observer,
                    account_id=self.account_id,
                    villages=villages,
                    radius=cfg.scan_radius,
                    prefix=prefix,
                )
                if found:
                    logger.info(
                        "%s CropperScanner: %d nuovi candidati in attesa di approvazione.",
                        prefix, found,
                    )
                elif tiles_seen == 0:
                    logger.info(
                        "%s CropperScanner: nessun tile letto dalla mappa "
                        "(layout non riconosciuto o pagina non caricata). "
                        "Riprovo tra %d min.",
                        prefix, int(MAP_SCAN_RETRY_SECONDS // 60),
                    )
            except Exception as e_scan:
                logger.warning(
                    "%s Scansione cropper fallita: %s",
                    prefix, e_scan, exc_info=True,
                )
            # Telemetria Diagnostica AI: metriche ultima scansione
            # (disponibili anche in caso di eccezione parziale).
            if scanner is not None and scanner.last_stats:
                self.scan_stats = dict(scanner.last_stats)
            # Cooldown pieno solo se la scansione ha letto tile; su vuoto/
            # errore si riprova al ciclo successivo con un retry breve.
            self.next_map_scan = now + (
                SCAN_COOLDOWN_SECONDS if tiles_seen else MAP_SCAN_RETRY_SECONDS
            )
            self._save()

        # 0.1 Snapshot Punti Cultura per la dashboard: gira anche con
        # auto_expand disattivo — la barra 'Punti Cultura' e il CP
        # Booster si nutrono di questo valore. Con Residenza/Palazzo
        # legge la tab CP; senza ricade sulle pagine profilo/panoramica.
        if now >= self.next_cp_read:
            cp_village = cp_res = None
            for v in villages:
                res = find_residence(
                    interior_map.get(v.name or str(v.village_id), [])
                )
                if res is not None:
                    cp_village, cp_res = v, res
                    break
            if cp_res is not None and cp_village is not None:
                cp_have, cp_need, cp_daily = (
                    await observer.read_culture_points(
                        cp_res.slot_id, cp_village.village_id
                    )
                )
            else:
                overview = getattr(
                    observer, "read_culture_points_overview", None
                )
                cp_have = cp_need = cp_daily = 0
                if overview is not None:
                    cp_have, cp_need, cp_daily = await overview()
            if cp_have or cp_need or cp_daily:
                self.cp_current = cp_have
                self.cp_required = cp_need
                self.cp_daily = cp_daily
                extra = getattr(observer, "cp_extra", None) or {}
                self.cp_slots_used = int(extra.get("slots_used") or 0)
                self.cp_slots_total = int(extra.get("slots_total") or 0)
                self.cp_next_slot = str(extra.get("next_slot") or "")
                save_culture_points(
                    self.account_id, cp_have, cp_need, cp_daily
                )
                self.next_cp_read = now + CP_SNAPSHOT_SECONDS
            else:
                self.next_cp_read = now + CP_SNAPSHOT_RETRY
            self._save()

        # 0.2 AUTO_CELEBRATION (expansion.auto_celebrate, Strategy
        # Compiler): CP in deficit -> Piccola Festa nel Municipio di
        # ogni villaggio che lo possiede e puo' pagarla. Indipendente da
        # auto_expand: usa lo snapshot CP del blocco 0.1 e il cooldown
        # condiviso next_celebration_attempt (una festa per ciclo).
        if (
            cfg.auto_celebrate
            and self.cp_required > 0
            and self.cp_current < self.cp_required
            and now >= self.next_celebration_attempt
        ):
            for v in villages:
                vkey = v.name or str(v.village_id)
                if find_town_hall(interior_map.get(vkey, [])) is None:
                    continue
                done = await self._try_celebration(
                    executor=executor,
                    village=v,
                    interior_map=interior_map,
                    states_by_village=states_by_village or {},
                    prefix=prefix,
                    now=now,
                    require_settler_reserve=False,
                )
                if done or now < self.next_celebration_attempt:
                    break

        if not cfg.auto_expand:
            return
        has_approved = bool(store.approved_targets(self.account_id))
        if not cfg.target_slots and not has_approved:
            logger.debug("%s Espansione attiva ma nessun target configurato/approvato.", prefix)
            return
        if now < self.next_found_attempt:
            return

        founder, residence = self._pick_founder(cfg, villages, interior_map)
        if founder is None:
            logger.info(
                "%s Espansione: nessun villaggio eleggibile come fondatore.",
                prefix,
            )
            return
        if residence is None:
            # Founder designato (ruolo) senza Residenza/Palazzo rilevata:
            # i feeder lo finanziano comunque — deve COSTRUIRE la residenza.
            self.feed_target = founder.name
            self._save()
            logger.info(
                "%s Espansione: founder '%s' senza Residenza/Palazzo "
                "rilevata; feeder attivi per la costruzione.",
                prefix, founder.name,
            )
            return

        # 1. Punti cultura (spec CP Booster: snapshot persistito per
        #    il booster del builder e la barra di progresso dashboard).
        #    Se il blocco 0.1 ha gia' letto un valore fresco lo
        #    riutilizza invece di navigare di nuovo sulla Residenza.
        if now < self.next_cp_read and (
            self.cp_current or self.cp_required or self.cp_daily
        ):
            cp_have = self.cp_current
            cp_need = self.cp_required
            cp_daily = self.cp_daily
        else:
            cp_have, cp_need, cp_daily = await observer.read_culture_points(
                residence.slot_id, founder.village_id
            )
        if cp_have or cp_need or cp_daily:
            self.cp_current, self.cp_required, self.cp_daily = (
                cp_have, cp_need, cp_daily
            )
            extra = getattr(observer, "cp_extra", None) or {}
            self.cp_slots_used = int(extra.get("slots_used") or 0)
            self.cp_slots_total = int(extra.get("slots_total") or 0)
            self.cp_next_slot = str(extra.get("next_slot") or "")
            save_culture_points(self.account_id, cp_have, cp_need, cp_daily)
            self.next_cp_read = now + CP_SNAPSHOT_SECONDS
            self._save()
        if cp_need > 0 and cp_have < cp_need + cfg.min_culture_margin:
            logger.info(
                "%s Espansione: CP insufficienti a '%s' (%d/%d). Attendo.",
                prefix, founder.name, cp_have, cp_need,
            )
            self.feed_target = ""
            # Piccola Festa automatica: Municipio presente, nessuna festa
            # recente e risorse sopra la riserva dei 3 coloni.
            await self._try_celebration(
                executor=executor,
                founder=founder,
                interior_map=interior_map,
                states_by_village=states_by_village or {},
                prefix=prefix,
                now=now,
            )
            return

        # Validazione pre-training (spec: correzione villaggio sorgente):
        # i coloni si addestrano solo da Residenza/Palazzo >= 10. Se il
        # livello letto e' inferiore, si forza il ricalcolo della base
        # escludendo il founder corrente (level 0 = non rilevato: ok).
        if 0 < residence.level < SETTLER_RESIDENCE_MIN_LEVEL:
            logger.error(
                "%s Espansione: '%s' ha Residenza/Palazzo livello %d (<%d): "
                "coloni non addestrabili.%s",
                prefix, founder.name, residence.level,
                SETTLER_RESIDENCE_MIN_LEVEL,
                (
                    " founder_village fissato da config: nessuna rotazione."
                    if (cfg.founder_village or "").strip()
                    else " Ricalcolo base di fondazione..."
                ),
            )
            alt_founder = alt_residence = None
            if not (cfg.founder_village or "").strip():
                alt_founder, alt_residence = self._pick_founder(
                    cfg, villages, interior_map,
                    exclude={founder.name or str(founder.village_id)},
                )
            if (
                alt_founder is not None
                and alt_residence is not None
                and not 0 < alt_residence.level < SETTLER_RESIDENCE_MIN_LEVEL
            ):
                logger.info(
                    "%s Espansione: base di fondazione ricalcolata -> '%s'.",
                    prefix, alt_founder.name,
                )
                founder, residence = alt_founder, alt_residence
            else:
                # Nessun sostituto idoneo: feed_target resta sul founder
                # designato — i feeder finanziano l'upgrade a Residenza 10,
                # ma niente tentativo di addestramento su livello basso.
                self.feed_target = founder.name
                self._save()
                logger.warning(
                    "%s Espansione: nessun villaggio alternativo con "
                    "Residenza/Palazzo >= %d; feed_target su '%s' per "
                    "finanziare l'upgrade della Residenza.",
                    prefix, SETTLER_RESIDENCE_MIN_LEVEL, founder.name,
                )
                return

        # 2. Coloni disponibili?
        settlers = await get_available_settlers(
            observer, residence.slot_id, founder.village_id
        )
        # Snapshot per la telemetria: 'Coloni: X/3' nella card founder.
        founder_state = (states_by_village or {}).get(founder.name or "")
        if founder_state is not None:
            founder_state.settlers_home = settlers
        if settlers < SETTLERS_REQUIRED:
            # Addestra coloni + attiva rete feeder verso il fondatore
            self.feed_target = founder.name
            self._save()
            train = Action(
                type=ActionType.TRAIN_SETTLERS,
                params={
                    "expansion_slot": residence.slot_id,
                    "settlers_target": SETTLERS_REQUIRED,
                    "already_trained": settlers,
                },
                reason=(
                    f"Espansione: addestramento coloni in "
                    f"'{residence.name or 'Residenza'}' ({settlers}/{SETTLERS_REQUIRED})"
                ),
            )
            result = await executor.execute(train, village_id=founder.village_id)
            logger.info(
                "%s Espansione: coloni %d/%d a '%s' -> %s",
                prefix, settlers, SETTLERS_REQUIRED, founder.name, result.message,
            )
            return

        # Deficit coloni risolto (3/3 pronti): i feeder tornano liberi.
        # Senza reset il founder continuerebbe a ricevere spedizioni
        # anche mentre si cerca solo la casella libera sulla mappa.
        if self.feed_target:
            self.feed_target = ""
            self._save()

        # 3. Scansione dei tile target (approved + configurati)
        target, tile_crop = await self._pick_target(observer, cfg, prefix, store)
        if target is None:
            logger.info(
                "%s Espansione: nessun tile libero valido tra i target configurati.",
                prefix,
            )
            self.next_found_attempt = now + FOUND_RETRY_SECONDS
            self._save()
            return

        # 4. Fondazione
        action = Action(
            type=ActionType.FOUND_VILLAGE,
            params={"x": target.x, "y": target.y},
            reason=(
                f"Espansione: fondazione nuovo villaggio a "
                f"({target.x}|{target.y}) {target.note}"
            ),
        )
        # Il candidato passa a 'settling' mentre i coloni sono in viaggio
        store.set_status_at(self.account_id, target.x, target.y, STATUS_SETTLING)
        result = await executor.execute(action, village_id=founder.village_id)
        if result.success:
            self.founded_tiles.append({
                "x": target.x, "y": target.y, "ts": now,
                "crop_type": f"{tile_crop}c" if tile_crop else "",
                "crop_fields": int(tile_crop or 0),
                "settled_at": now,
            })
            self.feed_target = ""
            # Bootstrap target (spec 2): al ciclo successivo, quando la
            # sidebar espone il nuovo newdid, l'orchestratore registra il
            # villaggio e avvia la fase is_bootstrap + feeder bridge.
            self.bootstrap_target = {
                "x": target.x, "y": target.y,
                "village_id": 0, "name": "",
            }
            store.set_status_at(self.account_id, target.x, target.y, STATUS_SETTLED)
            logger.info(
                "%s Espansione: villaggio fondato a (%d|%d)!",
                prefix, target.x, target.y,
            )
        else:
            # Fallimento: il candidato torna 'approved' per un retry futuro
            if store.status_at(self.account_id, target.x, target.y) == STATUS_SETTLING:
                store.set_status_at(self.account_id, target.x, target.y, STATUS_APPROVED)
            self.next_found_attempt = now + FOUND_RETRY_SECONDS
            logger.warning(
                "%s Espansione: fondazione a (%d|%d) fallita: %s",
                prefix, target.x, target.y, result.message,
            )
        self._save()
