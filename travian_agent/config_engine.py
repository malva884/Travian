"""Motore di configurazione adattiva ruolo+capacita' e preview di fattibilita'.

Spec "Role-Aware Village Configurator & Live Feasibility Preview":

- ``generate_role_config(village_state, role)`` fonde il template del ruolo
  (``presets.ROLE_PRESETS``) con i vincoli strutturali del villaggio
  corrente: upgrade di Magazzino/Granaio auto-inseriti prima degli step
  il cui costo supera la capienza, target truppe scalati al 75% della
  produzione lorda di grano (Net Crop Cap), unita' filtrate per
  prerequisiti degli edifici produttivi.

- ``evaluate_config_preview(village_state, draft_config)`` e' una
  funzione pura (nessuna mutazione di stato) che calcola le metriche del
  pannello Live Preview: Net Crop proiettato a regime, costo totale del
  deficit truppe vs capienza, tempo stimato della coda edifici e check
  dei prerequisiti.

Costi/tempi degli edifici e upkeep unita' sono approssimazioni T4.x
(crescita 1.6 costi / 1.5 tempi): la preview serve a stimare ordini di
grandezza, non a replicare il server di gioco.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .building_prereqs import (
    FIELD_GID_TYPES,
    FOCUS_KEY_GIDS,
    GID_ACADEMY,
    GID_BARRACKS,
    GID_CROPLAND,
    GID_GRANARY,
    GID_LABELS,
    GID_MAIN_BUILDING,
    GID_RALLY_POINT,
    GID_RESIDENCE,
    GID_STABLE,
    GID_WAREHOUSE,
    GID_WORKSHOP,
    _PREREQS,
    gids_for_focus_key,
)
from .models import ResourceType
from .presets import (
    allowed_unit_slots_for_role,
    detect_tribe_from_keys,
    preset_for_role,
    resolve_role,
    resolve_troop_preset,
    troop_preset_for_role,
)
from .replenishment import normalize_unit_key, unit_label_to_key

logger = logging.getLogger(__name__)

RES_KEYS = ("wood", "clay", "iron", "crop")

# ---------------------------------------------------------------------------
# Tabelle statiche T4.x (approssimazioni per la preview di fattibilita')
# ---------------------------------------------------------------------------

# Upkeep di grano (unita'/ora) per unita' — t1-10 Romani, t11-20 Teutoni,
# t21-30 Galli. Serve al Net Crop Cap e alla metrica "a regime".
UNIT_UPKEEP: dict[str, int] = {
    # Romani
    "t1": 1, "t2": 1, "t3": 1, "t4": 2, "t5": 3,
    "t6": 4, "t7": 3, "t8": 6, "t9": 5, "t10": 1,
    # Teutoni
    "t11": 1, "t12": 1, "t13": 1, "t14": 1, "t15": 2,
    "t16": 3, "t17": 3, "t18": 6, "t19": 4, "t20": 1,
    # Galli
    "t21": 1, "t22": 1, "t23": 1, "t24": 2, "t25": 2,
    "t26": 3, "t27": 3, "t28": 6, "t29": 4, "t30": 1,
}

# Edificio produttore per unita' (indice nella decade tribu').
_TRAINER_BLOCKS = (
    (GID_BARRACKS, {1, 2, 3, 11, 12, 13, 21, 22}),
    (GID_STABLE, {4, 5, 6, 14, 15, 16, 23, 24, 25, 26}),
    (GID_WORKSHOP, {7, 8, 17, 18, 27, 28}),
    # Senatore/Capo/Coloni: Residenza o Palazzo.
    (GID_RESIDENCE, {9, 10, 19, 20, 29, 30}),
)


def unit_trainer_gid(unit_key: str) -> int:
    """Gid dell'edificio che addestra l'unita' (0 = sconosciuto)."""
    k = normalize_unit_key(unit_key)
    try:
        idx = int(k.lstrip("tu"))
    except ValueError:
        return 0
    for gid, idxs in _TRAINER_BLOCKS:
        if idx in idxs:
            return gid
    return 0


