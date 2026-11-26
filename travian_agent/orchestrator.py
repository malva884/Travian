"""Orchestratore per gestire account multipli in parallelo con asyncio ed error recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Literal

from . import db, selectors
from .actions import ActionExecutor
from .ai_scout_brain import AIScoutBrain, assess_account_power
from .brain import LLMBrain, RuleBasedBrain
from .browser import (
    ConcurrentSessionError,
    TravianBrowser,
    navigate_via_dom,
)
from .builder import (
    bootstrap_complete,
    crop_threshold,
    expansion_freeze,
    focus_intents,
    focus_skip_reasons,
    is_founder_village,
    load_build_plans,
    net_crop,
    plan_upgrades,
    queue_slots_free,
    record_upgrade_exec,
    record_upgrade_fail,
    save_build_plan,
)
from .building_prereqs import (
    FIELD_GID_TYPES,
    GID_ACADEMY,
    GID_CRANNY,
    GID_MAIN_BUILDING,
    GID_SMITHY,
    GID_TOWN_HALL,
)
from .config import (
    BASE_DIR,
    Settings,
    VillageConfig,
    load_accounts,
    save_accounts,
)
from .defense import DefenseManager, PendingReinforce
from .expansion import (
    FEEDER_CROP_KEEP_RATIO,
    FEEDER_KEEP_RATIO,
    ExpansionManager,
    cropper_store,
    effective_expansion_config,
    feeder_surplus,
    find_market,
)
from .farm_store import farm_target_store
from .farming import FarmingManager, find_rally_point
from .hero import HeroManager
from .models import (
    Action,
    ActionResult,
    ActionType,
    BuildQueueItem,
    Field,
    GameState,
    GID_BARRACKS,
    GID_GRANARY,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_RESIDENCE,
    GID_STABLE,
    GID_WAREHOUSE,
    GID_WORKSHOP,
    Resources,
    STATIC_TROOP_LABELS,
    UNIT_ACADEMY_LEVELS,
    Village,
)
from .presets import (
    detect_tribe_from_keys,
    detect_tribe_from_labels,
    resolve_role,
    resolve_troop_preset,
    resolve_troop_slot,
    trainer_gid_for_unit,
)
from .radar_store import (
    CAT_INCOMING_ATTACK,
    CAT_MERCHANT,
    CAT_OUTGOING_ATTACK,
    CAT_OUTGOING_RAID,
    CAT_REINFORCEMENT,
    CAT_TROOP_RETURN,
    radar_store,
)
from .replenishment import (
    TroopReplenishmentManager,
    normalize_unit_key,
    unit_label_to_key,
)
from .sensors import GameObserver
from .server_profile import calibrate_routing
from .stealth import (
    ModulePauseRegistry,
    eta_pause_bounds,
    human_cycle_interval,
    night_sleep_remaining,
    resource_eta_seconds,
    sample_cycle_thresholds,
)
from . import telemetry
from .war import (
    MAX_WAVES_PER_DISPATCH,
    STATUS_RECLAIMED,
    STATUS_RECLAIMING,
    STATUS_STOLEN,
    WAVE_FAILED,
    WAVE_SENT,
    WarStore,
    is_defensive_unit,
    travel_seconds,
)

logger = logging.getLogger(__name__)

HISTORY_DIR = BASE_DIR / "state" / "history"
_last_video_bonus_claim: dict[str, float] = {}
# Manager dei moduli autonomi, un'istanza per account (persistenza su disco)
_farming_managers: dict[str, FarmingManager] = {}
_expansion_managers: dict[str, ExpansionManager] = {}
_scout_brains: dict[str, AIScoutBrain] = {}
_hero_managers: dict[str, HeroManager] = {}
_defense_managers: dict[str, DefenseManager] = {}
_replenish_managers: dict[str, TroopReplenishmentManager] = {}
_war_stores: dict[str, WarStore] = {}
# Ultimi 18 campi dorf1 noti per villaggio (chiave account:newdid):
# persistono per tutta la vita del processo, cosi' state.fields resta
# completo anche nei cicli in cui l'agente visita solo dorf2.php o la
# lettura dorf1 torna vuota (fields_count=18 non blocca Coda
# Prioritaria e rule_upgrade_weakest_field).
_last_known_fields: dict[str, list[Field]] = {}
# Throttle del ciclo Fucina auto (villaggio -> ts ultimo tentativo).
_last_smithy_try: dict[str, float] = {}
SMITHY_RETRY_SECONDS = 1800.0  # un tentativo ogni ~30 min per villaggio
# Throttle del ciclo Accademia auto (villaggio -> ts ultimo tentativo).
_last_academy_try: dict[str, float] = {}
ACADEMY_RETRY_SECONDS = 900.0  # un controllo ricerche ogni ~15 min per villaggio
# Pause anti-detection: moduli in SKIPPED/BLOCKED non riprovano al
# ciclo successivo ma dopo 15-30 min (o al cambio delle risorse).
_module_pauses = ModulePauseRegistry()


def _module_pause_left(
    settings: Settings,
    village_key: str,
    module: str,
    decision: str | None = None,
    resources: Resources | None = None,
) -> float:
    """Secondi residui di pausa del modulo; 0 se eseguibile.

    ``decision=None`` = qualunque pausa del modulo (gate a livello
    di chiamata del manager). Passare ``resources`` per lo sblocco
    anticipato quando i magazzini cambiano sensibilmente.
    """
    if not getattr(settings, "stealth_pauses", True):
        return 0.0
    return _module_pauses.remaining(
        settings.account_id,
        village_key,
        module,
        decision,
        resources=resources,
    )


def _record_module_paused(
    settings: Settings,
    *,
    state: GameState | None = None,
    village: Village | None = None,
    module: str,
    decision: str,
    remaining: float,
) -> None:
    """Audit del salto per pausa anti-detection (decisione esente:
    'MODULE_PAUSED' non ri-pausa il modulo)."""
    _record_decision(
        settings,
        state=state,
        village=village,
        module=module,
        decision="MODULE_PAUSED",
        outcome=DECISION_OUTCOME_SKIPPED,
        reason=(
            f"Modulo in pausa anti-detection dopo esito di '{decision}': "
            f"riprovo tra {int(remaining)}s"
        ),
    )


# Capienza di sicurezza per i destinatari dei feeder: una spedizione
# non deve mai portare il villaggio ricevente oltre questa quota del
# magazzino/granaio (margine per la produzione e i premi task).
FEED_TARGET_FILL_MARGIN = 0.85
# Soglia "destinatario quasi pieno": sotto questo spazio libero
# complessivo il viaggio dei mercanti non vale la candela.
FEED_TARGET_MIN_FREE = 300
# Filtro grano verso croplog/15c in crescita: il grano non parte se il
# granaio del target e' gia' pieno oltre questa quota — in fase di
# crescita servono solo legno/argilla/ferro per campi e magazzini.
FEED_CROP_FULL_RATIO = 0.70
# Fallback conservativo (per risorsa) se la capienza del target e'
# ignota (villaggio mai visitato dall'agente).
FEED_UNKNOWN_FREE = 800


def _inbound_merchant_resources(
    settings: Settings, village_key: str
) -> dict[str, int]:
    """Risorse gia' in viaggio verso ``village_key``.

    Somma i carichi dei vettori mercanti 'inbound' registrati nel radar
    store per il villaggio destinatario (incluse le spedizioni
    specchiate registrate alla partenza e i sync live dal Mercato).
    """
    key = str(village_key or "").strip()
    if not key:
        return {}
    out: dict[str, int] = {}
    try:
        moves = radar_store(settings.account_id).movements_for(key)
    except Exception:  # noqa: BLE001
        return {}
    for mv in moves:
        if (
            str(mv.get("category") or "") != CAT_MERCHANT
            or str(mv.get("direction") or "") != "inbound"
        ):
            continue
        res = (mv.get("details") or {}).get("resources") or {}
        for name in ("wood", "clay", "iron", "crop"):
            try:
                out[name] = out.get(name, 0) + int(res.get(name) or 0)
            except (TypeError, ValueError):
                continue
    return out


def _cap_feed_shipment(
    settings: Settings,
    *,
    target_key: str,
    surplus: dict,
    target_res: Resources | None = None,
    target_role: str = "",
) -> tuple[dict[str, int], dict[str, int], str]:
    """Cappa una spedizione feeder allo spazio libero del destinatario.

    - Per-risorsa: mai oltre ``FEED_TARGET_FILL_MARGIN`` della capienza
      del magazzino/granaio target, al netto delle scorte attuali e dei
      mercanti gia' in viaggio verso di esso (vettori inbound radar).
    - Filtro grano su croplog/15c: il grano non parte se il granaio del
      target e' gia' al >=``FEED_CROP_FULL_RATIO`` o la sua produzione
      netta di grano e' positiva (``crop_balance`` > 0) — in crescita
      il feeder manda solo legno/argilla/ferro.

    Ritorna ``(surplus_cappato, spazio_libero_per_risorsa, nota_filtro)``.
    """
    capped: dict[str, int] = {}
    for k, v in (surplus or {}).items():
        try:
            qty = int(v or 0)
        except (TypeError, ValueError):
            qty = 0
        if qty > 0:
            capped[str(k)] = qty
    inbound = _inbound_merchant_resources(settings, target_key)
    cap_wh = int(target_res.warehouse_capacity or 0) if target_res else 0
    cap_gr = int(target_res.granary_capacity or 0) if target_res else 0
    free: dict[str, int] = {}
    for name in ("wood", "clay", "iron"):
        have = int(getattr(target_res, name) or 0) if target_res else 0
        free[name] = max(
            0,
            (
                int(cap_wh * FEED_TARGET_FILL_MARGIN)
                if cap_wh > 0
                else FEED_UNKNOWN_FREE
            )
            - have
            - inbound.get(name, 0),
        )
    have_crop = int(target_res.crop or 0) if target_res else 0
    free["crop"] = max(
        0,
        (
            int(cap_gr * FEED_TARGET_FILL_MARGIN)
            if cap_gr > 0
            else FEED_UNKNOWN_FREE
        )
        - have_crop
        - inbound.get("crop", 0),
    )
    for name in list(capped):
        capped[name] = min(capped[name], free.get(name, 0))
    # Filtro grano verso croplog/15c in crescita.
    note = ""
    if capped.get("crop", 0) > 0:
        is_crop_target = "crop" in str(target_role or "").strip().lower()
        gr_full = (
            cap_gr > 0
            and have_crop >= int(cap_gr * FEED_CROP_FULL_RATIO)
        )
        crop_net_pos = (
            target_res is not None
            and target_res.crop_balance is not None
            and target_res.crop_balance > 0
        )
        if is_crop_target and (gr_full or crop_net_pos):
            why = (
                f"granaio al {int(have_crop * 100 / max(1, cap_gr))}%"
                if gr_full
                else "produzione grano netta positiva"
            )
            note = (
                f"grano escluso dal carico verso '{target_key}' "
                f"({why}: un croplog in crescita riceve solo "
                "legno/argilla/ferro)"
            )
            del capped["crop"]
    return (
        {k: v for k, v in capped.items() if v > 0},
        free,
        note,
    )


def _next_attack_eta_seconds(settings: Settings) -> float | None:
    """Secondi al prossimo attacco in arrivo su un villaggio proprio.

    Fonte: vettori 'inbound' categoria attacco del radar store
    (sincronizzati da rally overview e box '#movements' durante il
    ciclo appena concluso). None = nessuna minaccia nota.
    """
    try:
        rs = radar_store(settings.account_id)
    except Exception:  # noqa: BLE001
        return None
    keys: set[str] = set()
    for key, cfg in (getattr(settings, "villages", {}) or {}).items():
        name = str(getattr(cfg, "name", "") or key or "").strip()
        if name:
            keys.add(name)
    now = time.time()
    eta: float | None = None
    for vkey in keys:
        try:
            moves = rs.movements_for(vkey, now=now)
        except Exception:  # noqa: BLE001
            continue
        for mv in moves:
            if str(mv.get("category") or "") != CAT_INCOMING_ATTACK:
                continue
            secs = float(mv.get("arrives_at") or 0.0) - now
            if secs > 0 and (eta is None or secs < eta):
                eta = secs
    return eta


def _snapshot_dorf1_fields(
    settings: Settings, village: Village
) -> list[Field]:
    """Ricostruisce i 18 campi dorf1 dall'ultimo snapshot su disco.

    Seed di ``_last_known_fields`` al riavvio del processo: la cache
    in-memory riparte vuota, ma ``dashboard_state_<acc>.json`` conserva
    i campi dell'ultimo ciclo valido (chiave "fields", slot 1-18) —
    stessa convenzione di ``command_server._state_from_snapshot``.
    """
    try:
        raw = json.loads(
            telemetry.telemetry_path(settings.account_id).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return []
    rows = raw.get("villages") or []
    if not isinstance(rows, list):
        return []
    vid = str(village.village_id or "")
    vname = str(village.name or "")
    target = next(
        (
            v for v in rows
            if isinstance(v, dict)
            and (
                (vid and str(v.get("id") or "") == vid)
                or (vname and str(v.get("name") or "") == vname)
            )
        ),
        None,
    )
    if target is None:
        return []
    out: list[Field] = []
    for row in target.get("fields") or []:
        if not isinstance(row, dict):
            continue
        try:
            slot_id = int(row.get("slot") or row.get("slot_id") or 0)
            gid = int(
                row.get("gid") if row.get("gid") is not None else -1
            )
            level = int(row.get("level") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= slot_id <= 18:
            continue
        out.append(
            Field(
                slot_id=slot_id,
                name=str(row.get("name") or ""),
                level=level,
                resource_type=FIELD_GID_TYPES.get(gid),
                gid=gid,
            )
        )
    return out


def _persist_village_coords(
    settings: Settings, villages: list[Village]
) -> None:
    """Scrive le coordinate rilevate dalla mappa in accounts.json.

    Ricarica gli account dal file (fonte di verita', puo' essere stata
    toccata dalla dashboard) e aggiorna solo il VillageConfig dei
    villaggi con x/y note: crea la config se assente.
    """
    try:
        accounts = load_accounts()
        acc = next(
            (a for a in accounts if a.account_id == settings.account_id),
            None,
        )
        if acc is None:
            return
        # Villaggi omonimi (es. due 'Hipponion'): la chiave nome
        # colliderebbe. Il primo omonimo usa la chiave nome (eredita la
        # config utente esistente), i successivi ricevono una voce
        # separata keyed per newdid — village_config_for preferisce l'id.
        name_counts: dict[str, int] = {}
        for v in villages:
            if v.name:
                name_counts[v.name] = name_counts.get(v.name, 0) + 1
        dup_names = {n for n, c in name_counts.items() if c > 1}
        claimed: set[str] = set()
        changed = False
        for v in villages:
            if v.x is None or v.y is None:
                continue
            key_id = str(v.village_id) if v.village_id else ""
            cfg = acc.villages.get(key_id) if key_id else None
            if cfg is None and v.name:
                dup = v.name in dup_names
                if not dup or v.name not in claimed:
                    cfg = acc.villages.get(v.name)
                    if cfg is None:
                        cfg = next(
                            (
                                c
                                for c in acc.villages.values()
                                if c.name == v.name
                            ),
                            None,
                        )
                    if cfg is None:
                        cfg = VillageConfig(name=v.name)
                        acc.villages[v.name] = cfg
                    if dup:
                        claimed.add(v.name)
            if cfg is None:
                if not key_id:
                    continue
                cfg = VillageConfig(name=v.name or key_id)
                acc.villages[key_id] = cfg
            if cfg.x != v.x or cfg.y != v.y:
                cfg.x, cfg.y = v.x, v.y
                changed = True
        if changed:
            save_accounts(accounts)
            logger.info(
                "[%s] Coordinate villaggi persistite su accounts.json.",
                settings.account_id,
            )
    except Exception:
        logger.debug("Persistenza coordinate villaggi fallita", exc_info=True)


# Corsa al 15c (spec): gid degli step critici del founder durante il
# freeze — Palazzo/Residenza (coloni), Magazzino e Granaio (riserve),
# Palazzo Pubblico (velocizza i lavori), Deposito Segreto e Mercato
# (anti-razzia: drenaggio ed evasione risorse consentiti in freeze).
_EXPANSION_CRITICAL_GIDS = frozenset({
    GID_MAIN_BUILDING,
    GID_WAREHOUSE,
    GID_GRANARY,
    GID_RESIDENCE,
    GID_PALACE,
    GID_CRANNY,
    GID_MARKETPLACE,
})


def _freeze_demote(state: GameState, action: Action) -> "Action | None":
    """Declassa a WAIT le spese non critiche durante il freeze.

    Ritorna l'azione sostitutiva oppure None se l'azione e' consentita.
    Copre: addestramento truppe e upgrade di edifici fuori dagli step
    critici (Residenza/Palazzo, Magazzino, Granaio, Palazzo Pubblico).
    """
    if action.type == ActionType.TRAIN_TROOPS:
        return Action(
            type=ActionType.WAIT,
            params=dict(action.params or {}),
            reason=(
                "Freeze espansione: addestramento truppe sospeso "
                "finche' i 3 coloni non sono pronti"
            ),
        )
    if action.type == ActionType.UPGRADE_FIELD:
        params = action.params or {}
        slot_gid = {f.slot_id: f.gid for f in state.fields}
        gids: set[int] = set()
        for _slot, _gid in dict(params.get("construct") or {}).items():
            try:
                gids.add(int(_gid))
            except (TypeError, ValueError):
                continue
        for _slot in params.get("candidates") or []:
            try:
                g = slot_gid.get(int(_slot))
            except (TypeError, ValueError):
                g = None
            if g:
                gids.add(int(g))
        if gids and not (gids & _EXPANSION_CRITICAL_GIDS):
            return Action(
                type=ActionType.WAIT,
                params=dict(params),
                reason=(
                    "Freeze espansione: solo passi critici "
                    "(Residenza/Palazzo, Magazzino, Granaio, "
                    "Palazzo Pubblico, Deposito Segreto, Mercato)"
                ),
            )
    return None


# Riscatto premi on-demand (spec 4): attacchi in arrivo entro questa
# finestra congelano i premi-risorse (restano protetti nella UI task).
_TASK_CLAIM_THREAT_WINDOW = 20 * 60


def _task_claim_ctx(
    settings: Settings,
    *,
    village: Village,
    vname: str,
    last_states: dict[str, Resources] | None,
    expansion_mgr: ExpansionManager | None,
    defense_mgr: DefenseManager | None,
) -> dict:
    """Contesto decisionale per il riscatto on-demand dei premi task.

    - ``target_costs``: obiettivi pendenti del villaggio — primo step
      del build plan persistito (next_upgrade stimato, Residenza 10
      inclusa via founder_intents) e, sul fondatore in deficit coloni
      (feed_target attivo), il costo di UN colono (max fra le tribu');
    - ``threat_incoming``: minaccia registrata con impatto entro
      ``_TASK_CLAIM_THREAT_WINDOW`` secondi;
    - ``crop_balance``: bilancio grano del ciclo precedente (gate
      premi-truppe). Stock/capienze sono lette live dalla pagina task.
    """
    now = time.time()
    costs: list[dict] = []
    try:
        plans = load_build_plans(settings.account_id)
        plan = plans.get(vname) or plans.get(village.name or "") or []
        step = dict(plan[0]) if plan else {}
        gid = int(step.get("gid") or 0)
        lvl = int(step.get("target_level") or 0)
        if gid > 0 and lvl > 0:
            from .config_engine import level_cost  # no cicli

            cost = level_cost(gid, lvl)
            if cost:
                costs.append(cost)
    except Exception:  # noqa: BLE001
        pass
    try:
        feed = str(getattr(expansion_mgr, "feed_target", "") or "")
        if feed and feed in (vname, str(village.name or "")):
            from .replenishment import settler_reserve  # no cicli

            one = {
                k: int(v // 3) for k, v in (settler_reserve() or {}).items()
            }
            if any(one.values()):
                costs.append(one)
    except Exception:  # noqa: BLE001
        pass
    threat = False
    try:
        if defense_mgr is not None:
            info = (
                (defense_mgr.threats or {}).get(str(village.village_id or 0))
                or {}
            )
            arrival = float(info.get("arrival_ts") or 0.0)
            threat = now < arrival <= now + _TASK_CLAIM_THREAT_WINDOW
    except Exception:  # noqa: BLE001
        pass
    crop_balance = None
    try:
        res = (last_states or {}).get(vname) or (last_states or {}).get(
            village.name or ""
        )
        if res is not None:
            crop_balance = res.crop_balance
    except Exception:  # noqa: BLE001
        pass
    return {
        "target_costs": costs,
        "threat_incoming": threat,
        "crop_balance": crop_balance,
    }


async def _run_manual_commands(
    settings: Settings,
    *,
    executor: ActionExecutor,
    villages: list[Village],
    interior_map: dict[str, list[Field]],
    prefix: str,
) -> None:
    """Esegue i comandi rapidi accodati dal Command Center live.

    La coda e' il file ``state/manual_commands_<acc>.json`` scritto da
    ``POST /api/command``; ogni esecuzione e' tracciata nel Decision
    Audit Log come ``MANUAL_<AZIONE>``.
    """
    try:
        commands = telemetry.drain_commands(settings.account_id)
    except Exception:
        commands = []
    if not commands:
        return
    by_name = {
        str(v.name or ""): v for v in villages if v.name
    }
    by_id = {
        str(v.village_id): v for v in villages if v.village_id
    }
    for cmd in commands:
        action_name = str(cmd.get("action") or "").upper()
        vname = str(cmd.get("village_name") or "")
        params = cmd.get("params") if isinstance(cmd.get("params"), dict) else {}
        village = by_name.get(vname) or by_id.get(vname)
        outcome = DECISION_OUTCOME_SKIPPED
        reason = ""
        try:
            if action_name == "START_CELEBRATION":
                if village is None:
                    reason = f"villaggio '{vname}' non trovato"
                else:
                    interiors = interior_map.get(village.name or "", [])
                    town_hall = next(
                        (f for f in interiors
                         if f.gid == GID_TOWN_HALL and f.level >= 1),
                        None,
                    )
                    if town_hall is None:
                        reason = "nessun Municipio (GID 24) nel villaggio"
                    else:
                        result = await executor.execute(
                            Action(
                                type=ActionType.START_CELEBRATION,
                                params={
                                    "town_hall_slot": town_hall.slot_id,
                                    "kind": str(params.get("kind") or "small"),
                                },
                                reason=(
                                    "Comando manuale Command Center: "
                                    f"festa in '{village.name}'"
                                ),
                            ),
                            village_id=village.village_id,
                        )
                        outcome = (
                            DECISION_OUTCOME_EXECUTED
                            if result.success
                            else DECISION_OUTCOME_BLOCKED
                        )
                        reason = result.message or "festa avviata"
            elif action_name == "FORCE_TICK":
                outcome = DECISION_OUTCOME_EXECUTED
                reason = "ciclo forzato dalla dashboard"
            elif action_name in ("MOVE_HERO", "TRIGGER_DODGE"):
                reason = (
                    f"comando '{action_name}' accodato ma non ancora "
                    "mappato su un'azione esecutiva"
                )
            else:
                reason = f"azione '{action_name}' sconosciuta"
        except Exception as cmd_exc:
            outcome = DECISION_OUTCOME_BLOCKED
            reason = f"esecuzione comando fallita: {cmd_exc}"
        _record_decision(
            settings,
            village=village,
            module="command_center",
            decision=f"MANUAL_{action_name}",
            outcome=outcome,
            reason=reason or "comando processato",
            context={"village_name": vname, "params": params},
        )
        logger.info(
            "%s Comando Command Center %s -> %s (%s)",
            prefix, action_name, outcome, reason,
        )


def _register_founded_villages(
    settings: Settings,
    expansion_mgr: ExpansionManager | None,
    villages: list[Village],
) -> None:
    """Auto-discovery post-fondazione (spec 2): newdid -> VillageConfig.

    Quando la sidebar espone un villaggio le cui coordinate coincidono
    con un tile 'settled' in state/expansion_<acc>.json, genera la sua
    VillageConfig in accounts.json: ruolo 'croplog' per 9c/15c, 'feeder'
    altrimenti, flag is_bootstrap attivo. Aggiorna anche bootstrap_target
    con il newdid/nome rilevato per il feeder bridge.
    """
    if expansion_mgr is None or not expansion_mgr.founded_tiles:
        return
    changed_mgr = False
    for tile in expansion_mgr.founded_tiles:
        if tile.get("registered"):
            continue
        tx, ty = tile.get("x"), tile.get("y")
        if tx is None or ty is None:
            continue
        match = next(
            (v for v in villages if v.x == tx and v.y == ty), None
        )
        if match is None or not match.village_id:
            continue
        crop_type = str(tile.get("crop_type") or "")
        # Regola 1 (biforcazione bootstrap): crop_fields del tile
        # fondato — snapshot salvato alla fondazione, poi crop_type,
        # poi il CropperStore per target scelti a mano.
        crop_n = 0
        try:
            crop_n = int(tile.get("crop_fields") or 0)
        except (TypeError, ValueError):
            crop_n = 0
        if not crop_n:
            try:
                crop_n = int(crop_type.rstrip("c") or 0)
            except (TypeError, ValueError):
                crop_n = 0
        if not crop_n:
            try:
                for _cand in cropper_store(
                    settings.account_id
                ).list_candidates(settings.account_id):
                    if _cand.x == tx and _cand.y == ty:
                        crop_n = int(
                            str(_cand.type or "").rstrip("c") or 0
                        )
                        break
            except Exception:
                crop_n = 0
        is_cropper = crop_n >= 9 or crop_type in ("9c", "15c")
        role = "croplog" if is_cropper else "feeder"
        try:
            accounts = load_accounts()
            acc = next(
                (a for a in accounts if a.account_id == settings.account_id),
                None,
            )
            if acc is None:
                continue
            key_id = str(match.village_id)
            cfg = acc.villages.get(key_id)
            if cfg is None and match.name:
                cfg = acc.villages.get(match.name)
            if cfg is None:
                cfg = acc.villages.get(key_id) or VillageConfig(
                    name=match.name or key_id
                )
                acc.villages[key_id] = cfg
            cfg.name = cfg.name or match.name or key_id
            cfg.is_bootstrap = True
            # Non sovrascrivere un ruolo scelto dall'utente: si assegna
            # solo se il villaggio e' nuovo o ancora 'standard'.
            if cfg.role in ("", "standard"):
                cfg.role = role
            # Corsa al 15c (spec): il nuovo cropper diventa la base
            # d'espansione — ruolo founder + coda prioritaria iniziale
            # (Main -> Granaio -> Magazzino -> Campi grano -> Mercato).
            if is_cropper and cfg.role in ("", "standard", "croplog"):
                cfg.role = "early_cropper_founder"
            if is_cropper and not cfg.building_queue_plan:
                cfg.building_queue_plan = [
                    {"key": "main_building", "target_level": 5, "priority": 1},
                    {"key": "granary", "target_level": 10, "priority": 2},
                    {"key": "warehouse", "target_level": 10, "priority": 3},
                    {"key": "cropland", "target_level": 5, "priority": 4},
                    {"key": "marketplace", "target_level": 5, "priority": 5},
                ]
            # Caso B (biforcazione): villaggio standard — preset edifici
            # con campi risorsa bilanciati a 5 + Mercato. Il ruolo
            # resta 'feeder' e founder_village NON viene toccato.
            if not is_cropper and not cfg.building_queue_plan:
                cfg.building_queue_plan = [
                    {"key": "woodcutter", "target_level": 5, "priority": 1},
                    {"key": "clay_pit", "target_level": 5, "priority": 2},
                    {"key": "iron_mine", "target_level": 5, "priority": 3},
                    {"key": "cropland", "target_level": 5, "priority": 4},
                    {"key": "marketplace", "target_level": 5, "priority": 5},
                ]
            if match.x is not None:
                cfg.x = match.x
            if match.y is not None:
                cfg.y = match.y
            # Rete feeder (spec): i mercanti vanno subito al nuovo
            # insediamento; il vecchio founder viene retrocesso a
            # feeder e, se il founder_village configurato puntava a
            # lui, la designazione passa al nuovo cropper.
            founder_cfg_name = str(
                getattr(acc.expansion, "founder_village", "") or ""
            ).strip()
            demoted: list[tuple[str, "VillageConfig"]] = []
            founder_cfg_hit = False
            if is_cropper:
                for _k2, _c2 in acc.villages.items():
                    if _c2 is cfg:
                        continue
                    _hit = bool(
                        founder_cfg_name
                        and (
                            (_c2.name or "") == founder_cfg_name
                            or str(_k2) == founder_cfg_name
                        )
                    )
                    if _hit:
                        founder_cfg_hit = True
                    if (
                        resolve_role(_c2.role or "")
                        == "early_cropper_founder"
                        or _hit
                    ):
                        if _c2.role != "feeder":
                            _c2.role = "feeder"
                        demoted.append((_k2, _c2))
                if not founder_cfg_name or founder_cfg_hit:
                    acc.expansion.founder_village = cfg.name
                    settings.expansion.founder_village = cfg.name
                expansion_mgr.feed_target = cfg.name
            save_accounts(accounts)
            # Riflette la registrazione anche nelle settings live: il
            # ciclo corrente usa gia' il profilo bootstrap.
            settings.villages[key_id] = VillageConfig(
                name=cfg.name, role=cfg.role, is_bootstrap=True,
                x=cfg.x, y=cfg.y,
                building_queue_plan=list(cfg.building_queue_plan or []),
            )
            for _k3, _c3 in demoted:
                _live_c = settings.villages.get(_k3) or (
                    settings.villages.get(_c3.name) if _c3.name else None
                )
                if _live_c is not None:
                    _live_c.role = "feeder"
            tile["registered"] = True
            tile["village_id"] = match.village_id
            changed_mgr = True
            bt = expansion_mgr.bootstrap_target
            if bt and bt.get("x") == tx and bt.get("y") == ty:
                bt["village_id"] = match.village_id
                bt["name"] = match.name or cfg.name
            if is_cropper:
                _record_decision(
                    settings,
                    village=match,
                    module="expansion",
                    decision="NEW_VILLAGE_BOOTSTRAP",
                    outcome=DECISION_OUTCOME_EXECUTED,
                    reason=(
                        f"Nuovo {crop_type} rilevato a ({tx}|{ty}): "
                        "rete feeder reindirizzata sul nuovo villaggio"
                    ),
                    context={
                        "x": tx, "y": ty,
                        "village": cfg.name,
                        "role": cfg.role,
                        "demoted": [c.name or k for k, c in demoted],
                        "feed_target": cfg.name,
                    },
                )
            logger.info(
                "[%s] Bootstrap: nuovo villaggio '%s' (newdid=%d, %s) "
                "registrato con ruolo '%s' e is_bootstrap=True.",
                settings.account_id, cfg.name, match.village_id,
                crop_type or "tile std", cfg.role,
            )
        except Exception:
            logger.warning(
                "[%s] Registrazione villaggio fondato fallita",
                settings.account_id, exc_info=True,
            )
    if changed_mgr:
        expansion_mgr._save()


def _complete_bootstrap(
    settings: Settings,
    expansion_mgr: ExpansionManager | None,
    village: Village,
) -> None:
    """Bootstrap terminato: is_bootstrap=False su accounts.json + state."""
    try:
        accounts = load_accounts()
        acc = next(
            (a for a in accounts if a.account_id == settings.account_id),
            None,
        )
        if acc is not None:
            key_id = str(village.village_id)
            cfg = acc.villages.get(key_id) or (
                acc.villages.get(village.name) if village.name else None
            )
            if cfg is not None:
                cfg.is_bootstrap = False
                save_accounts(accounts)
        live = settings.village_config_for(village.name, village.village_id)
        if live is not None:
            live.is_bootstrap = False
        if expansion_mgr is not None:
            bt = expansion_mgr.bootstrap_target
            if bt and (
                bt.get("village_id") == village.village_id
                or (village.x is not None and bt.get("x") == village.x
                    and bt.get("y") == village.y)
            ):
                expansion_mgr.bootstrap_target = {}
            # Biforcazione bootstrap (regola 3): concluso l'avvio del
            # villaggio, il feed_target torna sul founder designato
            # (il 15c) se era ancora puntato sul villaggio appena
            # completato.
            founder_set = str(
                settings.expansion.founder_village or ""
            ).strip()
            if expansion_mgr.feed_target and expansion_mgr.feed_target in {
                str(village.name or ""),
                str(village.village_id or ""),
            }:
                expansion_mgr.feed_target = founder_set
                logger.info(
                    "[%s] Bootstrap chiuso: feed_target ripristinato "
                    "su '%s'.",
                    settings.account_id,
                    founder_set or "(vuoto)",
                )
            expansion_mgr._save()
        logger.info(
            "[%s] Bootstrap completato per '%s': passaggio al ruolo assegnato.",
            settings.account_id, village.name or village.village_id,
        )
    except Exception:
        logger.debug(
            "[%s] Chiusura bootstrap fallita", settings.account_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Reclaim & Chiefing Engine
# ---------------------------------------------------------------------------

async def _reconcile_village_losses(
    settings: Settings,
    observer: GameObserver,
    villages: list[Village],
    war_store: WarStore,
    expansion_mgr: ExpansionManager | None,
    prefix: str,
) -> None:
    """Confronto sidebar vs storico: STOLEN_VILLAGE e re-onboarding.

    (a) Ritorno: un villaggio in sidebar le cui coordinate erano state
        marcate stolen/reclaiming e' tornato sotto controllo -> status
        'reclaimed' e re-onboarding (ruolo originario + is_bootstrap).
    (b) Perdita: le coordinate di un VillageConfig persistito non
        compaiono piu' tra i villaggi in sidebar -> record 'stolen' in
        war_targets_<acc>.json con timestamp e info nemico dal tile.
    Guardia anti-falso-positivo: se la sidebar non espone nessun
    newdid la lettura e' fallita e non si marca nulla.
    """
    real_villages = [v for v in villages if v.village_id]
    if not real_villages:
        return
    owned_coords = {
        (v.x, v.y)
        for v in real_villages
        if v.x is not None and v.y is not None
    }

    # (a) Re-onboarding dei villaggi rientrati in sidebar
    for v in real_villages:
        if v.x is None or v.y is None:
            continue
        t = war_store.target_at(v.x, v.y)
        if t and t.get("status") in (STATUS_STOLEN, STATUS_RECLAIMING):
            war_store.mark_status(v.x, v.y, STATUS_RECLAIMED)
            _onboard_reclaimed_village(settings, v, t, expansion_mgr)

    # (b) Rilevamento perdite: coordinate configurate assenti in sidebar
    seen: set[tuple[int, int]] = set()
    for cfg in settings.villages.values():
        if cfg.x is None or cfg.y is None:
            continue
        if (cfg.x, cfg.y) in owned_coords or (cfg.x, cfg.y) in seen:
            continue
        seen.add((cfg.x, cfg.y))
        if war_store.target_at(cfg.x, cfg.y) is not None:
            continue
        # Info nemico dal tile (best-effort, un solo fetch per perdita)
        enemy_info = ""
        try:
            info = await observer.read_map_tile(cfg.x, cfg.y)
            enemy_info = str((info or {}).get("text") or "")[:200]
            if not (info or {}).get("occupied"):
                # Tile libero/irriconoscibile: il villaggio e' stato
                # cancellato/abbandonato, non sottratto -> niente target.
                continue
        except Exception:
            logger.debug(
                "%s Reclaim: lettura tile (%d|%d) fallita",
                prefix, cfg.x, cfg.y,
            )
        war_store.add_stolen(
            cfg.x, cfg.y,
            name=cfg.name,
            old_role=cfg.role or "standard",
            enemy_info=enemy_info,
        )
        logger.warning(
            "%s STOLEN_VILLAGE: '%s' (%d|%d) non compare piu' in sidebar "
            "— registrato in war_targets. %s",
            prefix, cfg.name or "?", cfg.x, cfg.y,
            enemy_info[:80] if enemy_info else "",
        )


def _onboard_reclaimed_village(
    settings: Settings,
    village: Village,
    target: dict,
    expansion_mgr: ExpansionManager | None,
) -> None:
    """Post-riconquista (spec 4): VillageConfig + bootstrap d'emergenza.

    Il villaggio rientra in accounts.json con il vecchio ruolo (se la
    config era tornata 'standard'), flag is_bootstrap attivo e, se il
    feeder bridge non e' gia' impegnato, come bootstrap_target per le
    spedizioni d'emergenza dei feeder.
    """
    old_role = str(target.get("old_role") or "")
    key_id = str(village.village_id or "")
    cfg = settings.village_config_for(village.name, village.village_id)
    if cfg is None:
        cfg = VillageConfig(name=village.name or key_id)
        settings.villages[key_id or cfg.name or village.name] = cfg
    cfg.name = cfg.name or village.name or key_id
    cfg.is_bootstrap = True
    if old_role and cfg.role in ("", "standard"):
        cfg.role = old_role
    if village.x is not None:
        cfg.x = village.x
    if village.y is not None:
        cfg.y = village.y
    try:
        accounts = load_accounts()
        acc = next(
            (a for a in accounts if a.account_id == settings.account_id),
            None,
        )
        if acc is not None:
            acfg = acc.villages.get(key_id) or (
                acc.villages.get(cfg.name) if cfg.name else None
            )
            if acfg is None:
                acfg = VillageConfig(name=cfg.name or key_id)
                acc.villages[key_id or cfg.name] = acfg
            acfg.name = acfg.name or cfg.name
            acfg.is_bootstrap = True
            if old_role and acfg.role in ("", "standard"):
                acfg.role = old_role
            acfg.x, acfg.y = cfg.x, cfg.y
            save_accounts(accounts)
    except Exception:
        logger.debug(
            "[%s] Reclaim: persistenza re-onboarding fallita",
            settings.account_id, exc_info=True,
        )
    if expansion_mgr is not None and not expansion_mgr.bootstrap_target:
        expansion_mgr.bootstrap_target = {
            "x": village.x,
            "y": village.y,
            "village_id": village.village_id,
            "name": cfg.name,
        }
        expansion_mgr._save()
    logger.warning(
        "[%s] RECLAIMED: villaggio '%s' (newdid=%s) rientrato sotto "
        "controllo — is_bootstrap attivo, ruolo '%s'.",
        settings.account_id, cfg.name,
        village.village_id or "?", cfg.role,
    )


async def _dispatch_reclaim_waves(
    executor: ActionExecutor,
    war_store: WarStore,
    village: Village,
    rally_slot: int,
    prefix: str,
) -> None:
    """Spedisce le wave del treno il cui depart_ts e' scaduto.

    Ogni wave va via form Punto di Raduno (tt=2) con tipo movimento
    Attacco (c=3), come per dodge/raid custom. L'impatto sincronizzato
    e' garantito a monte dal planner (depart_ts = impact - travel);
    qui la granularita' e' quella del ciclo dei villaggi — una wave
    scaduta viene spedita alla prima visita utile dell'origine.
    """
    vid = str(village.village_id or "")
    if not vid:
        return
    now = time.time()
    due = war_store.due_waves(vid, now)
    for target, wave in due[:MAX_WAVES_PER_DISPATCH]:
        action = Action(
            type=ActionType.SEND_CUSTOM_RAID,
            params={
                "rally_slot": rally_slot,
                "move_type": str(wave.get("move_type") or "3"),
                "targets": [
                    {
                        "x": int(target["x"]),
                        "y": int(target["y"]),
                        "troops": dict(wave.get("troops") or {}),
                        "include_hero": bool(wave.get("include_hero")),
                    }
                ],
            },
            reason=(
                f"Reclaim: wave {wave.get('kind', '?')} "
                f"#{wave.get('wave_id', '?')} verso "
                f"({target['x']}|{target['y']})"
            ),
        )
        res = await executor.execute(action, village_id=village.village_id)
        wave["status"] = WAVE_SENT if res.success else WAVE_FAILED
        wave["sent_ts"] = time.time()
        target["updated_ts"] = time.time()
        war_store._save()
        logger.info(
            "%s Reclaim wave %d (%s) %s -> (%s|%s): %s",
            prefix,
            int(wave.get("wave_id") or 0),
            str(wave.get("kind") or "?"),
            "inviata" if res.success else "FALLITA",
            target["x"], target["y"], res.message,
        )


async def _coordinate_inter_village_defense(
    *,
    executor: ActionExecutor,
    observer: GameObserver,
    settings: Settings,
    village: Village,
    state: GameState,
    villages: list[Village],
    last_states: dict[str, Resources] | None,
    defense_mgr: DefenseManager,
    replenish_mgr: TroopReplenishmentManager | None,
    rally_slot: int,
    market_slot: int,
    prefix: str,
    now: float,
) -> None:
    """Smart Reinforce Coordinator (spec: motore di selezione donatori).

    Eseguito nel contesto del villaggio corrente: se il villaggio e'
    sicuro, consulta il registro minacce di DefenseManager e manda un
    muro di rinforzo (move_type=2, solo unita' difensive) verso i
    villaggi alleati sotto attacco reale, purché la marcia arrivi
    prima dell'impatto meno ``reinforce_margin_seconds``.
    """
    cfg = settings.defense
    threats = defense_mgr.active_threats(now)
    my_id = int(village.village_id or state.village_id or 0)
    targets = {
        vid: t for vid, t in threats.items()
        if str(vid).isdigit() and int(vid) != my_id
    }
    if not targets:
        return

    # Sicurezza del donatore: niente minacce reali su di se', niente
    # lock d'emergenza, niente congelamento da assedio.
    if str(my_id) in threats or int(state.incoming_attacks or 0) > 0:
        _record_decision(
            settings,
            state=state, village=village,
            module="defense",
            decision="REINFORCE_SEND",
            outcome=DECISION_OUTCOME_SKIPPED,
            reason=(
                "Nessun rinforzo inviato: il donatore stesso e' sotto "
                "minaccia o rileva attacchi in arrivo"
            ),
        )
        return
    if defense_mgr.is_emergency_locked(my_id, now):
        _record_decision(
            settings,
            state=state, village=village,
            module="defense",
            decision="REINFORCE_SEND",
            outcome=DECISION_OUTCOME_BLOCKED,
            reason=(
                "Rinforzi sospesi: emergency_locked attivo "
                "(emergenza difesa in corso)"
            ),
        )
        return
    if replenish_mgr is not None and replenish_mgr.is_village_frozen(
        village.name or ""
    ):
        _record_decision(
            settings,
            state=state, village=village,
            module="defense",
            decision="REINFORCE_SEND",
            outcome=DECISION_OUTCOME_BLOCKED,
            reason=(
                "Rinforzi sospesi: villaggio congelato per assedio "
                "(siege_waves)"
            ),
        )
        return
    if village.x is None or village.y is None or not rally_slot:
        return

    # Pool difensive: solo unita' da muro della tribu' rilevata;
    # il pacchetto mobilita mobilize_def_pct lasciando in sede almeno
    # min_home_defense_pct di guarnigione.
    troops = dict(state.troops_home or {})
    if not troops:
        troops = await observer.read_troops_home(rally_slot, my_id)
        state.troops_home = dict(troops)
    tribe = settings.tribe or detect_tribe_from_keys(troops.keys())
    send: dict[str, int] = {}
    for unit, qty in troops.items():
        qty = int(qty or 0)
        if qty <= 0 or not is_defensive_unit(unit, tribe):
            continue
        movable = int(qty * max(0.0, cfg.mobilize_def_pct))
        keep = int(qty * max(0.0, cfg.min_home_defense_pct))
        qty_send = min(movable, qty - keep)
        if qty_send > 0:
            send[unit] = qty_send
    if not send:
        return

    for vid, info in sorted(
        targets.items(), key=lambda kv: float(kv[1].get("arrival_ts") or 0.0)
    ):
        target_id = int(vid)
        arrival = float(info.get("arrival_ts") or 0.0)
        secs_left = arrival - now
        if secs_left <= cfg.reinforce_margin_seconds:
            continue
        tname = str(info.get("name") or "")
        # Assedio insostenibile: il bersaglio e' congelato (ondate >=
        # siege_waves) e cedera' comunque — non sacrificare i muri.
        if replenish_mgr is not None and replenish_mgr.is_village_frozen(tname):
            continue
        tx, ty = info.get("x"), info.get("y")
        if tx is None or ty is None:
            tv = next(
                (
                    v
                    for v in villages
                    if int(v.village_id or 0) == target_id
                ),
                None,
            )
            if tv is not None and tv.x is not None and tv.y is not None:
                tx, ty = tv.x, tv.y
        if tx is None or ty is None:
            continue
        # Filtro di arrivo: marcia <= tempo residuo - margine operativo
        travel = travel_seconds(
            send, int(village.x), int(village.y), int(tx), int(ty)
        )
        if travel > secs_left - cfg.reinforce_margin_seconds:
            logger.debug(
                "%s Rinforzo verso '%s' impossibile: marcia %ds > "
                "finestra %ds.",
                prefix, tname, int(travel),
                int(secs_left - cfg.reinforce_margin_seconds),
            )
            _record_decision(
                settings,
                state=state, village=village,
                module="defense",
                decision="REINFORCE_SEND",
                outcome=DECISION_OUTCOME_SKIPPED,
                reason=(
                    f"Rinforzo verso '{tname}' non inviato: tempo di "
                    "marcia superiore alla finestra disponibile"
                ),
                context={
                    "target": tname,
                    "march_seconds": int(travel),
                    "window_seconds": int(
                        secs_left - cfg.reinforce_margin_seconds
                    ),
                },
            )
            continue
        if defense_mgr.reinforce_sent(target_id, my_id, arrival):
            continue

        action = Action(
            type=ActionType.SEND_CUSTOM_RAID,
            params={
                "rally_slot": rally_slot,
                "move_type": "2",  # rinforzi
                "targets": [
                    {"x": int(tx), "y": int(ty), "troops": dict(send)}
                ],
            },
            reason=(
                f"Coordinatore rinforzi: {sum(send.values())} difese "
                f"{village.name} -> '{tname}' ({tx}|{ty}), impatto "
                f"nemico tra {int(secs_left)}s",
            ),
        )
        result = await executor.execute(action, village_id=my_id)
        if not result.success:
            logger.warning(
                "%s Rinforzo verso '%s' fallito: %s",
                prefix, tname, result.message,
            )
            _record_decision(
                settings,
                state=state, village=village,
                module="defense",
                decision="REINFORCE_SEND",
                outcome=DECISION_OUTCOME_BLOCKED,
                reason=(
                    f"Muro di rinforzo verso '{tname}' fallito: "
                    f"{result.message}"
                ),
                context={"target": tname, "troops": dict(send)},
            )
            continue

        _record_decision(
            settings,
            state=state, village=village,
            module="defense",
            decision="REINFORCE_SEND",
            outcome=DECISION_OUTCOME_EXECUTED,
            reason=(
                f"Muro di rinforzo inviato: {sum(send.values())} difese "
                f"verso '{tname}' ({tx}|{ty}), impatto nemico tra "
                f"{int(secs_left)}s"
            ),
            context={
                "target": tname,
                "troops": dict(send),
                "seconds_to_impact": int(secs_left),
            },
        )
        defense_mgr.register_reinforce(
            PendingReinforce(
                donor_village_id=my_id,
                donor_name=village.name or "",
                target_village_id=target_id,
                target_name=tname,
                target_x=int(tx),
                target_y=int(ty),
                attack_arrival_ts=arrival,
                troops=dict(send),
                sent_ts=now,
            ),
            donor_x=village.x,
            donor_y=village.y,
            recall_delay_seconds=float(cfg.recall_delay_seconds),
            auto_recall=bool(cfg.auto_recall_reinforce),
        )
        logger.warning(
            "%s Muro in viaggio verso '%s': %d truppe, impatto nemico "
            "tra %ds (rientro %s).",
            prefix, tname, sum(send.values()), int(secs_left),
            "automatico" if cfg.auto_recall_reinforce else "manuale",
        )

        # Crop safety: se il bersaglio e' gia' in carestia (bilancio
        # grano netto negativo) il donatore accompagna il muro con un
        # carico di grano dal surplus feeder.
        t_res = (last_states or {}).get(tname)
        if (
            t_res is not None
            and t_res.crop_balance is not None
            and t_res.crop_balance < 0
            and market_slot
        ):
            surplus = feeder_surplus(state)
            crop_amt = int(surplus.get("crop", 0) or 0)
            if crop_amt > 0:
                await executor.execute(
                    Action(
                        type=ActionType.SEND_RESOURCES,
                        params={
                            "market_slot": market_slot,
                            "target_village": tname,
                            "target_x": int(tx),
                            "target_y": int(ty),
                            "resources": {"crop": crop_amt},
                        },
                        reason=(
                            f"Coordinatore: {crop_amt} grano d'emergenza "
                            f"verso '{tname}' in carestia"
                        ),
                    ),
                    village_id=my_id,
                )
        # Un muro per donatore per ciclo: le truppe sono partite, le
        # rimanenti destinazioni saranno rivalutate al ciclo successivo.
        break


def _raid_pressure_on_village(
    *,
    village: Village,
    state: GameState,
    villages: list[Village],
    defense_mgr: DefenseManager | None,
) -> bool:
    """True se il villaggio e' sotto pressione raid (spec Anti-Raid).

    Trigger:
    - attacco in corso sui movimenti della Piazza d'Armi;
    - minaccia reale nel registro del DefenseManager;
    - lock d'emergenza registrato nelle ultime 24h (il timestamp di
      fine emergenza resta persistito e attesta un attacco subito);
    - villaggio singolo con almeno un'emergenza passata qualsiasi:
      non ha destinazioni di fuga per le risorse via mercato.
    """
    if int(state.incoming_attacks or 0) > 0:
        return True
    if defense_mgr is None:
        return False
    now = time.time()
    vid = str(int(village.village_id or state.village_id or 0))
    try:
        if vid in defense_mgr.active_threats(now):
            return True
    except Exception:
        pass
    last_lock = float(defense_mgr.emergency_until.get(vid) or 0.0)
    # Storico recente: l'emergenza conclusa entro 24h attesta una
    # razzia subita di recente.
    if last_lock > now - 86400.0:
        return True
    # Villaggio singolo isolato: basta un'emergenza passata — senza
    # alleati propri non esiste fuga risorse via mercato.
    if len(villages) <= 1 and last_lock > 0:
        return True
    return False


def _fields_max_level_by_gid(state: GameState) -> dict[int, int]:
    """gid -> livello massimo tra gli slot del villaggio."""
    out: dict[int, int] = {}
    for f in state.fields or []:
        g = int(getattr(f, "gid", 0) or 0)
        if g > 0:
            out[g] = max(out.get(g, 0), int(getattr(f, "level", 0) or 0))
    return out


def _append_history(
    account_id: str, state: GameState, action: Action, tribe: str = ""
) -> None:
    """Salva lo storico decisionale per-villaggio (DB + JSONL di backup)."""
    try:
        safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
        entry = {
            "ts": state.timestamp,
            "account_id": account_id,
            "village_id": state.village_id,
            "village_name": state.village_name,
            "resources": {
                "wood": state.resources.wood,
                "clay": state.resources.clay,
                "iron": state.resources.iron,
                "crop": state.resources.crop,
                "warehouse_cap": state.resources.warehouse_capacity,
                "granary_cap": state.resources.granary_capacity,
            },
            # Arricchimento spec "Role-Aware Configurator": produzione,
            # netto grano, livelli edifici e truppe alimentano il
            # Capacity-Aware Scaling e la Live Preview della dashboard.
            "production": {
                str(getattr(k, "value", k)): int(v)
                for k, v in (state.resources.production_per_hour or {}).items()
            },
            "crop_balance": state.resources.crop_balance,
            # gid -> livello max: piu' campi risorsa condividono lo
            # stesso gid (1-4), serve il massimo non l'ultimo letto.
            "buildings": {
                str(g): lvl for g, lvl in _fields_max_level_by_gid(state).items()
            },
            "troops": dict(state.troops_home or {}),
            # Tribu' account per il Role-Aware Configurator (seed unita').
            "tribe": tribe,
            "build_queue": len(state.build_queue),
            "incoming_attacks": state.incoming_attacks,
            "action": action.type.value,
            "reason": action.reason,
        }
        db.history_insert(safe_id, entry)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path: Path = HISTORY_DIR / f"history_{safe_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("Scrittura storico fallita", exc_info=True)


# ---------------------------------------------------------------------------
# Decision Audit Log (spec): eventi strutturati per modulo/decisione/esito
# ---------------------------------------------------------------------------

DECISION_OUTCOME_EXECUTED = "EXECUTED"
DECISION_OUTCOME_SKIPPED = "SKIPPED"
DECISION_OUTCOME_BLOCKED = "BLOCKED"

# Anti-spam: SKIPPED/BLOCKED identici (stessa chiave villaggio+modulo+
# decisione+motivo normalizzato) non vengono riscritti entro il throttle.
_AUDIT_SKIP_THROTTLE_SECONDS = 1800.0  # 30 min come da spec (15-30)
_audit_last_write: dict[tuple, float] = {}
# Compattazione JSONL dello storico: al massimo una volta l'ora/account.
_audit_jsonl_compacted: dict[str, float] = {}
_AUDIT_JSONL_COMPACT_EVERY = 3600.0

# Decisioni che NON mettono in pausa il modulo: diagnostica, stati di
# monitoraggio benigni ed esiti di pause gia' attive. Senza esenzioni,
# uno SKIPPED di routine (es. 'step coda scavalcato', 'attacco solo
# monitorato', 'drain non necessario') spegnerebbe per 15-30 min
# moduli che invece devono restare sempre attivi (difesa = emergenze).
_PAUSE_EXEMPT_DECISIONS = frozenset(
    {
        "PRIORITY_QUEUE_STEP",   # diagnostica builder, non un fallimento
        "ATTACK_MONITORED",     # stato di sorveglianza, non un blocco
        "ATTACK_DETECTED",      # rilevamento, gia' EXECUTED comunque
        "RESOURCE_DRAIN",       # 'stock basso' e' benigno e frequente
        "MARKET_DRAIN",         # 'surplus coperto dai cranny' benigno
        "TROOP_DODGE",          # skip per poche truppe: il monitor resta
        "DODGE_SCHEDULED",
        "EMERGENCY_LOCK",
        "MODULE_PAUSED",        # esito del gate stesso: non ri-pausare
        "MODULE_EVENT",         # fallback generico dei moduli
        "NEW_VILLAGE_BOOTSTRAP",
        "FEEDER_PUSH_IDLE",     # 'nessun surplus' e' benigno: niente pausa
    }
)
_PAUSE_EXEMPT_PREFIXES = ("MANUAL_",)  # comandi utente dal Command Center


def _audit_entry(
    settings: Settings,
    state: GameState | None,
    village: Village | None,
    *,
    module: str,
    decision: str,
    outcome: str,
    reason: str,
    context: dict | None = None,
) -> dict:
    """Evento audit uniforme con metadati di villaggio/ruolo/risorse."""
    vname = ""
    vid = 0
    if state is not None:
        vname = state.village_name or ""
        vid = int(state.village_id or 0)
    if village is not None:
        vname = vname or (village.name or "")
        vid = vid or int(village.village_id or 0)
    role = ""
    try:
        vcfg = settings.village_config_for(vname, vid)
        role = str(getattr(vcfg, "role", "") or "")
    except Exception:
        role = ""
    entry = {
        "ts": time.time(),
        "account_id": settings.account_id,
        "village_id": vid,
        "village_name": vname,
        "village_role": role,
        "module": module,
        "decision": decision,
        "outcome": outcome,
        "reason": reason,
        "context": dict(context or {}),
    }
    if state is not None:
        res = state.resources
        entry["context"].setdefault(
            "resources",
            {
                "wood": res.wood,
                "clay": res.clay,
                "iron": res.iron,
                "crop": res.crop,
            },
        )
        entry["context"].setdefault("queue_used", len(state.build_queue))
        entry["context"].setdefault("queue_max", settings.max_build_queue)
    return entry


def _audit_reason_key(reason: str) -> str:
    """Normalizza la motivazione per la dedup: le cifre (tempi,
    quantita') non devono rendere 'nuovo' un evento identico."""
    return "".join("#" if c.isdigit() else c for c in reason)


def _record_decision(
    settings: Settings,
    *,
    state: GameState | None = None,
    village: Village | None = None,
    module: str,
    decision: str,
    outcome: str,
    reason: str,
    context: dict | None = None,
) -> None:
    """Registra un evento del Decision Audit Log (DB + JSONL).

    Deduplica gli stati stabili: SKIPPED/BLOCKED identici sullo stesso
    villaggio/modulo/decisione non vengono riscritti prima di
    ``_AUDIT_SKIP_THROTTLE_SECONDS`` (solo le transizioni di stato e
    gli eventi significativi entrano nel log).
    """
    try:
        entry = _audit_entry(
            settings,
            state,
            village,
            module=module,
            decision=decision,
            outcome=outcome,
            reason=reason,
            context=context,
        )
        # Anti-detection: un modulo che fallisce (SKIPPED/BLOCKED) va in
        # pausa 15-30 min invece di riprovare al ciclo dopo. Registrato
        # PRIMA della dedup: eventi ripetuti rinnovano la pausa.
        _vkey = str(entry["village_id"] or entry["village_name"] or "0")
        if outcome == DECISION_OUTCOME_EXECUTED:
            # Un successo sblocca subito la pausa della stessa decisione.
            _module_pauses.clear(
                settings.account_id, _vkey, module, decision
            )
        elif (
            outcome in (
                DECISION_OUTCOME_SKIPPED,
                DECISION_OUTCOME_BLOCKED,
            )
            and getattr(settings, "stealth_pauses", True)
            and decision not in _PAUSE_EXEMPT_DECISIONS
            and not decision.startswith(_PAUSE_EXEMPT_PREFIXES)
        ):
            _module_pauses.register(
                settings.account_id,
                _vkey,
                module,
                decision,
                resources=state.resources if state is not None else None,
                min_seconds=settings.module_pause_minutes * 60.0,
                max_seconds=settings.module_pause_max_minutes * 60.0,
            )
        if outcome in (
            DECISION_OUTCOME_SKIPPED,
            DECISION_OUTCOME_BLOCKED,
        ):
            key = (
                settings.account_id,
                entry["village_id"],
                module,
                decision,
                _audit_reason_key(reason),
            )
            now = entry["ts"]
            if (
                now - _audit_last_write.get(key, 0.0)
                < _AUDIT_SKIP_THROTTLE_SECONDS
            ):
                return
            _audit_last_write[key] = now

        safe_id = "".join(
            c if c.isalnum() or c in "._-" else "_"
            for c in settings.account_id
        )
        db.decision_insert(safe_id, entry)
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path: Path = HISTORY_DIR / f"history_{safe_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _compact_audit_jsonl(safe_id, path)
    except Exception:
        logger.debug("Decision audit non registrato", exc_info=True)


def _compact_audit_jsonl(safe_id: str, path: Path) -> None:
    """Rotazione del JSONL storico: ultimi 7 giorni / 5000 righe,
    al massimo una compattazione l'ora per account."""
    try:
        now = time.time()
        if (
            now - _audit_jsonl_compacted.get(safe_id, 0.0)
            < _AUDIT_JSONL_COMPACT_EVERY
        ):
            return
        _audit_jsonl_compacted[safe_id] = now
        lines = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
        cutoff = now - db.DECISION_LOG_MAX_AGE_SECONDS
        scan = lines[-(db.DECISION_LOG_MAX_ROWS * 3):]
        kept: list[str] = []
        for line in scan:
            try:
                ts = float(json.loads(line).get("ts") or 0.0)
            except (ValueError, AttributeError):
                ts = 0.0
            if ts >= cutoff:
                kept.append(line)
        kept = kept[-db.DECISION_LOG_MAX_ROWS:]
        if len(kept) != len(lines):
            path.write_text(
                "".join(line + "\n" for line in kept),
                encoding="utf-8",
            )
    except Exception:
        logger.debug("Compattazione audit JSONL fallita", exc_info=True)


def _flush_module_audits(
    settings: Settings,
    *,
    module: str,
    state: GameState | None = None,
    village: Village | None = None,
    managers: tuple = (),
) -> None:
    """Svuota i buffer ``drain_audit()`` dei manager di modulo nel
    Decision Audit Log (eventi decision/outcome/reason/context)."""
    for mgr in managers:
        if mgr is None:
            continue
        try:
            events = mgr.drain_audit()
        except Exception:
            events = []
        for ev in events:
            _record_decision(
                settings,
                state=state,
                village=village,
                module=module,
                decision=str(ev.get("decision") or "MODULE_EVENT"),
                outcome=str(
                    ev.get("outcome") or DECISION_OUTCOME_EXECUTED
                ),
                reason=str(ev.get("reason") or ""),
                context=(
                    ev.get("context")
                    if isinstance(ev.get("context"), dict)
                    else None
                ),
            )


# Modulo audit per tipo azione del brain: le azioni di building
# restano sotto 'builder', le altre rientrano nel dominio del modulo
# che le emette tipicamente (espansione, rimpiazzo, raid, fucina).
_ACTION_MODULE_MAP = {
    ActionType.TRAIN_SETTLERS: "expansion",
    ActionType.SEND_RESOURCES: "expansion",
    ActionType.TRAIN_TROOPS: "replenishment",
    ActionType.SEND_CUSTOM_RAID: "farming",
    ActionType.UPGRADE_UNITS: "smithy",
    ActionType.RESEARCH_UNIT: "academy",
}


def _action_module(action: Action) -> str:
    return _ACTION_MODULE_MAP.get(action.type, "builder")


def _academy_pending_units(
    wanted: list[str],
    ac_state: dict,
    academy_level: int,
    trainable: set[str] | None = None,
) -> list[dict]:
    """Unita' autorizzate che richiedono ancora una ricerca in Accademia.

    Confronta ``wanted`` (chiavi tN, ordine = priorita' dashboard) con
    lo stato letto da ``read_academy_state``:

    - ``researchable`` -> ``actionable``: il pulsante Ricerca e'
      abilitato (prerequisiti e risorse gia' valutati lato DOM);
    - ``researched`` o assente dal DOM senza prerequisito noto ->
      omessa (gia' sbloccata o non soggetta a ricerca: truppa base,
      coloni);
    - assente dal DOM ma presente in ``UNIT_ACADEMY_LEVELS`` ->
      verita' sul trainer (``trainable`` = chiavi addestrabili lette
      dalla form del building che la addestra);
    - ``blocked``/``unknown`` -> non actionable, ``reason`` motiva il
      blocco (livello Accademia insufficiente secondo la tabella).

    Le unita' 'in_progress' restano in coda come promemoria ma non
    sono actionable (la guard ``research_active`` le filtra a monte).
    """
    units = (ac_state or {}).get("units") or {}
    trainable = trainable or set()
    out: list[dict] = []
    for raw_key in wanted or []:
        key = normalize_unit_key(raw_key)
        if not key or not (key[1:].isdigit()):
            continue
        label = STATIC_TROOP_LABELS.get(key, key)
        req_level = int(UNIT_ACADEMY_LEVELS.get(key, 0) or 0)
        entry = units.get(key)
        if entry is None:
            # Assente dal DOM dell'Accademia: su alcune UI le unita'
            # ricercate spariscono dalla tabella. Se la scheda tribu'
            # non la prevede in Accademia non serve ricerca; altrimenti
            # decide il trainer (input tN presente = gia' sbloccata).
            if not req_level or key in trainable:
                continue
            reason = "stato ricerca non leggibile in Accademia"
            if req_level and academy_level < req_level:
                reason = (
                    f"Accademia liv.{academy_level} < {req_level} "
                    f"richiesto per {label}"
                )
            out.append({
                "unit": key,
                "label": label,
                "actionable": False,
                "required_level": req_level,
                "reason": reason,
            })
            continue
        status = str(entry.get("status") or "")
        if status == "researchable":
            out.append({
                "unit": key,
                "label": label,
                "actionable": True,
                "required_level": req_level,
                "costs": dict(entry.get("costs") or {}),
            })
        elif status == "researched":
            continue
        elif status == "in_progress":
            out.append({
                "unit": key,
                "label": label,
                "actionable": False,
                "required_level": req_level,
                "reason": f"ricerca di {label} gia' in corso",
            })
        else:  # blocked | unknown
            reason = f"ricerca di {label} non disponibile (prerequisiti o risorse)"
            if req_level and academy_level < req_level:
                reason = (
                    f"Accademia liv.{academy_level} < {req_level} "
                    f"richiesto per {label}"
                )
            out.append({
                "unit": key,
                "label": label,
                "actionable": False,
                "required_level": req_level,
                "reason": reason,
            })
    return out


def _apply_slot_corrections(
    settings: Settings,
    village_id: int,
    fields: list[Field],
) -> int:
    """Applica le rettifiche persistenti slot->gid alla scansione dorf2.

    Le voci in ``slot_corrections`` (DB) sono state verificate su pagina
    edificio (``div#build.gidN``), fonte autoritativa rispetto alla
    mappa dorf2 — che su alcune UI associa gid errati agli slot.

    - mappa ancora coerente col ``detected_gid`` registrato (o gid non
      letto): il campo assume l'``actual_gid`` verificato;
    - mappa concorda gia' con ``actual_gid``: rettifica superflua,
      rimossa;
    - gid diverso da entrambi: lo slot e' cambiato davvero
      (costruzione/demolizione) -> voce invalidata, non congelata.

    Ritorna il numero di campi rettificati.
    """
    vid = int(village_id or 0)
    if vid <= 0 or not fields:
        return 0
    stored = db.slot_corrections_load(settings.account_id, vid)
    if not stored:
        return 0
    stale: list[int] = []
    applied = 0
    for f in fields:
        entry = stored.get(int(f.slot_id or 0))
        if entry is None:
            continue
        fresh = int(f.gid if f.gid is not None else -1)
        detected = int(entry.get("detected_gid", -1))
        actual = int(entry.get("actual_gid", -1))
        if actual < 0 or fresh == actual:
            # Voce priva di gid verificato o mappa ora concorde col
            # reale: la rettifica non serve piu'.
            stale.append(f.slot_id)
            continue
        if fresh == detected or fresh < 0:
            f.gid = actual
            applied += 1
            continue
        stale.append(f.slot_id)
    if stale:
        db.slot_corrections_delete(settings.account_id, vid, stale)
    return applied


def _persist_slot_corrections(
    settings: Settings,
    village: Village,
    fields: list[Field],
    corrections: dict,
    resolved_slot: int = 0,
) -> None:
    """Salva su DB le rettifiche slot->gid emesse dai sensori.

    ``corrections`` = ``{slot_id: gid_reale}`` da ``read_academy_state``;
    ``detected_gid`` e' il valore che la mappa corrente associa allo
    slot (pre-correzione): serve in lettura a distinguere una mappa
    ancora errata da uno slot davvero cambiato. ``resolved_slot`` marca
    la sede risolta dell'edificio cercato (es. Accademia gid 22).
    """
    vid = int(village.village_id or 0)
    if vid <= 0 or not corrections:
        return
    pre_gid = {
        int(f.slot_id or 0): int(f.gid if f.gid is not None else -1)
        for f in fields or []
    }
    entries = [
        {
            "slot_id": int(sid),
            "detected_gid": pre_gid.get(int(sid), -1),
            "actual_gid": int(g),
            "source": "academy_probe",
        }
        for sid, g in corrections.items()
        if int(sid or 0) > 0
    ]
    db.slot_corrections_upsert(
        settings.account_id,
        vid,
        entries,
        resolved_slot=int(resolved_slot or 0) or None,
    )


INITIAL_BACKOFF = 10.0
MAX_BACKOFF = 300.0
BACKOFF_FACTOR = 2.0
JITTER_MIN = 0.9
JITTER_MAX = 1.1


def _calculate_backoff(attempt: int, base: float = INITIAL_BACKOFF, maximum: float = MAX_BACKOFF) -> float:
    """Calcola il ritardo di backoff esponenziale con jitter per evitare picchi simultanei."""
    delay = min(base * (BACKOFF_FACTOR ** max(0, attempt - 1)), maximum)
    return delay * random.uniform(JITTER_MIN, JITTER_MAX)


# Mappa tipo-movimento -> categoria radar per i sync passivi.
_RADAR_CAT_IN = {
    "attack": CAT_INCOMING_ATTACK,
    "raid": CAT_INCOMING_ATTACK,
    "scout": CAT_INCOMING_ATTACK,
    "unknown": CAT_INCOMING_ATTACK,
    "reinforce": CAT_REINFORCEMENT,
    "merchant": CAT_MERCHANT,
    "return": CAT_TROOP_RETURN,
}
_RADAR_CAT_OUT = {
    "attack": CAT_OUTGOING_ATTACK,
    "raid": CAT_OUTGOING_RAID,
    "scout": CAT_OUTGOING_RAID,
    "unknown": CAT_OUTGOING_ATTACK,
    "reinforce": CAT_REINFORCEMENT,
    "merchant": CAT_MERCHANT,
    "return": CAT_TROOP_RETURN,
}
# Categorie la cui assenza nella pagina corrente e' verita' (il rally
# overview e il box #movements elencano TUTTI i movimenti truppe).
# CAT_MERCHANT esclusa: viene sincronizzata dalla tabella Mercato, che
# e' la fonte definitiva — le righe '#movements' aggiungono vettori ma
# non ne dichiarano la completezza.
_RADAR_SYNC_CATS = frozenset({
    CAT_INCOMING_ATTACK,
    CAT_OUTGOING_ATTACK,
    CAT_OUTGOING_RAID,
    CAT_REINFORCEMENT,
    CAT_TROOP_RETURN,
})


def _mv_get(mv, key: str, default=None):
    """Accessor duck-typed: IncomingAttack o dict del box #movements."""
    if isinstance(mv, dict):
        return mv.get(key, default)
    return getattr(mv, key, default)


def _resolve_counterpart_coords(settings: Settings, who: str):
    """(x, y) della controparte se e' un villaggio noto dell'account.

    Il box '#movements' di dorf1 non espone link mappa: il nome del
    villaggio controparte nel label (es. 'Pizzo') viene risolto via
    ``settings.village_config_for`` / scansione dei nomi configurati.
    Ritorna ``(None, None)`` per controparti esterne.
    """
    text = str(who or "").strip()
    if not text:
        return None, None
    low = text.lower()
    try:
        villages = getattr(settings, "villages", None) or {}
        # Match esatto nome -> config (chiave o campo name).
        for key, cfg in villages.items():
            names = {str(getattr(cfg, "name", "") or ""), str(key)}
            for nm in names:
                nm = nm.strip().lower()
                if nm and nm in low:
                    cx = getattr(cfg, "x", None)
                    cy = getattr(cfg, "y", None)
                    if cx is not None and cy is not None:
                        return cx, cy
    except Exception:  # noqa: BLE001
        pass
    return None, None


def _sync_radar_movements(
    settings: Settings,
    vname: str,
    sx,
    sy,
    inc: list,
    outg: list,
    *,
    prefix: str = "",
    reconcile: bool = True,
) -> None:
    """Sincronizza i movimenti osservati nel RadarStore dell'account.

    ``inc``/``outg`` accettano ``IncomingAttack`` (panoramica rally +
    box #movements fusi dal sensore) oppure dict grezzi. Il dedup dei
    vettori registrati dalle azioni bot avviene nello store (stessa
    categoria+direzione+arrives_at entro 15s).

    ``reconcile=False`` (scansione rally non riuscita, solo box dorf1):
    i vettori militari vengono aggiunti/aggiornati ma NON rimossi —
    il box '#movements' non dichiara la completezza delle categorie.
    """
    vkey = str(vname or "")
    if not vkey:
        return
    try:
        rs = radar_store(settings.account_id)
        moves: list[dict] = []
        now = time.time()
        for mv in inc or []:
            mtype = str(_mv_get(mv, "movement_type",
                                _mv_get(mv, "type") or "unknown"))
            _is_return = mtype == "return"
            cat = _RADAR_CAT_IN.get(mtype, CAT_INCOMING_ATTACK)
            arr = float(_mv_get(mv, "arrival_ts", 0.0) or 0.0)
            if arr <= 0:
                arr = now + float(
                    _mv_get(mv, "seconds_remaining",
                            _mv_get(mv, "seconds") or 0) or 0
                )
            if arr <= now:
                continue
            cx = _mv_get(mv, "counterpart_x", _mv_get(mv, "cx"))
            cy = _mv_get(mv, "counterpart_y", _mv_get(mv, "cy"))
            who = str(
                _mv_get(mv, "attacker", "") or _mv_get(mv, "label", "")
                or "Movimento in arrivo"
            )[:80]
            _approx = False
            if cx is None or cy is None:
                cx, cy = _resolve_counterpart_coords(settings, who)
                _approx = cx is None or cy is None
            moves.append(
                {
                    "category": cat,
                    "direction": "return" if _is_return else "inbound",
                    "from_x": cx,
                    "from_y": cy,
                    "to_x": sx,
                    "to_y": sy,
                    "counterpart": who,
                    "label": who,
                    "arrives_at": arr,
                    "details": {
                        "troops_summary": str(
                            _mv_get(mv, "troop_count",
                                    _mv_get(mv, "count") or "") or ""
                        ),
                        "approx_coords": _approx,
                    },
                }
            )
        for mv in outg or []:
            mtype = str(_mv_get(mv, "movement_type",
                                _mv_get(mv, "type") or "unknown"))
            _is_return = mtype == "return"
            cat = _RADAR_CAT_OUT.get(mtype, CAT_OUTGOING_ATTACK)
            arr = float(_mv_get(mv, "arrival_ts", 0.0) or 0.0)
            if arr <= 0:
                arr = now + float(
                    _mv_get(mv, "seconds_remaining",
                            _mv_get(mv, "seconds") or 0) or 0
                )
            if arr <= now:
                continue
            cx = _mv_get(mv, "counterpart_x", _mv_get(mv, "cx"))
            cy = _mv_get(mv, "counterpart_y", _mv_get(mv, "cy"))
            who = str(
                _mv_get(mv, "attacker", "") or _mv_get(mv, "label", "")
                or "Movimento in partenza"
            )[:80]
            _approx = False
            if cx is None or cy is None:
                cx, cy = _resolve_counterpart_coords(settings, who)
                _approx = cx is None or cy is None
            moves.append(
                {
                    "category": cat,
                    # Rientro osservato nella sezione 'out' del rally
                    # (truppe proprie che tornano da un altro villaggio):
                    # vettore di ritorno controparte -> villaggio.
                    "direction": "return" if _is_return else "outbound",
                    "from_x": cx if _is_return else sx,
                    "from_y": cy if _is_return else sy,
                    "to_x": sx if _is_return else cx,
                    "to_y": sy if _is_return else cy,
                    "counterpart": who,
                    "label": who,
                    "arrives_at": arr,
                    "details": {
                        "troops_summary": str(
                            _mv_get(mv, "troop_count",
                                    _mv_get(mv, "count") or "") or ""
                        ),
                        "approx_coords": _approx,
                    },
                }
            )
        _n = rs.sync_categories(
            vkey, moves, _RADAR_SYNC_CATS if reconcile else frozenset()
        )
        logger.info(
            "%s Radar: villaggio '%s' — %d in/%d out letti, %d vettori "
            "attivi nello store",
            prefix, vkey, len(inc or []), len(outg or []),
            _n if isinstance(_n, int) else len(moves),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s Sync radar movimenti fallita: %s", prefix, exc
        )


async def monitor_all_villages(
    browser: TravianBrowser,
    observer: GameObserver,
    brain: RuleBasedBrain | LLMBrain,
    executor: ActionExecutor,
    settings: Settings,
    prefix: str,
    last_states: dict[str, Resources] | None = None,
    farming_mgr: FarmingManager | None = None,
    expansion_mgr: ExpansionManager | None = None,
    scout_brain: AIScoutBrain | None = None,
    hero_mgr: HeroManager | None = None,
    defense_mgr: DefenseManager | None = None,
    replenish_mgr: TroopReplenishmentManager | None = None,
    war_store: WarStore | None = None,
) -> None:
    """Ciclo di controllo multi-villaggio per l'account corrente.

    Flusso per ogni villaggio estratto dalla sidebar (parametro newdid):
    1. navigazione all'URL del villaggio
    2. lettura dello stato specifico (risorse, campi, coda, attacchi)
    3. decisione del brain e esecuzione dell'azione con contesto validato
    4. moduli autonomi: Farming (farm list), Espansione e AI Scout Brain (raid dinamico)
    5. pausa umana tra un villaggio e l'altro
    """
    # Assicuriamoci di essere sul dorf1 prima di leggere la sidebar (così i box dei villaggi sono presenti)
    if selectors.PATH_DORF1 not in (browser.page.url or ""):
        await browser.goto(selectors.PATH_DORF1)

    # Self-healing al boot: dal primo dorf1/dorf2 il profilo del server
    # apprende il routing edificio (legacy build.php vs modern /slot).
    try:
        await calibrate_routing(browser.page)
    except Exception:  # noqa: BLE001
        pass

    # 1. Estrae la lista dei villaggi dell'account
    villages = await observer.get_all_villages()

    if not villages:
        # Fallback di sicurezza: prova a rileggere o usa il villaggio corrente
        logger.warning("%s Lista villaggi vuota dalla sidebar. Tentativo di fallback sul villaggio attivo.", prefix)
        current_name = await observer.extract_village_name() or "corrente"
        villages = [Village(name=current_name)]
    else:
        logger.info(
            "%s Trovati %d villaggi nell'account: %s",
            prefix,
            len(villages),
            ", ".join(v.name for v in villages),
        )

    states_by_village: dict[str, GameState] = {}
    troops_by_village: dict[str, dict[str, int]] = {}

    # Mappa coordinate note dei villaggi (nome -> (x, y))
    village_coords_map: dict[str, tuple[int, int]] = {}
    for v in villages:
        # Backfill da VillageConfig se la sidebar non espone le coordinate:
        # il CropperScanner ha bisogno di villaggi con x/y come centri scansione.
        cfg = settings.village_config_for(v.name, v.village_id)
        if cfg is not None:
            if v.x is None and cfg.x is not None:
                v.x = cfg.x
            if v.y is None and cfg.y is not None:
                v.y = cfg.y
        if v.name and v.x is not None and v.y is not None:
            village_coords_map[v.name.strip().lower()] = (v.x, v.y)
        # Aggiorna anche VillageConfig se x/y non erano ancora valorizzati
        if cfg is not None:
            if v.x is not None and cfg.x is None:
                cfg.x = v.x
            if v.y is not None and cfg.y is None:
                cfg.y = v.y
            if cfg.x is not None and cfg.y is not None and v.name:
                village_coords_map[v.name.strip().lower()] = (cfg.x, cfg.y)

    # 1.05 Coordinate mancanti: fallback via mappa per i server dove la
    # sidebar non espone (x|y). Rilevate una tantum e persistite su
    # accounts.json; serve soprattutto alla scansione cropper.
    if settings.expansion.cropper_scan or settings.expansion.auto_expand:
        missing = [
            v for v in villages
            if (v.x is None or v.y is None) and v.village_id
        ]
        if missing:
            learned = False
            for v in missing[:5]:
                try:
                    coords = await observer.read_village_coords(v.village_id)
                except Exception:
                    coords = None
                if coords is None:
                    continue
                v.x, v.y = coords
                learned = True
                # Nota: non si scrive nel VillageConfig live (il fallback
                # per nome colliderebbe con villaggi omonimi); la
                # persistenza in _persist_village_coords gestisce il
                # caso e l'hot-reload la rilegge al ciclo successivo.
                if v.name:
                    village_coords_map[v.name.strip().lower()] = coords
                logger.info(
                    "%s Coordinate '%s' rilevate via mappa: (%d|%d)",
                    prefix, v.name, v.x, v.y,
                )
            if learned:
                _persist_village_coords(settings, villages)
            else:
                logger.debug(
                    "%s Coordinate non rilevate per %d villaggi "
                    "(mappa non leggibile o layout ignoto).",
                    prefix, len(missing),
                )

    # 1.06 New Village Bootstrap: il newdid appena comparso in sidebar
    # sulle coordinate di un tile 'settled' ottiene la VillageConfig
    # automatica (croplog 9c/15c, feeder standard) con is_bootstrap.
    _register_founded_villages(settings, expansion_mgr, villages)

    # 1.07 Reclaim & Chiefing: lo storico dei villaggi configurati viene
    # confrontato con la sidebar — coordinate scomparse = STOLEN_VILLAGE
    # (registrate in war_targets_<acc>.json), coordinate 'reclaiming'
    # ricomparse = villaggio riconquistato -> re-onboarding + bootstrap.
    if war_store is not None:
        try:
            await _reconcile_village_losses(
                settings, observer, villages, war_store,
                expansion_mgr, prefix,
            )
        except Exception as war_exc:
            logger.warning(
                "%s Errore modulo reclaim (non bloccante): %s",
                prefix, war_exc,
            )

    # 1.1 Controllo e attivazione automatica del bonus +15% per 8 ore su tutte le risorse (video ad)
    if settings.auto_video_bonus:
        try:
            now = time.time()
            last_claim = _last_video_bonus_claim.get(settings.account_id, 0.0)
            has_notification = await observer.has_video_bonus_available()
            # Riscatta se sono trascorse ~7.8 ore (28000s) o se l'InfoBox segnala la disponibilità
            if (now - last_claim >= 28000.0) or has_notification:
                logger.info(
                    "%s Verifica e riscatto automatico bonus produzione +15%% (8 ore) via video...",
                    prefix,
                )
                claim_action = Action(
                    type=ActionType.CLAIM_VIDEO_BONUS,
                    reason="Attivazione automatica bonus +15% produzione risorse per 8 ore (video ad)",
                )
                claim_result = await executor.execute(claim_action)
                # Registra il timer solo se il bonus e' stato davvero accreditato
                # o era gia' attivo: un fallimento viene ritentato al ciclo dopo.
                claimed = claim_result.success and (
                    "attivati" in claim_result.message
                    or "già attivi" in claim_result.message
                )
                if claimed:
                    _last_video_bonus_claim[settings.account_id] = now
                    logger.info("%s Bonus video +15%% registrato per le prossime 8 ore", prefix)
                elif claim_result.success:
                    logger.debug(
                        "%s Bonus video: %s (nessuna attivazione confermata)",
                        prefix,
                        claim_result.message,
                    )
                else:
                    logger.warning(
                        "%s Riscatto bonus video fallito: %s",
                        prefix,
                        claim_result.message,
                    )
        except Exception as vb_exc:
            # Senza questa guardia un fallimento qui abortirebbe l'intero
            # ciclo: villaggi, espansione ed eroe non verrebbero mai eseguiti.
            logger.warning(
                "%s Errore bonus video (ciclo continua): %s", prefix, vb_exc
            )

    # Edifici interni per villaggio: serve ai moduli farming/espansione
    interior_map: dict[str, list[Field]] = {}

    # Custom Farm Target Manager: store condiviso dei bersagli di raid
    # (scoperta via scansione mappa, approvazione dashboard, raid mirati).
    farm_store = farm_target_store(settings.account_id)

    # 2. Itera su ciascun villaggio trovato
    for v_idx, village in enumerate(villages):
        village.role = settings.village_role_for(village.name, village.village_id)
        vname = village.name or f"villaggio {v_idx + 1}"
        vprefix = f"{prefix}[{vname}:{village.role}]"
        try:
            # Naviga al villaggio specifico (l'URL contiene già il newdid).
            # Anti-detection: con dom_navigation si prova il click sulla
            # sidebar prima di un GET diretto dell'URL completo.
            if village.url:
                _nav_ok = False
                if getattr(settings, "dom_navigation", False):
                    try:
                        _nav_ok = await navigate_via_dom(
                            browser.page, village.url
                        )
                    except Exception:
                        _nav_ok = False
                if not _nav_ok:
                    if village.url.startswith("http"):
                        await browser.page.goto(
                            village.url, wait_until="domcontentloaded"
                        )
                    else:
                        await browser.goto(village.url)
            elif v_idx > 0:
                logger.warning("%s Villaggio '%s' privo di URL newdid, impossibile navigare.", prefix, vname)
                continue  # senza URL non si può cambiare villaggio

            # 2.5 Riscossione task/premi del villaggio — ON-DEMAND
            #     (spec 4): i premi sono una riserva non depredabile;
            #     si incassano solo se sbloccano un obiettivo pendente
            #     (next upgrade, coloni) senza overflow e senza attacchi
            #     in arrivo. I premi sono localizzati per insediamento,
            #     quindi va fatto in questo contesto prima delle routine
            #     economiche/militari; lo step 3 rilegge lo stock col
            #     premio gia' incassato e il builder lo spende nel
            #     medesimo ciclo.
            try:
                claim_ctx = _task_claim_ctx(
                    settings,
                    village=village,
                    vname=vname,
                    last_states=last_states,
                    expansion_mgr=expansion_mgr,
                    defense_mgr=defense_mgr,
                )
                claimed = await executor.claim_village_tasks(
                    village_id=village.village_id,
                    village_name=vname,
                    claim_ctx=claim_ctx,
                )
                if claimed:
                    logger.info(
                        "%s Task completate: %d premi riscattati con successo.",
                        vprefix, claimed,
                    )
            except Exception as t_exc:
                logger.debug("%s Check task fallito (non bloccante): %s", vprefix, t_exc)

            # Assicura di essere sul dorf1 per leggere i campi e la coda correttamente
            if selectors.PATH_DORF1 not in (browser.page.url or ""):
                await browser.goto(selectors.PATH_DORF1)

            # 3. Stato specifico di QUESTO villaggio
            state = await observer.read_state()
            state.fields = await observer.read_fields()
            state.build_queue = await observer.read_build_queue()

            # Cache persistente dei campi dorf1 per village_id: ogni
            # lettura non vuota aggiorna l'ultimo stato noto; se il ciclo
            # ha visitato solo dorf2 (o la lettura e' tornata vuota) i
            # 18 campi vengono ripopolati da _last_known_fields — con
            # seed dall'ultimo dashboard_state_<acc>.json su disco al
            # primo accesso dopo un riavvio — prima di brain.decide() e
            # della telemetria, cosi' fields_count non torna mai a 0
            # dopo un'azione su dorf2.php.
            _fkey = (
                f"{settings.account_id}:"
                f"{village.village_id or village.name or '0'}"
            )
            _res_fields = [f for f in state.fields if f.is_resource_field]
            if _res_fields:
                _last_known_fields[_fkey] = _res_fields
            else:
                if _fkey not in _last_known_fields:
                    _seed = _snapshot_dorf1_fields(settings, village)
                    if _seed:
                        _last_known_fields[_fkey] = _seed
                        logger.debug(
                            "%s Campi dorf1 seedati da snapshot (%d).",
                            vprefix,
                            len(_seed),
                        )
                if _fkey in _last_known_fields:
                    state.fields = list(_last_known_fields.get(_fkey, []))
                    logger.debug(
                        "%s Campi dorf1 ripopolati da stato noto (%d): "
                        "lettura corrente vuota.",
                        vprefix,
                        len(state.fields),
                    )

            # Edifici interni del dorf2 (slot 19-40): fusi in state.fields
            interior = await observer.read_interior_fields()
            if interior:
                # Rettifiche persistenti slot->gid (mappa dorf2 errata su
                # alcune UI): applicate al caricamento, prima che brain,
                # Accademia e planner valutino i campi. Le voci stale
                # (slot cambiato davvero) vengono invalidate sul DB.
                _apply_slot_corrections(
                    settings,
                    int(village.village_id or state.village_id or 0),
                    interior,
                )
                state.fields = [f for f in state.fields if f.is_resource_field] + interior
                interior_map[village.name or str(village.village_id)] = interior
                logger.debug(
                    "%s Edifici interni rilevati: %d", vprefix, len(interior)
                )

            # Produzione oraria e bilancio netto grano (production.php):
            # serve alla Grain Safety Net dello Smart Building planner.
            try:
                prod = await observer.read_production(village.village_id)
                if prod:
                    state.resources.production_per_hour = prod.get(
                        "production", {}
                    )
                    cb = prod.get("crop_balance")
                    if cb is not None:
                        # None = riga bilancio non letta: NON stimare
                        # il netto dalla produzione lorda.
                        state.resources.crop_balance = int(cb)
            except Exception as prod_exc:
                logger.debug(
                    "%s Lettura produzione fallita (non bloccante): %s",
                    vprefix, prod_exc,
                )

            if village.name:
                state.village_name = village.name
            if not state.village_id and village.village_id:
                state.village_id = village.village_id

            # DOM & Sensor Health Check (debug pipeline): selettori
            # rotti o layout cambiato -> SENSOR_ANOMALY sul trace.
            try:
                check_state_health(state, account_id=settings.account_id)
            except Exception:
                pass

            # Bootstrap auto-off (spec 2): requisiti minimi raggiunti ->
            # is_bootstrap=False e passaggio al ruolo assegnato.
            vcfg_boot = settings.village_config_for(
                village.name, village.village_id
            )
            if (
                vcfg_boot is not None
                and vcfg_boot.is_bootstrap
                and bootstrap_complete(state)
            ):
                _complete_bootstrap(settings, expansion_mgr, village)

            # Snapshot degli altri villaggi: serve al brain per scegliere
            # dove inviare le risorse in eccesso (emergenza storage pieno)
            if last_states is not None:
                state.peer_villages = {
                    name: res
                    for name, res in last_states.items()
                    if name != state.village_name
                }

            logger.info(
                "%s Risorse: L:%d A:%d F:%d G:%d | Coda:%d | Attacchi:%d",
                vprefix,
                state.resources.wood,
                state.resources.clay,
                state.resources.iron,
                state.resources.crop,
                len(state.build_queue),
                state.incoming_attacks,
            )

            # 3.5 Modulo difesa: reazione agli attacchi in arrivo
            #     (priorita' sulle azioni ordinarie del brain)
            if defense_mgr is not None and settings.defense.enabled:
                rally_def = find_rally_point(state.interior_fields())
                market_def = find_market(state.interior_fields())
                try:
                    await defense_mgr.handle_village(
                        browser=browser,
                        observer=observer,
                        executor=executor,
                        settings=settings,
                        village=village,
                        state=state,
                        villages=villages,
                        rally_slot=rally_def.slot_id if rally_def else 0,
                        market_slot=market_def.slot_id if market_def else 0,
                        prefix=vprefix,
                    )
                except Exception as d_exc:
                    logger.warning(
                        "%s Errore modulo difesa (non bloccante): %s",
                        vprefix, d_exc,
                    )
                _flush_module_audits(
                    settings,
                    module="defense",
                    state=state,
                    village=village,
                    managers=(defense_mgr,),
                )

            # 3.52 Movimenti truppe (telemetria radar): in arrivo e in
            #     partenza dalla Piazza d'Armi + box '#movements' dorf1
            #     (mercanti e villaggi senza rally). Con la difesa
            #     attiva la pagina rally e' gia' stata caricata da
            #     get_incoming_attacks: nessuna navigazione extra.
            rally_mv = find_rally_point(state.interior_fields())
            try:
                inc, outg = await observer.get_troop_movements(
                    rally_mv.slot_id if rally_mv is not None else 0,
                    village.village_id,
                )
                state.incoming_moves = inc
                state.outgoing_moves = outg
                hostile = [a for a in inc if a.is_threat]
                if hostile:
                    state.incoming_attacks = max(
                        state.incoming_attacks, len(hostile)
                    )
                # Sync live nel RadarStore: attacchi/raid/rinforzi e
                # mercanti osservati (incluse le spedizioni manuali)
                # compaiono sui vettori anche senza azione del bot.
                _parse_ok = (
                    getattr(observer, "_movements_rally_ok", False)
                    or getattr(observer, "_movements_dorf1_ok", False)
                )
                # Riconciliazione live (rimozione vettori fantasma):
                # solo quando la scansione del Punto di Raduno ha
                # esito positivo — il rally overview elenca TUTTI i
                # movimenti militari, quelli assenti sono conclusi.
                _rally_ok = bool(
                    getattr(observer, "_movements_rally_ok", False)
                )
                if _parse_ok:
                    _sync_radar_movements(
                        settings,
                        vname,
                        village.x,
                        village.y,
                        inc,
                        outg,
                        prefix=vprefix,
                        reconcile=_rally_ok,
                    )
            except Exception as mv_exc:
                logger.debug(
                    "%s Lettura movimenti truppe fallita "
                    "(non bloccante): %s",
                    vprefix, mv_exc,
                )

            # 3.55 Smart Reinforce Coordinator: il villaggio corrente,
            #     se non minacciato, valuta il registro attacchi e manda
            #     un muro di rinforzo ai villaggi alleati sotto attacco.
            if (
                defense_mgr is not None
                and settings.defense.enabled
                and settings.defense.reinforce_coordination
            ):
                _pl = _module_pause_left(
                    settings,
                    str(village.village_id or village.name or "0"),
                    "defense",
                    "REINFORCE_SEND",
                    resources=state.resources,
                )
                if _pl > 0:
                    _record_module_paused(
                        settings,
                        state=state,
                        village=village,
                        module="defense",
                        decision="REINFORCE_SEND",
                        remaining=_pl,
                    )
                    rally_co = None
                else:
                    rally_co = find_rally_point(state.interior_fields())
                market_co = find_market(state.interior_fields())
                try:
                    if rally_co is not None:
                        await _coordinate_inter_village_defense(
                            executor=executor,
                            observer=observer,
                            settings=settings,
                            village=village,
                            state=state,
                            villages=villages,
                            last_states=last_states,
                            defense_mgr=defense_mgr,
                            replenish_mgr=replenish_mgr,
                            rally_slot=rally_co.slot_id if rally_co else 0,
                            market_slot=market_co.slot_id if market_co else 0,
                            prefix=vprefix,
                            now=time.time(),
                        )
                except Exception as rc_exc:
                    logger.warning(
                        "%s Errore coordinatore rinforzi "
                        "(non bloccante): %s",
                        vprefix, rc_exc,
                    )

            # 3.6 Reclaim & Chiefing: wave pianificate il cui depart_ts
            #     e' scaduto partono dal Punto di Raduno di QUESTO
            #     villaggio (tipo movimento Attacco).
            if war_store is not None:
                rally_rc = find_rally_point(state.interior_fields())
                if rally_rc:
                    try:
                        await _dispatch_reclaim_waves(
                            executor=executor,
                            war_store=war_store,
                            village=village,
                            rally_slot=rally_rc.slot_id,
                            prefix=vprefix,
                        )
                    except Exception as rw_exc:
                        logger.warning(
                            "%s Errore dispatch wave reclaim "
                            "(non bloccante): %s",
                            vprefix, rw_exc,
                        )

            # 4. Il brain decide e l'esecutore agisce (con validazione newdid)
            if last_states is not None and state.village_name:
                last_states[state.village_name] = state.resources
            states_by_village[vname] = state
            troops_by_village[vname] = state.troops_home

            # Culture Points Booster: inietta l'ultimo snapshot PC letto
            # dal modulo espansione (pagina Residenza) — il gate della
            # regola culture_boost vive su questi campi.
            if expansion_mgr is not None and expansion_mgr.cp_required > 0:
                state.culture_points = expansion_mgr.cp_current
                state.culture_points_required = expansion_mgr.cp_required
                state.culture_points_daily = expansion_mgr.cp_daily

            # Routing risorse rigido: il founder in deficit coloni ha
            # priorita' anche sull'overflow dei magazzini pieni (letto
            # da _best_peer_for_surplus in brain.rule_emergency_overflow).
            if expansion_mgr is not None:
                state.feed_target = str(expansion_mgr.feed_target or "")

            # Anti-Raid (Depositi Segreti multipli): pressione raid del
            # villaggio corrente -> attiva la regola Nascondigli del
            # builder (e la preview di plan_upgrades).
            state.single_village = len(villages) <= 1
            state.under_raid_pressure = _raid_pressure_on_village(
                village=village,
                state=state,
                villages=villages,
                defense_mgr=defense_mgr,
            )

            action = brain.decide(state)

            # Toggle dashboard "Builder": disattivato -> nessuna nuova
            # costruzione (le code in corso proseguono comunque).
            try:
                if (
                    not getattr(settings, "builder_enabled", True)
                    and action.type == ActionType.UPGRADE_FIELD
                ):
                    action = Action(
                        type=ActionType.WAIT,
                        params=dict(action.params or {}),
                        reason="Builder disabilitato dalla dashboard",
                    )
            except Exception:
                pass

            # Corsa al 15c — Congelamento spese non-espansione (spec):
            # sul founder con coloni < 3 e CP pronti (o Reggia >= 8)
            # solo Palazzo/Residenza, Magazzino, Granaio e Palazzo
            # Pubblico possono consumare risorse; truppe e altri
            # edifici vengono declassati a WAIT.
            try:
                if expansion_freeze(state, settings):
                    _frozen = _freeze_demote(state, action)
                    if _frozen is not None:
                        logger.debug(
                            "%s %s", vprefix, _frozen.reason
                        )
                        action = _frozen
            except Exception:
                logger.debug("%s Freeze espansione ignorato", vprefix)

            # Smart Building planner: persiste i prossimi upgrade stimati
            # per la preview in dashboard (state/build_plan_<account>.json).
            try:
                plan = plan_upgrades(state, settings)
                save_build_plan(
                    settings.account_id, state.village_name or vname, plan
                )
            except Exception as plan_exc:
                logger.debug(
                    "%s Build plan non salvato: %s", vprefix, plan_exc
                )

            # Decision Audit — Coda Prioritaria: motivi degli step
            # bloccati in testa (mutua esclusione, prerequisiti, coda
            # piena) e scavalcamento da regole di emergenza.
            try:
                for why in focus_skip_reasons(state, settings):
                    _record_decision(
                        settings,
                        state=state,
                        village=village,
                        module="builder",
                        decision="PRIORITY_QUEUE_STEP",
                        outcome=DECISION_OUTCOME_SKIPPED,
                        reason=why,
                    )
                winning_rule = str(
                    (action.params or {}).get("rule") or ""
                )
                if winning_rule not in ("", "rule_building_focus"):
                    if focus_intents(state, settings):
                        _record_decision(
                            settings,
                            state=state,
                            village=village,
                            module="builder",
                            decision="PRIORITY_QUEUE_STEP",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                "Step Coda Prioritaria applicabile ma "
                                f"scavalcato dalla regola '{winning_rule}'"
                            ),
                            context={"rule": winning_rule},
                        )
            except Exception:
                logger.debug(
                    "%s Audit coda prioritaria fallito",
                    vprefix,
                    exc_info=True,
                )

            # Arricchimento coordinate target per invio risorse al mercato
            if action.type == ActionType.SEND_RESOURCES:
                act_params = action.params or {}
                t_name = str(act_params.get("target_village") or act_params.get("target") or "").strip().lower()
                if t_name and (act_params.get("target_x") is None or act_params.get("target_y") is None):
                    coords = village_coords_map.get(t_name)
                    if coords:
                        action.params["target_x"], action.params["target_y"] = coords
                    else:
                        # Fallback su VillageConfig
                        t_cfg = settings.village_config_for(t_name)
                        if t_cfg and t_cfg.x is not None and t_cfg.y is not None:
                            action.params["target_x"], action.params["target_y"] = t_cfg.x, t_cfg.y

            target_vid = state.village_id or village.village_id
            # La regola vincente serve all'audit, non all'executor.
            rule_name = str(
                (action.params or {}).pop("rule", "") or ""
            )
            # Anti-detection: un modulo in pausa non riprova l'azione
            # al ciclo successivo (es. coda piena, deficit risorse).
            _act_mod = _action_module(action)
            _pl = _module_pause_left(
                settings,
                str(village.village_id or village.name or "0"),
                _act_mod,
                action.type.value.upper(),
                resources=state.resources,
            )
            if _pl > 0:
                _record_module_paused(
                    settings,
                    state=state,
                    village=village,
                    module=_act_mod,
                    decision=action.type.value.upper(),
                    remaining=_pl,
                )
                result = ActionResult(
                    True,
                    f"modulo '{_act_mod}' in pausa anti-detection",
                )
                _append_history(settings.account_id, state, action)
            else:
                # Fallback opportunistico (spec 2): stock + produzione
                # per la valutazione pagabilita'/ETA dei candidati coda.
                if action.type == ActionType.UPGRADE_FIELD:
                    _p = dict(action.params or {})
                    _p["stock"] = {
                        "wood": int(state.resources.wood or 0),
                        "clay": int(state.resources.clay or 0),
                        "iron": int(state.resources.iron or 0),
                        "crop": int(state.resources.crop or 0),
                    }
                    _p["prod_h"] = {
                        str(getattr(k, "value", k)): int(v or 0)
                        for k, v in (
                            state.resources.production_per_hour or {}
                        ).items()
                    }
                    action.params = _p
                result = await executor.execute(action, village_id=target_vid)
                _append_history(settings.account_id, state, action)
                _record_decision(
                    settings,
                    state=state,
                    village=village,
                    module=_action_module(action),
                    decision=action.type.value.upper(),
                    outcome=(
                        DECISION_OUTCOME_EXECUTED
                        if result.success
                        else DECISION_OUTCOME_BLOCKED
                    ),
                    reason=action.reason or result.message or "",
                    context={
                        "rule": rule_name,
                        "executor_message": result.message,
                    },
                )
                # Resource-ETA Sleep: upgrade fallito per deficit risorse
                # -> cooldown builder pari al tempo di produzione reale
                # del deficit (niente polling a vuoto ogni ciclo).
                if (
                    not result.success
                    and action.type == ActionType.UPGRADE_FIELD
                    and (result.data or {}).get("cost")
                ):
                    _cost = result.data["cost"]
                    _deficit = {
                        k: max(
                            0,
                            int(v or 0)
                            - int(getattr(state.resources, k, 0) or 0),
                        )
                        for k, v in _cost.items()
                    }
                    _prod = {
                        str(getattr(k, "value", k)): int(v or 0)
                        for k, v in (
                            state.resources.production_per_hour or {}
                        ).items()
                    }
                    _eta = resource_eta_seconds(_deficit, _prod)
                    if _eta and _eta > 0:
                        _lo, _hi = eta_pause_bounds(_eta)
                        _module_pauses.register(
                            settings.account_id,
                            str(
                                village.village_id
                                or village.name
                                or "0"
                            ),
                            "builder",
                            action.type.value.upper(),
                            resources=state.resources,
                            min_seconds=_lo,
                            max_seconds=_hi,
                        )
                        logger.info(
                            "%s Builder in pausa ETA %.0fmin: deficit %s",
                            vprefix, _eta / 60.0, _deficit,
                        )
            if not result.success:
                logger.warning(
                    "%s Azione non eseguita: %s", vprefix, result.message
                )

                # Circuit-breaker Coda Prioritaria (spec anti-starvation):
                # fallimenti strutturali dell'esecutore (BTN_NOT_FOUND,
                # QUEUE_UNCONFIRMED, INVALID_SLOT_OCCUPIED,
                # PREREQUISITES_MISSING) conteggiati per slot — dopo >=2
                # esiti identici lo step viene marcato BLOCKED e la coda
                # lo salta con motivo esplicito in dashboard.
                if action.type == ActionType.UPGRADE_FIELD:
                    try:
                        _fd = result.data or {}
                        if _fd.get("error") and _fd.get("slot_id"):
                            record_upgrade_fail(
                                str(
                                    village.village_id
                                    or village.name
                                    or "0"
                                ),
                                int(_fd["slot_id"]),
                                str(_fd["error"]),
                                state=state,
                                cost=_fd.get("cost"),
                            )
                    except Exception:
                        logger.debug(
                            "%s record_upgrade_fail ignorato",
                            vprefix,
                            exc_info=True,
                        )

                # Emergency Unpack (spec 3.2): spesa prioritaria fallita per
                # risorse mancanti -> il baule dell'eroe copre il delta. La
                # coda piena non e' un deficit di risorse: niente unpack.
                queue_ok = (
                    action.type == ActionType.TRAIN_SETTLERS
                    or len(state.build_queue) < max(1, settings.max_build_queue)
                )
                if (
                    hero_mgr is not None
                    and settings.hero.enabled
                    and settings.hero.emergency_unpack
                    and queue_ok
                    and action.type in (
                        ActionType.UPGRADE_FIELD,
                        ActionType.TRAIN_SETTLERS,
                    )
                ):
                    try:
                        gained = await hero_mgr.emergency_unpack(
                            browser, village_id=target_vid, prefix=vprefix
                        )
                        if gained:
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="hero",
                                decision="EMERGENCY_UNPACK",
                                outcome=DECISION_OUTCOME_EXECUTED,
                                reason=(
                                    "Unpack baule eroe per coprire il "
                                    "deficit risorse della spesa "
                                    "prioritaria"
                                ),
                                context={
                                    "gained": gained,
                                    "action": action.type.value,
                                },
                            )
                            result = await executor.execute(
                                action, village_id=target_vid
                            )
                            if not result.success:
                                logger.warning(
                                    "%s Azione non eseguita dopo unpack: %s",
                                    vprefix, result.message,
                                )
                    except Exception as unpack_exc:
                        logger.warning(
                            "%s Errore emergency unpack eroe: %s",
                            vprefix, unpack_exc,
                        )

            # Doppia coda Romani/Plus: l'upgrade appena avviato occupa
            # una sola vista (dorf1 campi o dorf2 edifici). Simuliamo la
            # voce in coda e facciamo decidere di nuovo il brain: se
            # l'altra vista e' libera avvia subito anche quella nello
            # stesso ciclo, senza attendere il giro successivo.
            if result.success and action.type == ActionType.UPGRADE_FIELD:
                _done_slot = int(
                    (result.data or {}).get("slot_id") or 0
                )
                if _done_slot <= 0:
                    # Fallback: il primo candidato e' il preferito del brain.
                    for _c in (action.params or {}).get("candidates") or []:
                        try:
                            _done_slot = int(_c)
                            break
                        except (TypeError, ValueError):
                            continue
                if _done_slot > 0 and (result.data or {}).get(
                    "queue_confirmed"
                ):
                    # Circuit-breaker Coda Prioritaria: registra il livello
                    # dello slot REALMENTE eseguito E confermato in coda;
                    # se lo step non progredisce dopo 2 EXECUTED viene
                    # marcato STUCK. Skip per coda piena (UPGRADE_BLOCKED)
                    # o click non confermato non alimentano il contatore.
                    try:
                        _lvl = next(
                            (
                                int(f.level)
                                for f in state.fields
                                if f.slot_id == _done_slot
                            ),
                            0,
                        )
                        record_upgrade_exec(
                            str(
                                village.village_id or village.name or "0"
                            ),
                            _done_slot,
                            _lvl,
                        )
                    except Exception:
                        pass
                    state.build_queue.append(
                        BuildQueueItem(
                            name="(upgrade avviato)",
                            slot_id=_done_slot,
                            is_resource_field=_done_slot <= 18,
                        )
                    )
                    # Upgrade dorf1 avviato: il livello dello slot nello
                    # stato noto sale subito, cosi' la cache resta
                    # coerente anche se il prossimo ciclo non rilegge
                    # dorf1 (gli oggetti Field sono condivisi con
                    # state.fields).
                    if _done_slot <= 18:
                        for _cf in _last_known_fields.get(_fkey, []):
                            if _cf.slot_id == _done_slot:
                                _cf.level += 1
                                break
                _can_out, _can_in = queue_slots_free(state, settings)
                if _can_out or _can_in:
                    action2 = brain.decide(state)
                    rule2 = str(
                        (action2.params or {}).pop("rule", "") or ""
                    )
                    try:
                        if expansion_freeze(state, settings):
                            _frz = _freeze_demote(state, action2)
                            if _frz is not None:
                                action2 = _frz
                    except Exception:
                        pass
                    if action2.type == ActionType.UPGRADE_FIELD:
                        _pl2 = _module_pause_left(
                            settings,
                            str(
                                village.village_id or village.name or "0"
                            ),
                            "builder",
                            action2.type.value.upper(),
                            resources=state.resources,
                        )
                        if _pl2 > 0:
                            _record_module_paused(
                                settings,
                                state=state,
                                village=village,
                                module="builder",
                                decision=action2.type.value.upper(),
                                remaining=_pl2,
                            )
                        else:
                            _p2 = dict(action2.params or {})
                            _p2["stock"] = {
                                "wood": int(state.resources.wood or 0),
                                "clay": int(state.resources.clay or 0),
                                "iron": int(state.resources.iron or 0),
                                "crop": int(state.resources.crop or 0),
                            }
                            _p2["prod_h"] = {
                                str(getattr(k, "value", k)): int(v or 0)
                                for k, v in (
                                    state.resources.production_per_hour
                                    or {}
                                ).items()
                            }
                            action2.params = _p2
                            result2 = await executor.execute(
                                action2, village_id=target_vid
                            )
                            _append_history(
                                settings.account_id, state, action2
                            )
                            _record_decision(
                                settings,
                                state=state,
                                village=village,
                                module="builder",
                                decision=action2.type.value.upper(),
                                outcome=(
                                    DECISION_OUTCOME_EXECUTED
                                    if result2.success
                                    else DECISION_OUTCOME_BLOCKED
                                ),
                                reason=(
                                    "[coda parallela] "
                                    + (action2.reason or result2.message or "")
                                ),
                                context={
                                    "rule": rule2,
                                    "executor_message": result2.message,
                                    "parallel_second_slot": True,
                                },
                            )
                            if result2.success:
                                logger.info(
                                    "%s Doppia coda: avviato anche il "
                                    "secondo slot parallelo.",
                                    vprefix,
                                )

            # ------------------------------------------------------------------
            # Filtro ruoli e competenza territoriale per il Farming / Raid:
            # - Fondatori e Feeder non eseguono raid.
            # - Ogni villaggio raida SOLO i target di cui e' il piu' vicino
            #   (nearest_village) e che non siano stati gia' attaccati di recente.
            # ------------------------------------------------------------------
            is_raiding_role = village.role not in ("early_cropper_founder", "feeder")
            if not is_raiding_role:
                _record_decision(
                    settings,
                    state=state, village=village,
                    module="farming",
                    decision="RAID_SEND",
                    outcome=DECISION_OUTCOME_SKIPPED,
                    reason=(
                        f"Villaggio escluso dal ciclo raid: ruolo "
                        f"'{village.role}' non offensivo"
                    ),
                    context={"role": village.role},
                )

            # 4.1 Modulo farming: raid sui Custom Farm Targets approvati
            # (fallback: farm list nativa del Punto di Raduno), se scaduto.
            # Il gate enabled (globale + per-villaggio) precede il check di
            # pausa anti-detection: un modulo disabilitato esce subito e non
            # produce ne' SKIPPED ne' MODULE_PAUSED nel log.
            _vcfg_farm = settings.village_config_for(
                village.name, village.village_id
            )
            farming_enabled = bool(
                getattr(settings.farming, "enabled", True)
                and (_vcfg_farm is None or _vcfg_farm.farm_enabled)
            )
            if farming_mgr is not None and is_raiding_role and farming_enabled:
                _pl = _module_pause_left(
                    settings,
                    str(village.village_id or village.name or "0"),
                    "farming",
                    resources=state.resources,
                )
                if _pl > 0:
                    _record_module_paused(
                        settings,
                        state=state,
                        village=village,
                        module="farming",
                        decision="RAID_SEND",
                        remaining=_pl,
                    )
                rally = find_rally_point(state.interior_fields())
                try:
                    if _pl <= 0:
                        await farming_mgr.run_for_village(
                            browser=browser,
                            observer=observer,
                            executor=executor,
                            settings=settings,
                            village=village,
                            rally_slot=rally.slot_id if rally else 0,
                            prefix=vprefix,
                            farm_store=farm_store,
                        )
                except Exception as f_exc:
                    logger.warning("%s Errore modulo farming: %s", vprefix, f_exc)
                _flush_module_audits(
                    settings,
                    module="farming",
                    state=state,
                    village=village,
                    managers=(farming_mgr,),
                )

            # Report di combattimento letti dallo scout brain; valorizzati una
            # sola volta per villaggio e riusati dal modulo replenish.
            reports: list[dict] | None = None

            # 4.1.b Modulo AI Scout Brain: raid dinamico a coordinate libere basato su radar mappa e report
            if scout_brain is not None and settings.scout_brain.enabled and is_raiding_role:
                _pl = _module_pause_left(
                    settings,
                    str(village.village_id or village.name or "0"),
                    "scout",
                    resources=state.resources,
                )
                if _pl > 0:
                    _record_module_paused(
                        settings,
                        state=state,
                        village=village,
                        module="scout",
                        decision="TARGET_FILTER",
                        remaining=_pl,
                    )
                rally = find_rally_point(state.interior_fields())
                if rally is not None and _pl <= 0:
                    try:
                        troops = await observer.read_troops_home(rally.slot_id, village.village_id)
                        if troops:
                            state.troops_home = troops
                            troops_by_village[vname] = troops

                        scanned = await observer.scan_map_area(
                            village.x or 0, village.y or 0, radius=settings.scout_brain.scan_radius
                        )
                        own_coords = {
                            (v.x, v.y) for v in villages
                            if v.x is not None and v.y is not None
                        }
                        if own_coords:
                            scout_brain.purge_coords(own_coords)
                            farm_store.purge_coords(own_coords)
                        if scanned:
                            scout_brain.update_targets_from_map(
                                scanned,
                                village.x or 0,
                                village.y or 0,
                                own_coords=own_coords,
                            )
                            farm_store.register_scanned(
                                scanned,
                                origin_x=village.x or 0,
                                origin_y=village.y or 0,
                                nearest_village=village.name or "",
                                skip_names={v.name for v in villages if v.name},
                                skip_coords=own_coords,
                            )

                        reports = await observer.read_combat_reports(limit=10)
                        if reports:
                            scout_brain.process_combat_reports(
                                reports, blacklist_hours=settings.scout_brain.blacklist_hours
                            )

                        # Costruisce l'insieme delle coordinate di competenza di QUESTO villaggio
                        # e fuori dal cooldown anti-doppio-invio.
                        now_ts = time.time()
                        min_revisit_sec = max(
                            1200.0,
                            float(getattr(settings.farming, "interval_min", 25) or 25) * 60.0,
                        )
                        vname_norm = (village.name or "").strip().lower()
                        eligible_coords: set[tuple[int, int]] = set()

                        for t_obj in farm_store.get_approved_targets():
                            tx, ty = int(t_obj.x), int(t_obj.y)

                            # 1. Competenza territoriale (nearest_village, con fallback su distanza minima)
                            nearest = (getattr(t_obj, "nearest_village", "") or "").strip().lower()
                            if not nearest and village_coords_map:
                                nearest = min(
                                    village_coords_map.keys(),
                                    key=lambda vn: (
                                        (village_coords_map[vn][0] - tx) ** 2
                                        + (village_coords_map[vn][1] - ty) ** 2
                                    ),
                                )
                            if nearest and nearest != vname_norm:
                                continue

                            # 2. Cooldown per-coordinata (evita invii ravvicinati allo stesso target)
                            last_sent = float(
                                getattr(t_obj, "last_raid_ts", 0.0)
                                or getattr(t_obj, "last_attempt_ts", 0.0)
                                or 0.0
                            )
                            if last_sent > 0 and (now_ts - last_sent) < min_revisit_sec:
                                continue

                            eligible_coords.add((tx, ty))

                        if not eligible_coords:
                            logger.info(
                                "%s AI Scout: nessun target approvato di competenza o fuori cooldown.",
                                vprefix,
                            )
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="scout",
                                decision="TARGET_FILTER",
                                outcome=DECISION_OUTCOME_SKIPPED,
                                reason=(
                                    "AI Scout: nessun target approvato "
                                    "di competenza o fuori cooldown "
                                    "per-coordinata"
                                ),
                            )
                        else:
                            plan = await scout_brain.decide_raid_plan(
                                village, state.troops_home, settings
                            )
                            if plan:
                                gated, skipped = [], []
                                for t in plan:
                                    tx = int(t.get("x") or 0)
                                    ty = int(t.get("y") or 0)
                                    if (tx, ty) in eligible_coords:
                                        gated.append(t)
                                    else:
                                        skipped.append(
                                            f"({tx}|{ty}) non di competenza / in cooldown"
                                        )
                                if skipped:
                                    logger.info(
                                        "%s AI Scout: %d target saltati: %s",
                                        vprefix, len(skipped), "; ".join(skipped),
                                    )
                                    _record_decision(
                                        settings,
                                        state=state, village=village,
                                        module="scout",
                                        decision="TARGET_FILTER",
                                        outcome=DECISION_OUTCOME_SKIPPED,
                                        reason=(
                                            f"AI Scout: {len(skipped)} "
                                            "target scartati "
                                            "(fuori competenza "
                                            "nearest_village o in "
                                            "cooldown)"
                                        ),
                                        context={
                                            "skipped": skipped,
                                            "approved": len(gated),
                                        },
                                    )
                                plan = gated

                            if not plan:
                                _record_decision(
                                    settings,
                                    state=state, village=village,
                                    module="scout",
                                    decision="RAID_SEND",
                                    outcome=DECISION_OUTCOME_SKIPPED,
                                    reason=(
                                        "AI Scout: nessun raid "
                                        "pianificato dopo i filtri "
                                        "(target coperti, in blacklist "
                                        "o senza truppe)"
                                    ),
                                )
                            if plan:
                                scout_act = Action(
                                    type=ActionType.SEND_CUSTOM_RAID,
                                    params={"rally_slot": rally.slot_id, "targets": plan},
                                    reason=f"AI Scout Brain: invio {len(plan)} raid dinamici mirati",
                                )
                                scout_res = await executor.execute(scout_act, village_id=target_vid)
                                for t in plan:
                                    farm_store.record_raid_attempt(
                                        int(t.get("x") or 0),
                                        int(t.get("y") or 0),
                                        scout_res.success,
                                        scout_res.message,
                                    )
                                logger.info("%s AI Scout: %s", vprefix, scout_res.message)
                                _record_decision(
                                    settings,
                                    state=state, village=village,
                                    module="scout",
                                    decision="RAID_SEND",
                                    outcome=(
                                        DECISION_OUTCOME_EXECUTED
                                        if scout_res.success
                                        else DECISION_OUTCOME_BLOCKED
                                    ),
                                    reason=(
                                        f"AI Scout: {len(plan)} raid "
                                        "dinamici mirati — "
                                        f"{scout_res.message}"
                                    ),
                                    context={
                                        "coords": [
                                            [t.get("x"), t.get("y")]
                                            for t in plan
                                        ],
                                        "units": [
                                            t.get("troops")
                                            for t in plan
                                        ],
                                    },
                                )
                    except Exception as s_exc:
                        logger.warning("%s Errore modulo AI Scout Brain: %s", vprefix, s_exc)

            # 4.1.b2 Custom Farm Targets: esiti raid dai report ->
            # aggiornamento loot ratio e auto-blacklist anti-suicidio.
            # Riutilizza `reports` letto dallo scout; se lo scout e'
            # spento lo legge qui (una sola volta per villaggio).
            if settings.farming.enabled:
                try:
                    if reports is None:
                        reports = await observer.read_combat_reports(limit=10)
                    if reports:
                        farm_store.process_combat_reports(reports)
                except Exception as fs_exc:
                    logger.warning(
                        "%s Errore farm target store: %s", vprefix, fs_exc
                    )

            # 4.1.c Smart Troop Replenishment: analisi post-battaglia e
            # rimpiazzo condizionato delle perdite (target - attuali - coda)
            # Lock temporaneo (spec 4.1): durante l'emergenza difesa il
            # rimpiazzo e' congelato per non spendere le risorse svuotate.
            emergency_locked = (
                defense_mgr is not None
                and defense_mgr.is_emergency_locked(
                    village.village_id or state.village_id
                )
            )

            # 4.1.b3 Accademia (gid 22) — Auto-Ricerca Truppe: un'unita'
            # autorizzata in allowed_units ma non ancora sbloccata viene
            # ricercata automaticamente prima di ogni addestramento, se
            # prerequisiti e risorse sono soddisfatti. Rispetta la Grain
            # Safety Net, la riserva grano configurata e il freeze
            # espansione; throttled e pausato come gli altri moduli.
            try:
                vcfg_ac = settings.village_config_for(
                    village.name, target_vid
                )
                academy = next(
                    (
                        f for f in state.interior_fields()
                        if f.gid == GID_ACADEMY and f.level > 0
                    ),
                    None,
                )
                wanted_units: list[str] = []
                for _u in (
                    (vcfg_ac.allowed_units or vcfg_ac.priority_units)
                    if vcfg_ac is not None
                    else []
                ):
                    _k = unit_label_to_key(_u) or resolve_troop_slot(
                        str(_u), str(settings.tribe or "")
                    )
                    if _k and _k not in wanted_units:
                        wanted_units.append(_k)
                last_ac = _last_academy_try.get(vname, 0.0)
                if (
                    wanted_units
                    and academy is not None
                    and not emergency_locked
                    and (time.time() - last_ac) >= ACADEMY_RETRY_SECONDS
                    and _module_pause_left(
                        settings,
                        str(village.village_id or village.name or "0"),
                        "academy",
                        resources=state.resources,
                    )
                    <= 0
                ):
                    _last_academy_try[vname] = time.time()
                    ac_state = await observer.read_academy_state(
                        academy.slot_id, village.village_id
                    )
                    # Rettifiche slot->gid: il sensore ha verificato che
                    # la pagina aperta fosse davvero gid 22; se la mappa
                    # era errata (es. data-aid ambiguo su UI moderne) le
                    # correzioni vengono applicate subito cosi' i planner
                    # downstream vedono i gid reali.
                    _corr = (ac_state or {}).get("slot_corrections") or {}
                    _resolved = int((ac_state or {}).get("resolved_slot") or 0)
                    if _corr:
                        # Persistenza per (village_id, slot_id): le
                        # rettifiche sopravvivono al ciclo e al riavvio —
                        # al prossimo giro _apply_slot_corrections le
                        # applica gia' in fase di lettura del dorf2.
                        _persist_slot_corrections(
                            settings, village, state.fields, _corr,
                            resolved_slot=_resolved,
                        )
                        for _f in state.fields:
                            _g = _corr.get(_f.slot_id)
                            if _g is not None and _f.gid != int(_g):
                                _f.gid = int(_g)
                        _imap = interior_map.get(
                            village.name or str(village.village_id)
                        ) or []
                        for _f in _imap:
                            _g = _corr.get(_f.slot_id)
                            if _g is not None and _f.gid != int(_g):
                                _f.gid = int(_g)
                    if _resolved and _resolved != academy.slot_id:
                        academy = Field(
                            slot_id=_resolved,
                            name=academy.name,
                            level=academy.level,
                            gid=GID_ACADEMY,
                        )
                    if not ac_state:
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="academy",
                            decision="UNIT_RESEARCH",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                "Accademia: stato ricerche non "
                                "leggibile dalla pagina"
                            ),
                        )
                    elif "units" not in ac_state:
                        # Solo rettifiche (o snapshot non parsabile):
                        # nessuno slot gid22 verificato o pagina
                        # illeggibile. Le correzioni sono gia' persistite:
                        # dal ciclo successivo la mappa e' corretta e il
                        # modulo non riprova a vuoto.
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="academy",
                            decision="UNIT_RESEARCH",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                "Accademia non accessibile: "
                                + (
                                    f"slot {_resolved} aperto ma stato "
                                    "ricerche non leggibile"
                                    if _resolved
                                    else "nessuno slot gid22 verificato "
                                         "dopo correzione mappa"
                                )
                            ),
                        )
                    elif ac_state.get("research_active"):
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="academy",
                            decision="UNIT_RESEARCH",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                "Accademia: ricerca gia' in corso "
                                f"({', '.join(ac_state.get('researching') or [])})"
                            ),
                        )
                    else:
                        # Tie-breaker 'gia' sbloccata': le unita' assenti
                        # dal DOM Accademia ma soggette a ricerca si
                        # verificano sugli input tN del trainer.
                        _absent = {
                            k
                            for k in wanted_units
                            if k in UNIT_ACADEMY_LEVELS
                            and k not in (ac_state.get("units") or {})
                        }
                        trainable: set[str] = set()
                        if _absent:
                            for _f in state.interior_fields():
                                _gid = int(_f.gid or 0)
                                if _gid in (
                                    GID_BARRACKS,
                                    GID_STABLE,
                                    GID_WORKSHOP,
                                    GID_RESIDENCE,
                                    GID_PALACE,
                                ) and any(
                                    trainer_gid_for_unit(k)
                                    in (_gid, GID_RESIDENCE)
                                    for k in _absent
                                ):
                                    trainable |= (
                                        await observer.read_trainable_units(
                                            _f.slot_id, village.village_id
                                        )
                                    )
                        pending = _academy_pending_units(
                            wanted_units,
                            ac_state,
                            int(academy.level or 0),
                            trainable=trainable,
                        )
                        pick = next(
                            (p for p in pending if p.get("actionable")),
                            None,
                        )
                        if pick is None:
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="academy",
                                decision="UNIT_RESEARCH",
                                outcome=DECISION_OUTCOME_SKIPPED,
                                reason=(
                                    pending[0]["reason"]
                                    if pending
                                    else "Tutte le unita' autorizzate "
                                         "sono gia' ricercate"
                                ),
                                context={
                                    "pending": [
                                        p.get("unit") for p in pending
                                    ],
                                },
                            )
                        elif expansion_freeze(state, settings):
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="academy",
                                decision="UNIT_RESEARCH",
                                outcome=DECISION_OUTCOME_BLOCKED,
                                reason=(
                                    "Freeze espansione: ricerca "
                                    f"{pick['label']} sospesa finche' "
                                    "i 3 coloni non sono pronti"
                                ),
                            )
                        else:
                            # Guard risorse: il costo letto non deve
                            # superare lo stock; il grano rispetta la
                            # riserva configurata e la soglia netta.
                            costs = {
                                str(k): int(v or 0)
                                for k, v in (
                                    pick.get("costs") or {}
                                ).items()
                            }
                            stock_ok = all(
                                int(getattr(state.resources, r, 0) or 0)
                                >= int(costs.get(r) or 0)
                                for r in ("wood", "clay", "iron", "crop")
                            )
                            net = net_crop(state)
                            thr = float(
                                crop_threshold(state, settings) or 0.0
                            )
                            granary = int(
                                state.resources.granary_capacity or 0
                            )
                            crop_reserve = int(
                                granary
                                * max(
                                    0.0,
                                    settings.replenishment.crop_reserve_pct,
                                )
                            )
                            crop_after = (
                                int(state.resources.crop or 0)
                                - int(costs.get("crop") or 0)
                            )
                            if not stock_ok:
                                _record_decision(
                                    settings,
                                    state=state, village=village,
                                    module="academy",
                                    decision="UNIT_RESEARCH",
                                    outcome=DECISION_OUTCOME_SKIPPED,
                                    reason=(
                                        f"Ricerca {pick['label']} "
                                        "rinviata: risorse "
                                        "insufficienti "
                                        f"(costo {costs})"
                                    ),
                                    context={"costs": costs},
                                )
                            elif (
                                net is not None
                                and thr > 0
                                and float(net) < thr
                            ):
                                _record_decision(
                                    settings,
                                    state=state, village=village,
                                    module="academy",
                                    decision="UNIT_RESEARCH",
                                    outcome=DECISION_OUTCOME_SKIPPED,
                                    reason=(
                                        f"Ricerca {pick['label']} "
                                        "sospesa: bilancio grano "
                                        "sotto soglia "
                                        f"({net} < {thr})"
                                    ),
                                    context={
                                        "net_crop": float(net),
                                        "threshold": float(thr),
                                    },
                                )
                            elif crop_after < crop_reserve:
                                _record_decision(
                                    settings,
                                    state=state, village=village,
                                    module="academy",
                                    decision="UNIT_RESEARCH",
                                    outcome=DECISION_OUTCOME_SKIPPED,
                                    reason=(
                                        f"Ricerca {pick['label']} "
                                        "sospesa: intaccherebbe la "
                                        "riserva grano "
                                        f"({crop_after} < {crop_reserve})"
                                    ),
                                    context={
                                        "crop_reserve": crop_reserve,
                                    },
                                )
                            else:
                                ac_res = await executor.execute(
                                    Action(
                                        type=ActionType.RESEARCH_UNIT,
                                        params={
                                            "academy_slot": academy.slot_id,
                                            "unit": pick["unit"],
                                            "label": pick["label"],
                                        },
                                        reason=(
                                            "Auto-ricerca Accademia: "
                                            f"{pick['label']} "
                                            "(unita' autorizzata "
                                            "non ancora sbloccata)"
                                        ),
                                    ),
                                    village_id=target_vid,
                                )
                                logger.info(
                                    "%s Accademia: %s",
                                    vprefix, ac_res.message,
                                )
                                _record_decision(
                                    settings,
                                    state=state, village=village,
                                    module="academy",
                                    decision="UNIT_RESEARCH",
                                    outcome=(
                                        DECISION_OUTCOME_EXECUTED
                                        if ac_res.success
                                        else DECISION_OUTCOME_BLOCKED
                                    ),
                                    reason=(
                                        f"Ricerca {pick['label']} in "
                                        f"Accademia: {ac_res.message}"
                                    ),
                                    context={
                                        "unit": pick["unit"],
                                        "academy_level": int(
                                            academy.level or 0
                                        ),
                                        "costs": costs,
                                    },
                                )
            except Exception as ac_exc:
                logger.warning(
                    "%s Errore automazione Accademia: %s",
                    vprefix, ac_exc,
                )

            if emergency_locked:
                logger.info(
                    "%s Replenishment congelato: emergenza difesa attiva.",
                    vprefix,
                )
                _record_decision(
                    settings,
                    state=state, village=village,
                    module="replenishment",
                    decision="TROOP_TRAINING",
                    outcome=DECISION_OUTCOME_BLOCKED,
                    reason=(
                        "Addestramento congelato: emergency_locked "
                        "(emergenza difesa attiva)"
                    ),
                )
            _pl = _module_pause_left(
                settings,
                str(village.village_id or village.name or "0"),
                "replenishment",
                resources=state.resources,
            )
            if (
                replenish_mgr is not None
                and settings.replenishment.enabled
                and not emergency_locked
            ):
                if _pl > 0:
                    _record_module_paused(
                        settings,
                        state=state,
                        village=village,
                        module="replenishment",
                        decision="TROOP_TRAINING",
                        remaining=_pl,
                    )
                try:
                    # Scenario 2: il villaggio sotto assedio blocca rimpiazzo
                    # e rinforzi; se la difesa e' stabile si sblocca da solo.
                    replenish_mgr.update_siege_state(
                        vname,
                        state.incoming_attacks,
                        siege_waves=settings.replenishment.siege_waves,
                    )

                    vcfg_rep = settings.village_config_for(
                        village.name, target_vid
                    )
                    if (
                        _pl <= 0
                        and vcfg_rep is not None
                        and (
                            vcfg_rep.troop_targets
                            or vcfg_rep.troop_replenishment_preset
                        )
                    ):
                        # Scenario 1: report -> blocco target suicida e
                        # congelamento unita' offensive coinvolte.
                        if reports is None:
                            reports = await observer.read_combat_reports(
                                limit=10
                            )
                        if reports:
                            raid_units: list[str] = []
                            if vcfg_rep.farm_unit:
                                raid_units.append(vcfg_rep.farm_unit)
                            raid_units.extend(vcfg_rep.priority_units or [])
                            replenish_mgr.process_combat_reports(
                                reports,
                                auto_block=settings.replenishment.auto_block,
                                fail_streak_threshold=(
                                    settings.replenishment.fail_streak_threshold
                                ),
                                raid_units=raid_units or None,
                            )

                        # Truppe a casa: riuso lettura scout o nuova lettura
                        troops_now = dict(state.troops_home or {})
                        rally_rep = find_rally_point(state.interior_fields())
                        if not troops_now and rally_rep is not None:
                            troops_now = await observer.read_troops_home(
                                rally_rep.slot_id, village.village_id
                            )
                            state.troops_home = dict(troops_now)

                        # Truppe in coda di addestramento (tutti i trainer)
                        trainer_slots_rep = [
                            f.slot_id
                            for f in state.interior_fields()
                            if f.gid in (GID_BARRACKS, GID_STABLE, GID_WORKSHOP)
                        ]
                        queued: dict[str, int] = {}
                        for _slot in trainer_slots_rep:
                            for _u, _q in (
                                await observer.read_training_queue(
                                    _slot, village.village_id
                                )
                            ).items():
                                queued[_u] = queued.get(_u, 0) + _q

                        # Smart Role Presets: il preset truppe del ruolo
                        # si fonde nei target (i target manuali hanno
                        # sempre la precedenza). 'settlers_target' resta
                        # al modulo espansione (Residenza/Palazzo).
                        rep_targets = dict(vcfg_rep.troop_targets)
                        if vcfg_rep.troop_replenishment_preset:
                            tribe = (
                                settings.tribe
                                or detect_tribe_from_keys(troops_now.keys())
                                or detect_tribe_from_labels(
                                    vcfg_rep.allowed_units
                                )
                            )
                            for _tk, _tq in resolve_troop_preset(
                                vcfg_rep.troop_replenishment_preset, tribe
                            ).items():
                                rep_targets.setdefault(_tk, _tq)

                        # Formula: da_addestrare = target - (attuali + coda)
                        deficit = replenish_mgr.compute_deficit(
                            rep_targets, troops_now, queued
                        )
                        # Anti-saturazione (spec 2.1): se il villaggio e'
                        # il fondatore designato o la rete feeder gli sta'
                        # spedendo risorse, la riserva 3x colono e' intoccabile.
                        founder_cfg = (
                            settings.expansion.founder_village or ""
                        ).strip()
                        reserve_settlers = bool(
                            founder_cfg
                            and founder_cfg in (vname, str(target_vid))
                        ) or bool(
                            expansion_mgr is not None
                            and expansion_mgr.feed_target == vname
                        )
                        (
                            plan,
                            plan_reason,
                            plan_ctx,
                        ) = replenish_mgr.plan_training_detailed(
                            deficit, state, settings, village_name=vname,
                            reserve_settlers=reserve_settlers,
                        )
                        # Freeze espansione (spec Corsa 15c): niente
                        # addestramento truppe sul founder finche' i 3
                        # coloni non sono completi.
                        if expansion_freeze(state, settings):
                            plan = {}
                            plan_reason = "expansion_freeze"
                        if plan and trainer_slots_rep:
                            rep_action = Action(
                                type=ActionType.TRAIN_TROOPS,
                                params={
                                    "trainer_slots": trainer_slots_rep,
                                    "unit_quantities": plan,
                                },
                                reason=(
                                    "Smart Replenishment: deficit "
                                    f"{plan} verso target "
                                    f"{vcfg_rep.troop_targets}"
                                ),
                            )
                            rep_res = await executor.execute(
                                rep_action, village_id=target_vid
                            )
                            logger.info(
                                "%s Replenishment: %s",
                                vprefix, rep_res.message,
                            )
                            if rep_res.success:
                                # Stesso ciclo: FEEDER_PUSH valuta il surplus
                                # piu' avanti sullo stesso state. Le risorse
                                # spese server-side vanno scalate anche qui o
                                # il push proporrebbe stock non piu' presente
                                # (invii sotto la riserva keep o BLOCKED).
                                spent = (rep_res.data or {}).get("spent") or {}
                                for _rk in ("wood", "clay", "iron", "crop"):
                                    _sv = int(spent.get(_rk) or 0)
                                    if _sv > 0:
                                        setattr(
                                            state.resources,
                                            _rk,
                                            max(
                                                0,
                                                int(
                                                    getattr(
                                                        state.resources, _rk, 0
                                                    )
                                                    or 0
                                                )
                                                - _sv,
                                            ),
                                        )
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="replenishment",
                                decision="TROOP_TRAINING",
                                outcome=(
                                    DECISION_OUTCOME_EXECUTED
                                    if rep_res.success
                                    else DECISION_OUTCOME_BLOCKED
                                ),
                                reason=(
                                    "Addestramento deficit "
                                    f"{plan}: {rep_res.message}"
                                ),
                                context={
                                    "deficit": dict(deficit),
                                    "reserve_settlers": reserve_settlers,
                                },
                            )
                        elif deficit:
                            # Diagnosi esplicita: attesa naturale per risorse
                            # (insufficient_resources / grain_net /
                            # crop_reserve) vs blocco strutturale (villaggio
                            # congelato, trainer assenti, freeze espansione).
                            if plan and not trainer_slots_rep:
                                _skip_outcome = DECISION_OUTCOME_BLOCKED
                                _skip_reason = (
                                    "Deficit truppe presente ma nessun "
                                    "edificio di addestramento disponibile "
                                    "(caserma/scuderia/officina assenti)"
                                )
                            else:
                                _skip_outcome = DECISION_OUTCOME_SKIPPED
                                _skip_reasons = {
                                    "frozen_village": (
                                        "Rimpiazzo sospeso: villaggio sotto "
                                        "assedio (la difesa preserva "
                                        "l'esercito)"
                                    ),
                                    "building_queue": (
                                        "Addestramento rimandato: coda "
                                        "costruzioni attiva (priorita' allo "
                                        "sviluppo economico)"
                                    ),
                                    "pending_upgrade": (
                                        "Addestramento sospeso: upgrade "
                                        "critico in attesa di risorse "
                                        f"({plan_ctx.get('kind', '')})"
                                    ),
                                    "grain_net": (
                                        "Addestramento sospeso: bilancio "
                                        "grano netto sotto soglia "
                                        f"({plan_ctx.get('net_crop')}/h < "
                                        f"{plan_ctx.get('threshold')}/h) — "
                                        "attesa riequilibrio"
                                    ),
                                    "crop_reserve": (
                                        "Addestramento sospeso: grano sotto "
                                        "la riserva configurata "
                                        f"({plan_ctx.get('crop')} < "
                                        f"{plan_ctx.get('crop_reserve')}) — "
                                        "attesa accumulo"
                                    ),
                                    "insufficient_resources": (
                                        "Risorse insufficienti per le unita' "
                                        "richieste al netto delle riserve "
                                        f"(manca {plan_ctx.get('missing')}"
                                        f" per {plan_ctx.get('unit')}) — "
                                        "attesa accumulo naturale"
                                    ),
                                    "expansion_freeze": (
                                        "Addestramento congelato: freeze "
                                        "espansione attivo finche' i 3 "
                                        "coloni non sono pronti"
                                    ),
                                }
                                _skip_reason = _skip_reasons.get(
                                    plan_reason,
                                    "Deficit truppe presente ma "
                                    "addestramento non avviabile "
                                    "(riserve attive o unita' non "
                                    "pianificabili)",
                                )
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="replenishment",
                                decision="TROOP_TRAINING",
                                outcome=_skip_outcome,
                                reason=_skip_reason,
                                context={
                                    "deficit": dict(deficit),
                                    "reserve_settlers": reserve_settlers,
                                    "trainer_slots": len(
                                        trainer_slots_rep
                                    ),
                                    "plan_reason": plan_reason,
                                    **{
                                        k: v
                                        for k, v in plan_ctx.items()
                                        if k in ("missing", "unit", "kind")
                                    },
                                },
                            )
                            # Attesa naturale per risorse: pausa calcolata
                            # sul tempo di produzione reale del deficit
                            # (Resource-ETA Sleep, come UPGRADE_FIELD) —
                            # niente polling a vuoto ne' circuit-breaker
                            # impropri su condizioni che si risolvono da
                            # sole quando le scorte maturano.
                            if plan_reason == "insufficient_resources":
                                _missing = plan_ctx.get("missing") or {}
                                _prod = {
                                    str(getattr(k, "value", k)): int(v or 0)
                                    for k, v in (
                                        state.resources.production_per_hour
                                        or {}
                                    ).items()
                                }
                                _eta = resource_eta_seconds(_missing, _prod)
                                if _eta and _eta > 0:
                                    _lo, _hi = eta_pause_bounds(_eta)
                                    _module_pauses.register(
                                        settings.account_id,
                                        str(
                                            village.village_id
                                            or village.name
                                            or "0"
                                        ),
                                        "replenishment",
                                        "TROOP_TRAINING",
                                        resources=state.resources,
                                        min_seconds=_lo,
                                        max_seconds=_hi,
                                    )
                                    logger.info(
                                        "%s Replenishment in pausa ETA "
                                        "%.0fmin: deficit %s",
                                        vprefix, _eta / 60.0, _missing,
                                    )
                except Exception as rep_exc:
                    logger.warning(
                        "%s Errore modulo replenishment: %s",
                        vprefix, rep_exc,
                    )

            # 4.1.d Automazione Fucina (smithy_auto_upgrade): i ruoli
            # militari accodano upgrade unita' fino al target, con
            # throttling per-villaggio e rispetto della Grain Safety Net.
            try:
                vcfg_sm = settings.village_config_for(village.name, target_vid)
                smithy_cfg = (
                    dict(vcfg_sm.smithy_auto_upgrade)
                    if vcfg_sm is not None
                    else {}
                )
                smithy = next(
                    (
                        f for f in state.interior_fields()
                        if f.gid == GID_SMITHY and f.level > 0
                    ),
                    None,
                )
                last_try = _last_smithy_try.get(vname, 0.0)
                if (
                    smithy_cfg.get("enabled")
                    and smithy is not None
                    and emergency_locked
                    and (time.time() - last_try) >= SMITHY_RETRY_SECONDS
                    and _module_pause_left(
                        settings,
                        str(village.village_id or village.name or "0"),
                        "smithy",
                        resources=state.resources,
                    )
                    <= 0
                ):
                    _last_smithy_try[vname] = time.time()
                    _record_decision(
                        settings,
                        state=state, village=village,
                        module="smithy",
                        decision="UNIT_UPGRADE",
                        outcome=DECISION_OUTCOME_BLOCKED,
                        reason=(
                            "Upgrade Fucina sospeso: emergency_locked "
                            "(emergenza difesa attiva)"
                        ),
                    )
                if (
                    smithy_cfg.get("enabled")
                    and smithy is not None
                    and not emergency_locked
                    and (time.time() - last_try) >= SMITHY_RETRY_SECONDS
                    and _module_pause_left(
                        settings,
                        str(village.village_id or village.name or "0"),
                        "smithy",
                        resources=state.resources,
                    )
                    <= 0
                ):
                    _last_smithy_try[vname] = time.time()

                    # Grain Safety Net: upgrade metallurgici solo con
                    # margine grano sopra soglia. net_crop() torna None
                    # quando il bilancio non e' stato letto (production.php
                    # non visitata): niente confronto -> salto pulito.
                    net = net_crop(state)
                    thr = float(crop_threshold(state, settings) or 0.0)
                    if net is None:
                        logger.debug(
                            "%s Fucina: bilancio grano non letto "
                            "(net_crop=None) — tentativo rinviato.",
                            vprefix,
                        )
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="smithy",
                            decision="UNIT_UPGRADE",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                "Upgrade Fucina rinviato: bilancio "
                                "grano non letto (net_crop=None)"
                            ),
                        )
                    elif thr > 0 and float(net) < thr:
                        logger.info(
                            "%s Fucina: upgrade sospeso (net crop %s < %s)",
                            vprefix, net, thr,
                        )
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="smithy",
                            decision="UNIT_UPGRADE",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                "Upgrade Fucina sospeso: bilancio "
                                "grano sotto soglia"
                            ),
                            context={
                                "net_crop": float(net),
                                "threshold": float(thr),
                            },
                        )
                    else:
                        sm_res = await executor.execute(
                            Action(
                                type=ActionType.UPGRADE_UNITS,
                                params={
                                    "smithy_slot": smithy.slot_id,
                                    "target_level": int(
                                        smithy_cfg.get("target_level") or 20
                                    ),
                                    "units": list(
                                        smithy_cfg.get("priority_units") or []
                                    ),
                                },
                                reason=(
                                    "Smithy auto-upgrade: potenziamento "
                                    "unita' fino al liv. "
                                    f"{smithy_cfg.get('target_level') or 20}"
                                ),
                            ),
                            village_id=target_vid,
                        )
                        logger.info("%s Fucina: %s", vprefix, sm_res.message)
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="smithy",
                            decision="UNIT_UPGRADE",
                            outcome=(
                                DECISION_OUTCOME_EXECUTED
                                if sm_res.success
                                else DECISION_OUTCOME_BLOCKED
                            ),
                            reason=(
                                f"Potenziamento unita' in Fucina "
                                f"(liv. {smithy_cfg.get('target_level') or 20}): "
                                f"{sm_res.message}"
                            ),
                            context={
                                "units": list(
                                    smithy_cfg.get("priority_units")
                                    or []
                                ),
                                "smithy_level": int(smithy.level),
                            },
                        )
            except Exception as sm_exc:
                logger.warning(
                    "%s Errore automazione Fucina: %s", vprefix, sm_exc,
                )

            # 4.2 Modulo espansione: i villaggi non-fondatore con mercato
            # spediscono il surplus al fondatore mentre servono coloni
            if (
                expansion_mgr is not None
                and expansion_mgr.feed_target
                and village.name != expansion_mgr.feed_target
            ):
                market = find_market(state.interior_fields())
                vcfg_feed = settings.village_config_for(village.name, target_vid)
                market_slot = (
                    int(vcfg_feed.market_slot or 0)
                    if vcfg_feed is not None and vcfg_feed.market_slot
                    else (market.slot_id if market is not None else 0)
                )
                surplus = feeder_surplus(state)
                _pl = _module_pause_left(
                    settings,
                    str(village.village_id or village.name or "0"),
                    "expansion",
                    "FEEDER_SEND",
                    resources=state.resources,
                )
                if _pl > 0 and market_slot and surplus:
                    _record_module_paused(
                        settings,
                        state=state,
                        village=village,
                        module="expansion",
                        decision="FEEDER_SEND",
                        remaining=_pl,
                    )
                if market_slot and surplus and _pl <= 0:
                    f_target = expansion_mgr.feed_target
                    # Destinatario per nome o newdid: le coordinate del
                    # payload si leggono SOLO dal suo record, mai dal
                    # villaggio mittente.
                    f_target_l = f_target.strip().lower()
                    founder_v = next(
                        (
                            v for v in villages
                            if v.name == f_target
                            or str(v.village_id) == f_target
                        ),
                        None,
                    )
                    feed_params: dict = {
                        "market_slot": market_slot,
                        "target_village": f_target,
                        "target": f_target,
                        "resources": surplus,
                    }
                    if founder_v is not None and founder_v.x is not None and founder_v.y is not None:
                        feed_params["target_x"] = founder_v.x
                        feed_params["target_y"] = founder_v.y
                    elif f_target_l in village_coords_map:
                        feed_params["target_x"], feed_params["target_y"] = village_coords_map[f_target_l]
                    else:
                        t_cfg = settings.village_config_for(f_target)
                        if t_cfg and t_cfg.x is not None and t_cfg.y is not None:
                            feed_params["target_x"], feed_params["target_y"] = t_cfg.x, t_cfg.y

                    if "target_x" not in feed_params:
                        # Coordinate del destinatario non risolte: salto
                        # l'invio invece di spedire verso un target
                        # ambiguo (es. coordinate del mittente).
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="expansion",
                            decision="FEEDER_SEND",
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=(
                                f"Invio surplus al founder '{f_target}' "
                                "saltato: coordinate del destinatario "
                                "non note."
                            ),
                            context={"target": str(f_target)},
                        )
                    else:
                        feed_action = Action(
                            type=ActionType.SEND_RESOURCES,
                            params=feed_params,
                            reason=(
                                f"Espansione: feeder -> '{expansion_mgr.feed_target}' "
                                f"invia surplus {surplus} per i coloni"
                            ),
                        )
                        feed_res = await executor.execute(
                            feed_action, village_id=target_vid
                        )
                        logger.info(
                            "%s Espansione feeder: %s -> '%s' (%s)",
                            vprefix, surplus, expansion_mgr.feed_target,
                            feed_res.message,
                        )
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="expansion",
                            decision="FEEDER_SEND",
                            outcome=(
                                DECISION_OUTCOME_EXECUTED
                                if feed_res.success
                                else DECISION_OUTCOME_BLOCKED
                            ),
                            reason=(
                                f"Surplus {surplus} spedito al founder "
                                f"'{expansion_mgr.feed_target}': "
                                f"{feed_res.message}"
                            ),
                            context={
                                "resources": dict(surplus),
                                "target": str(expansion_mgr.feed_target),
                            },
                        )
                else:
                    _record_decision(
                        settings,
                        state=state, village=village,
                        module="expansion",
                        decision="FEEDER_SEND",
                        outcome=DECISION_OUTCOME_SKIPPED,
                        reason=(
                            "Invio surplus al founder non partito: "
                            "mercato assente"
                            if not market_slot
                            else "Invio surplus al founder non "
                                 "partito: nessun surplus disponibile"
                        ),
                        context={
                            "feed_target": str(
                                expansion_mgr.feed_target
                            )
                        },
                    )

            # 4.3 Auto-Feeder Bridge (spec 2): finche' bootstrap_target
            # e' attivo, i villaggi maturi (magazzino > 50%) inviano
            # risorse al nuovo insediamento, cappate allo spazio libero
            # reale del target (peer_villages) per evitare overflow.
            # Regola rigida: se il founder e' in deficit coloni
            # (feed_target attivo) il bridge cede il passo — tutte le
            # spedizioni dei feeder vanno prima al founder.
            bt = (
                expansion_mgr.bootstrap_target
                if expansion_mgr is not None
                and not expansion_mgr.feed_target
                else {}
            )
            _pl = _module_pause_left(
                settings,
                str(village.village_id or village.name or "0"),
                "expansion",
                "BOOTSTRAP_BRIDGE",
                resources=state.resources,
            )
            if bt and _pl > 0:
                _record_module_paused(
                    settings,
                    state=state,
                    village=village,
                    module="expansion",
                    decision="BOOTSTRAP_BRIDGE",
                    remaining=_pl,
                )
                bt = {}
            if bt:
                b_id = int(bt.get("village_id") or 0)
                b_name = str(bt.get("name") or "")
                if b_id and b_id != (village.village_id or 0):
                    b_res = (
                        state.peer_villages.get(b_name)
                        or state.peer_villages.get(str(b_id))
                    )
                    # Villaggio maturo: spedisce solo sopra il 50% del
                    # magazzino (keep_ratio dedicata al bridge).
                    b_surplus = feeder_surplus(state, keep_ratio=0.50)
                    if b_surplus:
                        # Capping allo spazio libero del target: max 85%
                        # della capienza per risorsa, al netto dei
                        # mercanti gia' in viaggio (radar). Su croplog /
                        # 15c in crescita il grano resta a casa se il
                        # granaio e' quasi pieno o il netto e' positivo:
                        # in bootstrap servono solo legno/argilla/ferro.
                        (
                            b_surplus,
                            b_free,
                            b_note,
                        ) = _cap_feed_shipment(
                            settings,
                            target_key=b_name or str(b_id),
                            surplus=b_surplus,
                            target_res=b_res,
                            target_role=settings.village_role_for(
                                b_name, b_id
                            ),
                        )
                        if b_note:
                            logger.info(
                                "%s Bootstrap bridge: %s",
                                vprefix,
                                b_note,
                            )
                        if (
                            b_surplus
                            and sum(b_free.values())
                            < FEED_TARGET_MIN_FREE
                        ):
                            # Destinatario quasi pieno: skip + cooldown
                            # anti-detection (SKIPPED pausa il modulo).
                            b_surplus = {}
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="expansion",
                                decision="BOOTSTRAP_BRIDGE",
                                outcome=DECISION_OUTCOME_SKIPPED,
                                reason=(
                                    f"Bootstrap bridge verso '{b_name}' "
                                    "saltato: destinatario quasi pieno."
                                ),
                                context={"target": b_name},
                            )
                    if b_surplus:
                        b_market = find_market(state.interior_fields())
                        b_mslot = (
                            int(vcfg_feed.market_slot or 0)
                            if (
                                (vcfg_feed := settings.village_config_for(
                                    village.name, target_vid
                                )) is not None
                                and vcfg_feed.market_slot
                            )
                            else (b_market.slot_id if b_market else 0)
                        )
                        if b_mslot:
                            b_params: dict = {
                                "market_slot": b_mslot,
                                "target_village": b_name,
                                "target": b_name,
                                "resources": b_surplus,
                            }
                            if bt.get("x") is not None and bt.get("y") is not None:
                                b_params["target_x"] = bt["x"]
                                b_params["target_y"] = bt["y"]
                            b_action = Action(
                                type=ActionType.SEND_RESOURCES,
                                params=b_params,
                                reason=(
                                    f"Bootstrap bridge: feeder -> "
                                    f"'{b_name}' invia {b_surplus} "
                                    "(cappato allo spazio libero)"
                                ),
                            )
                            b_res_exec = await executor.execute(
                                b_action, village_id=target_vid
                            )
                            logger.info(
                                "%s Bootstrap feeder: %s -> '%s' (%s)",
                                vprefix, b_surplus, b_name,
                                b_res_exec.message,
                            )
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="expansion",
                                decision="BOOTSTRAP_BRIDGE",
                                outcome=(
                                    DECISION_OUTCOME_EXECUTED
                                    if b_res_exec.success
                                    else DECISION_OUTCOME_BLOCKED
                                ),
                                reason=(
                                    f"Bootstrap bridge: {b_surplus} -> "
                                    f"'{b_name}' (cappato allo spazio "
                                    f"libero): {b_res_exec.message}"
                                ),
                                context={
                                    "resources": dict(b_surplus),
                                    "target": b_name,
                                },
                            )

            # 4.4 Feeder Role Push: un villaggio con role == "feeder" e
            # target_village valorizzato alimenta costantemente il
            # villaggio bersaglio appena supera la soglia di riserva
            # (keep_ratio), cappando il carico allo spazio libero reale
            # del destinatario (peer_villages) per evitare overflow.
            vcfg_push = settings.village_config_for(village.name, target_vid)
            push_target = (
                str(vcfg_push.target_village or "").strip()
                if (
                    vcfg_push is not None
                    and str(vcfg_push.role or "").strip().lower() == "feeder"
                )
                else ""
            )
            push_target_l = push_target.lower()
            if (
                push_target
                and push_target_l != str(village.name or "").strip().lower()
                and push_target != str(village.village_id or "")
            ):
                _pl = _module_pause_left(
                    settings,
                    str(village.village_id or village.name or "0"),
                    "expansion",
                    "FEEDER_PUSH",
                    resources=state.resources,
                )
                if _pl > 0:
                    _record_module_paused(
                        settings,
                        state=state,
                        village=village,
                        module="expansion",
                        decision="FEEDER_PUSH",
                        remaining=_pl,
                    )
                else:
                    push_market = find_market(state.interior_fields())
                    push_mslot = (
                        int(vcfg_push.market_slot or 0)
                        if vcfg_push.market_slot
                        else (
                            push_market.slot_id
                            if push_market is not None
                            else 0
                        )
                    )
                    p_surplus = feeder_surplus(
                        state,
                        keep_ratio=FEEDER_KEEP_RATIO,
                        crop_keep_ratio=FEEDER_CROP_KEEP_RATIO,
                    )
                    # Surplus calcolato prima del capping al destinatario:
                    # serve a distinguere "niente da spedire" (stato
                    # benigno, nessuna pausa) da "destinatario senza
                    # spazio libero per questo surplus" (cooldown).
                    p_had_surplus = bool(p_surplus)
                    # Destinatario per nome o newdid: le coordinate del
                    # payload si leggono SOLO dal suo record, mai dal
                    # villaggio mittente.
                    t_village = next(
                        (
                            v for v in villages
                            if v.name == push_target
                            or str(v.village_id) == push_target
                        ),
                        None,
                    )
                    t_name = (
                        str(t_village.name)
                        if t_village is not None and t_village.name
                        else push_target
                    )
                    t_res = (
                        state.peer_villages.get(t_name)
                        or state.peer_villages.get(push_target)
                        or next(
                            (
                                res
                                for n, res in state.peer_villages.items()
                                if n.strip().lower() == push_target_l
                            ),
                            None,
                        )
                    )
                    p_skip_logged = False
                    if p_surplus:
                        # Capping allo spazio libero del destinatario con
                        # margine di sicurezza: mai oltre l'85% della sua
                        # capienza massima per risorsa, al netto dei
                        # mercanti gia' in viaggio (radar), per non
                        # bloccare i premi task. Su croplog/15c in
                        # crescita il grano non parte se il granaio e'
                        # gia' al 70%+ o il netto grano e' positivo.
                        (
                            p_surplus,
                            p_free,
                            p_note,
                        ) = _cap_feed_shipment(
                            settings,
                            target_key=t_name or push_target,
                            surplus=p_surplus,
                            target_res=t_res,
                            target_role=settings.village_role_for(
                                t_name,
                                t_village.village_id
                                if t_village is not None
                                else 0,
                            ),
                        )
                        if p_note:
                            logger.info(
                                "%s Feeder push: %s", vprefix, p_note
                            )
                        if sum(p_free.values()) < FEED_TARGET_MIN_FREE:
                            # Destinatario quasi pieno: salto l'invio per
                            # evitare viaggi a vuoto o sprechi. SKIPPED
                            # mette in pausa FEEDER_PUSH (cooldown).
                            p_surplus = {}
                            p_skip_logged = True
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="expansion",
                                decision="FEEDER_PUSH",
                                outcome=DECISION_OUTCOME_SKIPPED,
                                reason=(
                                    f"Invio surplus a '{t_name}' saltato: "
                                    "destinatario quasi pieno (meno di "
                                    f"{FEED_TARGET_MIN_FREE} risorse di "
                                    "spazio libero)."
                                ),
                                context={"target": t_name},
                            )
                    if p_surplus and push_mslot:
                        p_params: dict = {
                            "market_slot": push_mslot,
                            "target_village": t_name,
                            "target": t_name,
                            "resources": p_surplus,
                        }
                        if (
                            t_village is not None
                            and t_village.x is not None
                            and t_village.y is not None
                        ):
                            p_params["target_x"] = t_village.x
                            p_params["target_y"] = t_village.y
                        else:
                            p_coords = village_coords_map.get(
                                t_name.strip().lower()
                            ) or village_coords_map.get(push_target_l)
                            if p_coords:
                                (
                                    p_params["target_x"],
                                    p_params["target_y"],
                                ) = p_coords
                            else:
                                t_cfg = settings.village_config_for(
                                    push_target
                                )
                                if (
                                    t_cfg is not None
                                    and t_cfg.x is not None
                                    and t_cfg.y is not None
                                ):
                                    p_params["target_x"] = t_cfg.x
                                    p_params["target_y"] = t_cfg.y
                        if "target_x" not in p_params:
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="expansion",
                                decision="FEEDER_PUSH",
                                outcome=DECISION_OUTCOME_SKIPPED,
                                reason=(
                                    f"Invio surplus a '{t_name}' saltato: "
                                    "coordinate del destinatario non note."
                                ),
                                context={"target": t_name},
                            )
                        else:
                            push_action = Action(
                                type=ActionType.SEND_RESOURCES,
                                params=p_params,
                                reason=(
                                    f"Feeder '{village.name}' -> "
                                    f"'{t_name}': invio surplus "
                                    f"{p_surplus}"
                                ),
                            )
                            push_res = await executor.execute(
                                push_action, village_id=target_vid
                            )
                            logger.info(
                                "%s Feeder push: %s -> '%s' (%s)",
                                vprefix, p_surplus, t_name,
                                push_res.message,
                            )
                            # BLOCKED (es. nessun mercante disponibile)
                            # mette in pausa FEEDER_PUSH via decision log.
                            _record_decision(
                                settings,
                                state=state, village=village,
                                module="expansion",
                                decision="FEEDER_PUSH",
                                outcome=(
                                    DECISION_OUTCOME_EXECUTED
                                    if push_res.success
                                    else DECISION_OUTCOME_BLOCKED
                                ),
                                reason=(
                                    f"Feeder '{village.name}' -> "
                                    f"'{t_name}': invio surplus "
                                    f"{p_surplus}: {push_res.message}"
                                ),
                                context={
                                    "resources": dict(p_surplus),
                                    "target": t_name,
                                },
                            )
                    elif not p_skip_logged:
                        if not push_mslot:
                            # Strutturale: senza mercato il push non puo'
                            # partire — SKIPPED sotto FEEDER_PUSH applica
                            # il cooldown standard anti-rilevamento.
                            _push_dec = "FEEDER_PUSH"
                            _push_reason = (
                                f"Invio surplus a '{t_name}' non "
                                "partito: mercato assente"
                            )
                        elif p_had_surplus:
                            # Il surplus c'era ma il destinatario non ha
                            # spazio libero per quelle risorse: cooldown
                            # come 'destinatario quasi pieno'.
                            _push_dec = "FEEDER_PUSH"
                            _push_reason = (
                                f"Invio surplus a '{t_name}' saltato: "
                                "destinatario senza spazio libero per "
                                "il surplus disponibile."
                            )
                        else:
                            # Stato benigno: il villaggio non ha ancora
                            # accumulato oltre la riserva. Decisione
                            # esente da pausa: rivaluto a ogni ciclo
                            # senza churn SKIPPED -> pausa -> retry.
                            _push_dec = "FEEDER_PUSH_IDLE"
                            _push_reason = (
                                f"Invio surplus a '{t_name}' non "
                                "partito: nessun surplus oltre la "
                                "riserva keep_ratio."
                            )
                        _record_decision(
                            settings,
                            state=state, village=village,
                            module="expansion",
                            decision=_push_dec,
                            outcome=DECISION_OUTCOME_SKIPPED,
                            reason=_push_reason,
                            context={"target": t_name},
                        )

            # 5. Pausa umana tra un villaggio e l'altro
            if v_idx < len(villages) - 1:
                await asyncio.sleep(random.uniform(2.5, 5.0))
        except Exception as v_exc:
            logger.warning(
                "%s Errore nel villaggio '%s': %s. Passo al successivo.",
                prefix,
                vname,
                v_exc,
            )
            continue

    # 2.9 Comandi rapidi del Command Center live: eseguiti dopo il giro
    # dei villaggi, quando interior_map e' completa per tutti i villaggi.
    try:
        await _run_manual_commands(
            settings,
            executor=executor,
            villages=villages,
            interior_map=interior_map,
            prefix=prefix,
        )
    except Exception as cmd_exc:
        logger.debug(
            "%s Comandi Command Center ignorati: %s", prefix, cmd_exc
        )

    # 3. Step espansione account-level: coloni/CP/target dopo il giro
    _pl = _module_pause_left(settings, "0", "expansion")
    if _pl > 0:
        _record_module_paused(
            settings,
            module="expansion",
            decision="NEW_VILLAGE",
            remaining=_pl,
        )
    if expansion_mgr is not None and _pl <= 0:
        try:
            await expansion_mgr.step(
                browser=browser,
                observer=observer,
                executor=executor,
                settings=settings,
                villages=villages,
                interior_map=interior_map,
                prefix=prefix,
                states_by_village=states_by_village,
            )
        except Exception as e_exc:
            logger.warning("%s Errore modulo espansione: %s", prefix, e_exc)
        _flush_module_audits(
            settings,
            module="expansion",
            managers=(expansion_mgr,),
        )

    # 3.1 Modulo eroe: cura, avventure, attributi (account-level, con cooldown)
    _pl = _module_pause_left(settings, "0", "hero")
    if _pl > 0:
        _record_module_paused(
            settings,
            module="hero",
            decision="HERO_TASKS",
            remaining=_pl,
        )
    if hero_mgr is not None and settings.hero.enabled and _pl <= 0:
        try:
            await hero_mgr.run(
                browser=browser,
                settings=settings,
                prefix=prefix,
                villages=villages,
            )
        except Exception as h_exc:
            logger.warning("%s Errore modulo eroe: %s", prefix, h_exc)

    # 4. Global Power Assessment: valutazione complessiva impero multi-villaggio
    if scout_brain is not None:
        try:
            power = assess_account_power(settings, villages, states_by_village, troops_by_village)
            scout_brain.last_assessment = power
            scout_brain._save()
            logger.info(
                "%s Global Power Assessment: Potenza Militare=%d, Net Crop=%d/h, Saturazione=%.1f%%",
                prefix, power.military_power, power.net_crop_production, power.storage_saturation_pct,
            )
        except Exception as p_exc:
            logger.debug("%s Errore Global Power Assessment: %s", prefix, p_exc)

    # 5. Telemetria Command Center: snapshot JSON del ciclo appena
    # concluso (letto da GET /api/dashboard-state).
    try:
        telemetry.export_dashboard_state(
            settings,
            villages=villages,
            states_by_village=states_by_village,
            expansion_mgr=expansion_mgr,
            hero_mgr=hero_mgr,
        )
    except Exception as tele_exc:
        logger.warning(
            "%s Snapshot telemetria non scritto: %s", prefix, tele_exc,
            exc_info=True,
        )

    logger.debug("%s Tutti i %d villaggi processati.", prefix, len(villages))


