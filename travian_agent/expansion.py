"""Modulo Espansione: fondazione autonoma di nuovi villaggi.

Flusso (eseguito una volta per ciclo account, dopo il giro dei villaggi):

1. Individua il villaggio fondatore: `expansion.founder_village` se
   configurato, altrimenti il primo villaggio con Residenza/Palazzo
   (gid 25/26) negli edifici interni rilevati durante il ciclo.
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
# Retry breve quando la scansione mappa non legge tile (layout/pagina ko):
# il cooldown pieno di 3h si applica solo a scansioni andate a buon fine.
MAP_SCAN_RETRY_SECONDS = 600.0

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


def feeder_surplus(state: GameState, keep_ratio: float = FEEDER_KEEP_RATIO) -> dict[str, int]:
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
    if cap > 0:
        excess = int(res.crop - cap * keep_ratio)
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
    next_celebration_attempt: float = 0.0

    def __post_init__(self) -> None:
        self._kv_key: str | None = None
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self._kv_key = f"expansion_{safe}.json"
            self.state_path = STATE_DIR / self._kv_key
        self._load()

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
                self.next_celebration_attempt = float(
                    data.get("next_celebration_attempt") or 0.0
                )
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
                    "next_celebration_attempt": self.next_celebration_attempt,
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
    ) -> tuple["Village | None", Field | None]:
        """Villaggio fondatore e suo edificio Residenza/Palazzo.

        Se `cfg.expansion_slot` e' impostato (es. 26/31/32), usa quello
        slot designato per l'addestramento coloni, verificando che sia
        un edificio di tipo Residenza/Palazzo quando rilevato.
        """
        cfg_name = (cfg.founder_village or "").strip()
        slot = int(cfg.expansion_slot or 0)
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

        if cfg_name:
            for v in villages:
                if v.name == cfg_name or str(v.village_id) == cfg_name:
                    return v, _residence_for(v)
            logger.warning(
                "Espansione: founder_village '%s' non trovato tra i villaggi.",
                cfg_name,
            )
            return None, None
        # Auto: primo villaggio con residenza/palazzo >= 10 (fallback >= 1)
        for min_level in (10, 1):
            for v in villages:
                res = _residence_for(v)
                if res is not None and res.level >= min_level:
                    return v, res
        # Ultimo fallback: qualunque residenza trovata dallo slot designato
        if slot > 0:
            for v in villages:
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
        # I cropper approved dalla dashboard hanno priorita' sui target manuali
        ordered: list[ExpansionTarget] = [
            ExpansionTarget(
                x=c.x, y=c.y,
                note=f"{c.type} +{c.bonus_crop_percent - 100}% oasi "
                     f"({c.distance_hours}h da {c.nearest_village or '?'}) [approved]",
            )
            for c in store.approved_targets(self.account_id)
        ]
        ordered += [
            t for t in cfg.target_slots
            if (t.x, t.y) not in {(c.x, c.y) for c in store.approved_targets(self.account_id)}
        ]
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
            return target, crop
        return None, 0

    async def _try_celebration(
        self,
        *,
        executor: "ActionExecutor",
        founder: "Village",
        interior_map: dict[str, list[Field]],
        states_by_village: dict[str, GameState],
        prefix: str,
        now: float,
    ) -> None:
        """Avvia una Piccola Festa nel Municipio del fondatore (spec 2).

        Condizioni: Municipio >= 1, cooldown scaduto e risorse del
        villaggio sopra la riserva dei 3 coloni (settler_reserve).
        """
        if now < self.next_celebration_attempt:
            return
        town_hall = find_town_hall(
            interior_map.get(founder.name or "", [])
        )
        if town_hall is None:
            return
        fstate = states_by_village.get(founder.name or "")
        if fstate is None:
            return
        try:
            from .replenishment import settler_reserve  # no cicli
        except Exception:  # noqa: BLE001
            settler_reserve = None
        if settler_reserve is not None:
            reserve = settler_reserve()
            res = fstate.resources
            available = {
                "wood": res.wood, "clay": res.clay,
                "iron": res.iron, "crop": res.crop,
            }
            if any(
                int(available.get(k, 0)) < int(reserve.get(k, 0))
                for k in ("wood", "clay", "iron", "crop")
            ):
                return
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
            action, village_id=founder.village_id
        )
        self.next_celebration_attempt = now + (
            CELEBRATION_COOLDOWN_SECONDS
            if result.success
            else CELEBRATION_RETRY_SECONDS
        )
        self._save()
        logger.info(
            "%s CP Booster: festa al Municipio di '%s' -> %s",
            prefix, founder.name, result.message,
        )

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
            try:
                found, tiles_seen = await MapScanner(store).scan(
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
            except Exception:
                logger.warning(
                    "%s Scansione cropper fallita", prefix, exc_info=True
                )
            # Cooldown pieno solo se la scansione ha letto tile; su vuoto/
            # errore si riprova al ciclo successivo con un retry breve.
            self.next_map_scan = now + (
                SCAN_COOLDOWN_SECONDS if tiles_seen else MAP_SCAN_RETRY_SECONDS
            )
            self._save()

        if not cfg.auto_expand:
            return
        has_approved = bool(store.approved_targets(self.account_id))
        if not cfg.target_slots and not has_approved:
            logger.debug("%s Espansione attiva ma nessun target configurato/approvato.", prefix)
            return
        if now < self.next_found_attempt:
            return

        founder, residence = self._pick_founder(cfg, villages, interior_map)
        if founder is None or residence is None:
            logger.info(
                "%s Espansione: nessun villaggio con Residenza/Palazzo trovato.",
                prefix,
            )
            return

        # 1. Punti cultura (spec CP Booster: snapshot persistito per
        #    il booster del builder e la barra di progresso dashboard)
        cp_have, cp_need, cp_daily = await observer.read_culture_points(
            residence.slot_id, founder.village_id
        )
        if cp_have or cp_need or cp_daily:
            self.cp_current, self.cp_required, self.cp_daily = (
                cp_have, cp_need, cp_daily
            )
            save_culture_points(self.account_id, cp_have, cp_need, cp_daily)
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

        # 2. Coloni disponibili?
        settlers = await get_available_settlers(
            observer, residence.slot_id, founder.village_id
        )
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