# Costo livello 1 (legno, argilla, ferro, grano) e tempo base (secondi).
# Crescita per livello: costo x1.6, tempo x1.5 (approssimazione T4.x).
_BUILDING_BASE: dict[int, tuple[int, int, int, int, int]] = {
    1: (40, 100, 50, 60, 260),      # woodcutter
    2: (80, 40, 80, 50, 250),       # clay_pit
    3: (100, 80, 30, 60, 450),      # iron_mine
    4: (70, 90, 70, 20, 145),       # cropland
    5: (520, 380, 290, 90, 5400),   # sawmill
    6: (440, 480, 320, 50, 5200),   # brickyard
    7: (200, 450, 510, 120, 7200),  # iron_foundry
    8: (500, 440, 360, 210, 4300),  # grain_mill
    9: (1200, 1480, 870, 1600, 5600),  # bakery
    GID_WAREHOUSE: (130, 160, 90, 40, 3600),
    GID_GRANARY: (80, 100, 70, 20, 3400),
    13: (170, 200, 380, 130, 3800),  # smithy
    14: (1750, 2250, 1530, 240, 5400),  # tournament_square
    GID_MAIN_BUILDING: (70, 40, 60, 20, 2100),
    GID_RALLY_POINT: (110, 160, 90, 70, 2800),
    17: (80, 70, 120, 50, 3600),     # marketplace
    18: (180, 130, 150, 80, 2700),   # embassy
    GID_BARRACKS: (210, 140, 260, 120, 3300),
    GID_STABLE: (260, 140, 220, 100, 3700),
    GID_WORKSHOP: (460, 510, 600, 320, 4400),
    GID_ACADEMY: (220, 160, 90, 40, 3600),
    23: (40, 50, 30, 10, 780),       # cranny
    24: (1250, 1110, 1260, 600, 20000),  # town_hall
    GID_RESIDENCE: (580, 460, 350, 180, 3900),
    26: (550, 800, 750, 250, 6100),  # palace
    27: (2880, 2740, 2580, 990, 9100),   # treasury
    28: (1400, 1330, 1200, 400, 5300),   # trade_office
    29: (630, 420, 780, 360, 5500),  # great_barracks
    30: (780, 420, 660, 300, 6000),  # great_stable
    31: (70, 90, 170, 70, 2500),     # city_wall
    32: (50, 80, 40, 30, 2200),      # earth_wall
    33: (160, 100, 80, 60, 2000),    # palisade
    34: (155, 130, 125, 70, 2400),   # stonemason
    35: (1460, 930, 1250, 1740, 6000),   # brewery
    36: (80, 120, 70, 90, 2000),     # trapper
    37: (700, 670, 700, 240, 2400),  # hero_mansion
    38: (650, 800, 450, 200, 6000),  # great_warehouse
    39: (400, 500, 350, 100, 6000),  # great_granary
    41: (780, 420, 660, 540, 6000),  # horse_trough
    42: (650, 620, 1480, 240, 4200),  # waterworks
}

_COST_GROWTH = 1.6
_TIME_GROWTH = 1.5
# Sconto tempo per livello dell'Edificio Principale (T4.x ~3-4%/lvl).
_MB_SPEED_FACTOR = 0.96
_MB_FLOOR = 0.35

# Capienza Magazzino/Granaio per livello (T4.x non-plus; livello 0 = 800
# base del villaggio). Capitale x3 — l'approssimazione non la copre.
_CAP_LEVELS = [
    800, 1200, 1700, 2300, 3100, 4000, 5000, 6300, 7800, 9600,
    11800, 14400, 17600, 21400, 25900, 31300, 37800, 45700, 55100,
    66400, 80000,
]

# Soglia Net Crop Cap: i target truppe non superano il 75% della
# produzione lorda di grano (override manuale sempre possibile).
NET_CROP_SAFETY_PCT = 0.75


def cap_at_level(level: int) -> int:
    """Capienza Magazzino/Granaio al livello dato (approssimazione)."""
    lvl = max(0, min(20, int(level or 0)))
    return _CAP_LEVELS[lvl]


def level_for_capacity(needed: int) -> int:
    """Livello minimo di Magazzino/Granaio per contenere `needed`."""
    for lvl, cap in enumerate(_CAP_LEVELS):
        if cap >= needed:
            return lvl
    return 20