async def run_single_account_loop(
    settings: Settings,
    brain_type: Literal["rules", "llm"] = "rules",
    once: bool = False,
    dump: bool = False,
) -> None:
    """Loop di monitoraggio resiliente per un singolo account isolato."""
    prefix = f"[{settings.account_id}]"
    brain = RuleBasedBrain(settings) if brain_type == "rules" else LLMBrain(settings)

    logger.info("%s Avvio agente per %s", prefix, settings.server_url)

    consecutive_errors = 0
    browser = TravianBrowser(settings)
    observer: GameObserver | None = None
    executor: ActionExecutor | None = None
    # Snapshot risorse per villaggio: persiste tra i cicli, usato dal brain
    # per decidere dove inviare l'eccedenza in caso di storage pieno
    last_states: dict[str, Resources] = {}

    try:
        while True:
            try:
                # 0. Hot-reload della configurazione da accounts.json senza riavviare il bot
                try:
                    for fresh_acc in load_accounts():
                        if fresh_acc.account_id == settings.account_id:
                            settings.villages = fresh_acc.villages
                            settings.village_roles = fresh_acc.village_roles
                            settings.strategy_profile = fresh_acc.strategy_profile
                            settings.allow_parallel_build = fresh_acc.allow_parallel_build
                            settings.builder_enabled = fresh_acc.builder_enabled
                            settings.auto_video_bonus = fresh_acc.auto_video_bonus
                            settings.min_net_crop_threshold = fresh_acc.min_net_crop_threshold
                            settings.loop_interval_min = fresh_acc.loop_interval_min
                            settings.loop_interval_max = fresh_acc.loop_interval_max
                            settings.farming = fresh_acc.farming
                            settings.expansion = fresh_acc.expansion
                            settings.scout_brain = fresh_acc.scout_brain
                            settings.hero = fresh_acc.hero
                            settings.defense = fresh_acc.defense
                            settings.replenishment = fresh_acc.replenishment
                            break
                except Exception as hr_err:
                    logger.debug("%s Errore hot-reload config: %s", prefix, hr_err)

                # Jitter anti-detection: soglie campionate una volta per
                # ciclo (storage ~0.82-0.93, cura eroe ~45-62%) e lette
                # dalle regole del brain e dal modulo eroe.
                settings.cycle_jitter = sample_cycle_thresholds()

                # 0.1 Stop selettivo per-account (flag creato dalla dashboard GUI):
                # termina solo questo task, lasciando attivi gli altri account.
                stop_file = BASE_DIR / "state" / f"stop_{settings.account_id}.flag"
                if stop_file.exists():
                    stop_file.unlink(missing_ok=True)
                    # Il PID puo' essere condiviso con altri account (orchestratore
                    # unico): rimuovere il file e' l'unico modo per segnalare alla
                    # dashboard che QUESTO account e' fermo.
                    (BASE_DIR / "state" / f"agent_{settings.account_id}.pid").unlink(
                        missing_ok=True
                    )
                    logger.info("%s Stop richiesto dalla dashboard. Uscita.", prefix)
                    return

                # 0.2 Controllo interruttore di pausa d'emergenza (creato dalla dashboard GUI)
                pause_file = BASE_DIR / "state" / f"pause_{settings.account_id}.flag"
                if pause_file.exists():
                    logger.info("%s Agente in PAUSA (trovato %s). Attesa 10s...", prefix, pause_file.name)
                    await asyncio.sleep(10.0)
                    continue

                # Inizializza o ripristina il browser se non attivo
                if not browser.is_alive():
                    logger.info("%s Inizializzazione / ripristino contesto browser...", prefix)
                    await browser.recover()
                    assert browser.page is not None
                    observer = GameObserver(browser.page)
                    observer.dom_nav = settings.dom_navigation
                    executor = ActionExecutor(browser, settings)

                assert browser.page is not None
                if observer is None:
                    observer = GameObserver(browser.page)
                    observer.dom_nav = settings.dom_navigation
                else:
                    observer.page = browser.page

                if executor is None:
                    executor = ActionExecutor(browser, settings)
                else:
                    executor.browser = browser

                if dump:
                    await browser.ensure_logged_in()
                    await browser.goto(selectors.PATH_DORF1)
                    path = await browser.dump_debug("dorf1")
                    logger.info("%s Dump salvato in %s", prefix, path)
                    if once:
                        return

                await browser.ensure_logged_in()
                await browser.goto(selectors.PATH_DORF1)
                # Boot calibration: routing_mode nel profilo per-server.
                try:
                    await calibrate_routing(browser.page)
                except Exception:  # noqa: BLE001
                    pass

                # Esegue il ciclo completo su tutti i villaggi dell'account
                if settings.account_id not in _farming_managers:
                    _farming_managers[settings.account_id] = FarmingManager(
                        settings.account_id
                    )
                if settings.account_id not in _expansion_managers:
                    _expansion_managers[settings.account_id] = ExpansionManager(
                        settings.account_id
                    )
                if settings.account_id not in _scout_brains:
                    _scout_brains[settings.account_id] = AIScoutBrain(
                        settings.account_id
                    )
                if settings.account_id not in _hero_managers:
                    _hero_managers[settings.account_id] = HeroManager(
                        settings.account_id
                    )
                if settings.account_id not in _defense_managers:
                    _defense_managers[settings.account_id] = DefenseManager(
                        settings.account_id
                    )
                if settings.account_id not in _replenish_managers:
                    _replenish_managers[settings.account_id] = (
                        TroopReplenishmentManager(settings.account_id)
                    )
                if settings.account_id not in _war_stores:
                    _war_stores[settings.account_id] = WarStore(
                        settings.account_id
                    )
                await monitor_all_villages(
                    browser=browser,
                    observer=observer,
                    brain=brain,
                    executor=executor,
                    settings=settings,
                    prefix=prefix,
                    last_states=last_states,
                    farming_mgr=_farming_managers[settings.account_id],
                    expansion_mgr=_expansion_managers[settings.account_id],
                    scout_brain=_scout_brains[settings.account_id],
                    hero_mgr=_hero_managers[settings.account_id],
                    defense_mgr=_defense_managers[settings.account_id],
                    replenish_mgr=_replenish_managers[settings.account_id],
                    war_store=_war_stores[settings.account_id],
                )

                # Reset del conteggio errori e ripristino al completamento con successo
                if consecutive_errors > 0:
                    logger.info(
                        "%s Ciclo ripristinato con successo dopo %d errore/i.",
                        prefix,
                        consecutive_errors,
                    )
                    consecutive_errors = 0

                if once:
                    return

                interval = human_cycle_interval(
                    settings.loop_interval_min, settings.loop_interval_max
                )
                # Attacco in arrivo: il sonno non deve superare la
                # finestra di difesa (impatto - margine fuga - buffer),
                # altrimenti il modulo difesa scopre l'impatto troppo
                # tardi (log osservato: pause ~474s vs soglia panico).
                _attack_eta = _next_attack_eta_seconds(settings)
                if _attack_eta is not None:
                    _dodge_margin = float(
                        getattr(
                            settings.defense, "dodge_margin_seconds", 30
                        )
                        or 30
                    )
                    interval = min(
                        interval,
                        max(15.0, _attack_eta - _dodge_margin - 15.0),
                    )
                    logger.info(
                        "%s Attacco in arrivo tra %.0fs: intervallo "
                        "ciclo ridotto a %.0fs (margine fuga %.0fs).",
                        prefix, _attack_eta, interval, _dodge_margin,
                    )
                else:
                    logger.info(
                        "%s Prossimo ciclo tra %.0fs", prefix, interval
                    )
                # Attesa a chunk: FORCE_TICK anticipa il ciclo; i flag
                # stop/pausa vengono rilevati entro ~5s (non solo a fine
                # attesa), cosi' Interrompi/Pausa rispondono subito.
                _tick_flag = telemetry.force_tick_path(settings.account_id)
                _waited = 0.0
                while _waited < interval:
                    if _tick_flag.exists():
                        _tick_flag.unlink(missing_ok=True)
                        logger.info(
                            "%s FORCE_TICK: ciclo anticipato dalla dashboard.",
                            prefix,
                        )
                        break
                    if stop_file.exists() or pause_file.exists():
                        logger.info(
                            "%s Attesa interrotta da flag di controllo "
                            "(stop/pausa).",
                            prefix,
                        )
                        break
                    _step = min(5.0, interval - _waited)
                    await asyncio.sleep(_step)
                    _waited += _step

                # Sonno notturno: nessuna richiesta per 4-6h (inizio e
                # durata randomizzati per giorno intorno al base_hour).
                _ns_left = (
                    night_sleep_remaining(
                        settings.account_id,
                        base_hour=settings.night_sleep_start_hour,
                        hours_min=settings.night_sleep_hours_min,
                        hours_max=settings.night_sleep_hours_max,
                    )
                    if settings.night_sleep_enabled
                    else 0.0
                )
                if _ns_left > 0:
                    logger.info(
                        "%s Sonno notturno: prossima attivita' tra %.0f min.",
                        prefix,
                        _ns_left / 60.0,
                    )
                _ns_waited = 0.0
                # Scansione radar passiva: un GET dorf1 ogni 10-15 min
                # mantiene i vettori aggiornati senza azioni di gioco.
                _ns_radar_next = random.uniform(600.0, 900.0)
                _ns_radar_last = 0.0
                while _ns_waited < _ns_left:
                    # I flag operativi restano attivi anche durante il sonno.
                    if stop_file.exists() or pause_file.exists():
                        logger.info(
                            "%s Sonno notturno interrotto da flag di controllo.",
                            prefix,
                        )
                        break
                    if _tick_flag.exists():
                        _tick_flag.unlink(missing_ok=True)
                        logger.info(
                            "%s FORCE_TICK: sonno interrotto dalla dashboard.",
                            prefix,
                        )
                        break
                    _step = min(60.0, _ns_left - _ns_waited)
                    await asyncio.sleep(_step)
                    _ns_waited += _step
                    # Radar passivo: solo lettura DOM, nessuna azione.
                    if (
                        _ns_waited - _ns_radar_last >= _ns_radar_next
                        and observer is not None
                        and browser.is_alive()
                    ):
                        _ns_radar_last = _ns_waited
                        _ns_radar_next = random.uniform(600.0, 900.0)
                        try:
                            await browser.goto(selectors.PATH_DORF1)
                            _inc_r, _out_r = (
                                await observer.get_troop_movements(0)
                            )
                            _rvname = (
                                await observer.extract_village_name()
                            )
                            _rx = _ry = None
                            try:
                                for _rv in (
                                    await observer.get_all_villages()
                                ):
                                    if str(_rv.name or "") == str(
                                        _rvname or ""
                                    ):
                                        _rx, _ry = _rv.x, _rv.y
                                        break
                            except Exception:  # noqa: BLE001
                                pass
                            if _rx is None:
                                _rcfg = settings.village_config_for(
                                    str(_rvname or "")
                                )
                                if _rcfg is not None:
                                    _rx, _ry = _rcfg.x, _rcfg.y
                            _sync_radar_movements(
                                settings,
                                str(_rvname or ""),
                                _rx,
                                _ry,
                                _inc_r,
                                _out_r,
                                prefix=prefix,
                            )
                            logger.debug(
                                "%s Scansione radar notturna: %d in "
                                "arrivo, %d in partenza.",
                                prefix, len(_inc_r), len(_out_r),
                            )
                        except Exception as _ns_exc:  # noqa: BLE001
                            logger.debug(
                                "%s Scansione radar notturna fallita "
                                "(non bloccante): %s",
                                prefix, _ns_exc,
                            )

            except ConcurrentSessionError as cs_exc:
                # Login manuale altrove (o sessione invalidata): il bot si
                # ferma, niente contesa di sessione. Dopo la pausa il loop
                # riprova il login normalmente.
                logger.warning(
                    "%s %s Pausa di cortesia di %.0f min prima del retry.",
                    prefix,
                    cs_exc,
                    settings.session_conflict_pause_minutes,
                )
                browser.reset_session_tracking()
                try:
                    await asyncio.sleep(
                        settings.session_conflict_pause_minutes * 60.0
                    )
                except asyncio.CancelledError:
                    logger.info(
                        "%s Ciclo interrotto durante la pausa sessione.",
                        prefix,
                    )
                    break
                continue
            except asyncio.CancelledError:
                logger.info("%s Ciclo interrotto (cancellazione ricevuta).", prefix)
                break
            except Exception as exc:
                consecutive_errors += 1
                backoff_wait = _calculate_backoff(consecutive_errors)
                logger.warning(
                    "%s [Errore #%d] %s: %s. Tentativo di ripristino e retry tra %.1fs (backoff esponenziale)...",
                    prefix,
                    consecutive_errors,
                    type(exc).__name__,
                    exc,
                    backoff_wait,
                )

                # Tentativo di recupero del contesto o ricaricamento pagina
                try:
                    if not browser.is_alive():
                        await browser.recover()
                        if browser.page is not None and observer is not None:
                            observer.page = browser.page
                    else:
                        await browser.reload()
                except Exception as rec_exc:
                    logger.debug(
                        "%s Errore durante il recupero immediato: %s",
                        prefix,
                        rec_exc,
                    )

                if once:
                    logger.error("%s Uscita in modalità --once dopo errore.", prefix)
                    return

                try:
                    await asyncio.sleep(backoff_wait)
                except asyncio.CancelledError:
                    logger.info("%s Ciclo interrotto durante l'attesa di backoff.", prefix)
                    break
    finally:
        try:
            await browser.stop()
        except Exception:
            logger.debug("%s Errore durante la chiusura del browser", prefix, exc_info=True)
        logger.info("%s Agente terminato.", prefix)