def level_cost(gid: int, level: int) -> dict[str, int]:
    """Costo del singolo upgrade `gid` -> `level` (level >= 1)."""
    base = _BUILDING_BASE.get(gid)
    if base is None:
        return {}
    w, c, i, cr, _t = base
    f = _COST_GROWTH ** max(0, level - 1)
    return {
        "wood": int(w * f), "clay": int(c * f),
        "iron": int(i * f), "crop": int(cr * f),
    }


def level_time_hours(gid: int, level: int, mb_level: int) -> float:
    """Ore di costruzione per `gid` -> `level` con Ed. Principale `mb_level`."""
    base = _BUILDING_BASE.get(gid)
    if base is None:
        return 0.0
    secs = base[4] * (_TIME_GROWTH ** max(0, level - 1))
    mb = max(_MB_FLOOR, _MB_SPEED_FACTOR ** max(0, int(mb_level or 1) - 1))
    return secs * mb / 3600.0


# ---------------------------------------------------------------------------
# VillageState: stato strutturale letto dallo snapshot storico
# ---------------------------------------------------------------------------


@dataclass
class VillageState:
    """Capacita' strutturali correnti del villaggio (da snapshot)."""

    warehouse_cap: int = 0
    granary_cap: int = 0
    crop_production: int = 0          # produzione lorda grano/ora
    crop_balance: int | None = None   # netto grano/ora (None = non letto)
    buildings: dict[int, int] = field(default_factory=dict)   # gid -> lvl
    troops: dict[str, int] = field(default_factory=dict)      # tN -> qty
    tribe: str = ""

    @property
    def main_building_level(self) -> int:
        return int(self.buildings.get(GID_MAIN_BUILDING, 0) or 0)

    @property
    def net_crop(self) -> int:
        """Bilancio netto grano attuale; fallback sulla produzione lorda."""
        if self.crop_balance is not None:
            return int(self.crop_balance)
        return int(self.crop_production or 0)


def village_state_from_snapshot(snap: dict | None) -> VillageState:
    """Normalizza lo snapshot ``village_snapshots`` in ``VillageState``.

    Tollerante ai record storici pre-arricchimento (senza edifici,
    produzione o truppe): i campi mancanti restano ai default.
    """
    snap = snap or {}
    res = snap.get("resources") or {}
    prod = snap.get("production") or {}
    buildings: dict[int, int] = {}
    for k, v in (snap.get("buildings") or {}).items():
        try:
            buildings[int(k)] = int(v or 0)
        except (TypeError, ValueError):
            continue
    troops: dict[str, int] = {}
    for k, v in (snap.get("troops") or {}).items():
        uk = unit_label_to_key(str(k)) or normalize_unit_key(k)
        try:
            qty = int(v or 0)
        except (TypeError, ValueError):
            qty = 0
        if uk and qty > 0:
            troops[uk] = qty
    crop_balance = snap.get("crop_balance")
    try:
        crop_balance = int(crop_balance) if crop_balance is not None else None
    except (TypeError, ValueError):
        crop_balance = None
    tribe = str(snap.get("tribe") or "") or detect_tribe_from_keys(
        troops.keys()
    )
    return VillageState(
        warehouse_cap=int(res.get("warehouse_cap") or 0),
        granary_cap=int(res.get("granary_cap") or 0),
        crop_production=int(prod.get("crop") or 0),
        crop_balance=crop_balance,
        buildings=buildings,
        troops=troops,
        tribe=tribe,
    )


# ---------------------------------------------------------------------------
# Helpers interni
# ---------------------------------------------------------------------------


def _step_gid(key: str) -> int:
    """Primo gid del focus step (i gruppi alternativi usano la prima variante)."""
    gids = gids_for_focus_key(key)
    return gids[0] if gids else 0


def _step_levels(
    step: dict, buildings: dict[int, int]
) -> tuple[int, int, int]:
    """(gid, livello_corrente, target) per uno step di coda.

    ``target_level`` 0 = fino al massimo consentito (usiamo 20).
    Il livello corrente e' il massimo fra i gid del gruppo.
    """
    key = str(step.get("key") or step.get("gid") or "")
    raw = FOCUS_KEY_GIDS.get(key)
    gids = (raw,) if isinstance(raw, int) else tuple(raw or ())
    gids = tuple(g for g in gids if g > 0)
    if not gids:
        return 0, 0, 0
    current = max((buildings.get(g, 0) for g in gids), default=0)
    target = int(step.get("target_level") or 0) or 20
    return gids[0], current, min(20, target)