async def _supervised_account_loop(
    settings: Settings,
    brain_type: Literal["rules", "llm"],
    once: bool,
    dump: bool,
) -> None:
    """Supervisore per isolare ogni account in modo che un crash non intacchi gli altri."""
    prefix = f"[{settings.account_id}]"
    try:
        await run_single_account_loop(
            settings, brain_type=brain_type, once=once, dump=dump
        )
    except asyncio.CancelledError:
        logger.info("%s Task account interrotto.", prefix)
    except Exception:
        logger.exception(
            "%s Eccezione critica non gestita nell'account (isolata dagli altri).",
            prefix,
        )


async def run_all_accounts(
    accounts: list[Settings],
    brain_type: Literal["rules", "llm"] = "rules",
    once: bool = False,
    dump: bool = False,
) -> None:
    """Esegue tutti gli account contemporaneamente in modo concorrente tramite asyncio.gather isolato."""
    if not accounts:
        logger.warning("Nessun account configurato da eseguire.")
        return

    logger.info("Avvio parallelo per %d account...", len(accounts))
    tasks = [
        asyncio.create_task(
            _supervised_account_loop(
                acc, brain_type=brain_type, once=once, dump=dump
            ),
            name=f"task_{acc.account_id}",
        )
        for acc in accounts
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("Arresto parallelo richiesto, interruzione task in corso...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Tutti i task account sono terminati.")