def _prereq_list(gid: int) -> list[tuple[int, int]]:
    reqs, _cap = _PREREQS.get(gid, ([], False))
    return list(reqs)


def _missing_prereqs(gid: int, levels: dict[int, int]) -> list[str]:
    """Prerequisiti mancanti per costruire `gid` date le `levels`."""
    out: list[str] = []
    for req_gid, req_lvl in _prereq_list(gid):
        if levels.get(req_gid, 0) < req_lvl:
            out.append(
                f"{GID_LABELS.get(req_gid, f'gid {req_gid}')} {req_lvl}"
            )
    return out


def _simulate_queue(
    queue: list[dict],
    buildings: dict[int, int],
    warehouse_cap: int,
    granary_cap: int,
) -> dict:
    """Simula la coda step per step.

    Ritorna ore totali, il primo step bloccato per capienza, i warning
    sui prerequisiti e le capienze finali proiettate. Gli upgrade di
    Magazzino/Granaio nella coda aggiornano la capienza proiettata per
    gli step successivi.
    """
    levels = dict(buildings)
    wh = warehouse_cap or cap_at_level(levels.get(GID_WAREHOUSE, 0))
    gr = granary_cap or cap_at_level(levels.get(GID_GRANARY, 0))
    mb = levels.get(GID_MAIN_BUILDING, 0)
    total_h = 0.0
    first_block = ""
    warnings: list[str] = []
    for pos, step in enumerate(queue, start=1):
        gid, current, target = _step_levels(step, levels)
        if gid <= 0 or target <= current:
            continue
        # Prerequisiti nuova costruzione vs livelli simulati (incluse le
        # code precedenti): un edificio gia' presente ha i prerequisiti
        # per definizione soddisfatti.
        if current == 0:
            missing = _missing_prereqs(gid, levels)
            if missing:
                label = GID_LABELS.get(gid, str(step.get("key") or gid))
                warnings.append(
                    f"Step {pos} ({label}): manca {', '.join(missing)}"
                )
                # Non saltabile: la coda resta teorica, si prosegue la
                # simulazione assumendo che il prerequisito verra' fatto.
        for lvl in range(current + 1, target + 1):
            total_h += level_time_hours(gid, lvl, max(1, mb))
            if gid == GID_MAIN_BUILDING:
                mb = lvl
        cost_hi = level_cost(gid, target)
        if cost_hi and not first_block:
            need_wh = max(
                cost_hi["wood"], cost_hi["clay"], cost_hi["iron"]
            )
            if need_wh > wh:
                first_block = (
                    f"Step {pos} ({GID_LABELS.get(gid, gid)} -> {target}): "
                    f"costo ~{need_wh} > Magazzino {wh}"
                )
            elif cost_hi["crop"] > gr:
                first_block = (
                    f"Step {pos} ({GID_LABELS.get(gid, gid)} -> {target}): "
                    f"costo ~{cost_hi['crop']} > Granaio {gr}"
                )
        levels[gid] = target
        if gid == GID_WAREHOUSE:
            wh = cap_at_level(target)
        elif gid == GID_GRANARY:
            gr = cap_at_level(target)
    return {
        "estimated_queue_hours": round(total_h, 1),
        "first_capacity_block": first_block,
        "queue_warnings": warnings,
        "warehouse_cap_final": wh,
        "granary_cap_final": gr,
    }


def _normalize_unit_targets(targets: dict) -> dict[str, int]:
    """{label|tN: qty} -> {tN: qty} (0 o chiave ignota escluse)."""
    out: dict[str, int] = {}
    for k, v in (targets or {}).items():
        uk = unit_label_to_key(str(k)) or normalize_unit_key(k)
        try:
            qty = int(v or 0)
        except (TypeError, ValueError):
            qty = 0
        if uk and qty > 0:
            out[uk] = qty
    return out


# ---------------------------------------------------------------------------
# 1.1 Motore di generazione default adattivo (Role + Village Capacity)
# ---------------------------------------------------------------------------


def generate_role_config(
    village_state: VillageState | dict | None,
    role: str,
) -> dict:
    """Profilo iniziale ruolo adattato alle capacita' del villaggio.

    ``village_state`` puo' essere un ``VillageState`` o il dict snapshot
    grezzo. Output::

        {
          "role": <ruolo canonico>,
          "building_queue": [{"key", "target_level", "priority",
                              "source": "auto"}],
          "troop_targets": {tN: qty},
          "allowed_slots": [slot, ...],
          "excluded_units": [{unit, reason}],
          "storage_auto_inserted": [{"key", "target_level", "before"}],
        }
    """
    vstate = (
        village_state
        if isinstance(village_state, VillageState)
        else village_state_from_snapshot(village_state)
    )
    resolved = resolve_role(role)
    steps = [dict(s) for s in preset_for_role(resolved)]

    # --- Capienza: inserisce upgrade Magazzino/Granaio prima degli step
    # il cui costo di picco supera la capienza corrente/proiettata.
    levels = dict(vstate.buildings)
    wh = vstate.warehouse_cap or cap_at_level(levels.get(GID_WAREHOUSE, 0))
    gr = vstate.granary_cap or cap_at_level(levels.get(GID_GRANARY, 0))
    queue: list[dict] = []
    inserted: list[dict] = []
    wh_lvl = levels.get(GID_WAREHOUSE, 0)
    gr_lvl = levels.get(GID_GRANARY, 0)

    def _push(key: str, lvl: int) -> None:
        queue.append({
            "key": key, "target_level": lvl,
            "priority": len(queue) + 1, "source": "auto",
        })

    for step in steps:
        gid, current, target = _step_levels(step, levels)
        if gid > 0 and target > current:
            cost = level_cost(gid, target)
            need_wh = max(
                cost.get("wood", 0), cost.get("clay", 0),
                cost.get("iron", 0),
            )
            if need_wh > wh and wh_lvl < 20:
                new_lvl = max(wh_lvl + 1, level_for_capacity(need_wh))
                if new_lvl > wh_lvl:
                    _push("warehouse", new_lvl)
                    inserted.append({
                        "key": "warehouse", "target_level": new_lvl,
                        "before": step.get("key"),
                    })
                    wh_lvl, wh = new_lvl, cap_at_level(new_lvl)
            if cost.get("crop", 0) > gr and gr_lvl < 20:
                new_lvl = max(
                    gr_lvl + 1, level_for_capacity(cost["crop"])
                )
                if new_lvl > gr_lvl:
                    _push("granary", new_lvl)
                    inserted.append({
                        "key": "granary", "target_level": new_lvl,
                        "before": step.get("key"),
                    })
                    gr_lvl, gr = new_lvl, cap_at_level(new_lvl)
        _push(str(step.get("key")), int(step.get("target_level") or 0))
        if gid > 0:
            levels[gid] = max(levels.get(gid, 0), target)
            if gid == GID_WAREHOUSE:
                wh_lvl = max(wh_lvl, target)
                wh = cap_at_level(wh_lvl)
            elif gid == GID_GRANARY:
                gr_lvl = max(gr_lvl, target)
                gr = cap_at_level(gr_lvl)

    # --- Truppe: preset ruolo -> tN, filtro edifici produttivi, poi
    # Net Crop Cap (75% della produzione lorda di grano).
    queue_gids = {_step_gid(s.get("key", "")) for s in queue}
    raw_targets = resolve_troop_preset(
        troop_preset_for_role(resolved), vstate.tribe
    )
    excluded: list[dict] = []
    targets: dict[str, int] = {}
    for unit, qty in raw_targets.items():
        tg = unit_trainer_gid(unit)
        if tg and levels.get(tg, 0) == 0 and tg not in queue_gids:
            excluded.append({
                "unit": unit,
                "reason": (
                    f"{GID_LABELS.get(tg, tg)} assente e non in coda"
                ),
            })
            continue
        targets[unit] = qty
    gross = int(vstate.crop_production or 0)
    if gross > 0 and targets:
        budget = NET_CROP_SAFETY_PCT * gross
        upkeep = sum(q * UNIT_UPKEEP.get(u, 1) for u, q in targets.items())
        if upkeep > budget:
            scale = budget / upkeep
            targets = {
                u: max(1, int(q * scale)) for u, q in targets.items()
            }
    return {
        "role": resolved,
        "building_queue": queue,
        "troop_targets": targets,
        "allowed_slots": allowed_unit_slots_for_role(resolved),
        "excluded_units": excluded,
        "storage_auto_inserted": inserted,
    }


# ---------------------------------------------------------------------------
# 1.2 Live Preview & Fattibilita' (funzione pura — nessuna mutazione)
# ---------------------------------------------------------------------------


def evaluate_config_preview(
    village_state: VillageState | dict | None,
    draft_config: dict,
) -> dict:
    """Metriche del pannello Live Preview per la bozza di configurazione.

    ``draft_config``: {"building_queue": [step], "troop_targets": {u: q}}.
    Non modifica alcuno stato persistente.
    """
    vstate = (
        village_state
        if isinstance(village_state, VillageState)
        else village_state_from_snapshot(village_state)
    )
    queue = list(draft_config.get("building_queue") or [])
    targets = _normalize_unit_targets(
        draft_config.get("troop_targets") or {}
    )

    # --- Net Crop a regime: netto attuale - upkeep del deficit truppe.
    deficit = {
        u: max(0, q - int(vstate.troops.get(u, 0)))
        for u, q in targets.items()
    }
    upkeep_add = sum(q * UNIT_UPKEEP.get(u, 1) for u, q in deficit.items())
    projected = vstate.net_crop - upkeep_add
    if projected < 0:
        net_crop_status = "danger"
    elif projected <= 50:
        net_crop_status = "warning"
    else:
        net_crop_status = "ok"

    # --- Costo totale del deficit truppe vs capienza attuale.
    try:
        from .actions import STATIC_TROOP_COSTS
    except Exception:  # noqa: BLE001
        STATIC_TROOP_COSTS = {}
    total_cost = {r: 0 for r in RES_KEYS}
    for u, q in deficit.items():
        cost = STATIC_TROOP_COSTS.get(u) or {}
        for r in RES_KEYS:
            total_cost[r] += int(cost.get(r, 0) or 0) * q
    wh_cap = vstate.warehouse_cap or cap_at_level(
        vstate.buildings.get(GID_WAREHOUSE, 0)
    )
    gr_cap = vstate.granary_cap or cap_at_level(
        vstate.buildings.get(GID_GRANARY, 0)
    )
    wh_need = max(total_cost[r] for r in ("wood", "clay", "iron"))
    warehouse_overflow = wh_cap > 0 and wh_need > wh_cap
    granary_overflow = gr_cap > 0 and total_cost["crop"] > gr_cap

    # --- Simulazione coda: tempi, primo blocco capienza, prerequisiti.
    sim = _simulate_queue(
        queue, vstate.buildings, vstate.warehouse_cap, vstate.granary_cap
    )

    # --- Prerequisiti truppe: edificio produttore assente e non in coda.
    queue_gids = {_step_gid(s.get("key", "")) for s in queue}
    missing_prereqs: list[str] = []
    for u, q in targets.items():
        tg = unit_trainer_gid(u)
        if (
            tg
            and vstate.buildings.get(tg, 0) == 0
            and tg not in queue_gids
        ):
            missing_prereqs.append(
                f"{u}: {GID_LABELS.get(tg, tg)} assente (target {q})"
            )

    return {
        "projected_net_crop": projected,
        "net_crop_status": net_crop_status,
        "current_net_crop": vstate.net_crop,
        "troop_upkeep_delta": upkeep_add,
        "troop_cost_total": total_cost,
        "warehouse_overflow_risk": warehouse_overflow,
        "granary_overflow_risk": granary_overflow,
        "warehouse_cap": wh_cap,
        "granary_cap": gr_cap,
        "estimated_queue_hours": sim["estimated_queue_hours"],
        "first_capacity_block": sim["first_capacity_block"],
        "queue_warnings": sim["queue_warnings"],
        "missing_prerequisites": missing_prereqs,
    }
