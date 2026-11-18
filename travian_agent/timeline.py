"""Proiezione "Scaletta Costruzioni" (Timeline & Previsione Code).

Spec "Timeline Costruzioni & Previsione Code": produce per ogni
villaggio una vista unificata dei lavori attivi (dorf1/dorf2) e della
scaletta stimata dei prossimi upgrade, con orario di inizio/fine e
stato delle risorse.

Nessuna chiamata al browser e nessuno scraping: la proiezione lavora
sul ``GameState`` del ciclo (coda, stock, produzione, livelli) e sul
piano decisionale del builder (``building_queue_plan`` per il dorf2 +
selezione economica per il dorf1), riusando le tabelle T4.x di
``config_engine`` (costo/tempo per gid+livello).

Approssimazioni note (documentate, volutamente conservative):
- durate = tabella T4.x * fattore Edificio Principale / ``server_speed``
  (``config_engine.level_time_hours``);
- le risorse degli step pianificati sono addebitate in modo cumulativo
  ("committed") come se venissero prelevate all'avvio del lavoro;
- un candidato il cui costo supera la capienza viene marcato
  ``storage_cap_exceeded`` e saltato (il binario prosegue: il bot
  sceglierebbe un altro candidato);
- gli upgrade del Municipio (gid 15) nella proiezione riducono i tempi
  stimati dei lavori schedulati DOPO il loro avvio.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Any, Iterator

from . import builder
from .building_prereqs import (
    FIELD_GID_TYPES,
    GID_LABELS,
    GID_MAIN_BUILDING,
)
from .config import Settings
from .config_engine import level_cost, level_time_hours
from .models import Field, GameState, ResourceType

_RES_KEYS: tuple[str, ...] = ("wood", "clay", "iron", "crop")
_RES_LABELS = {
    "wood": "legno",
    "clay": "argilla",
    "iron": "ferro",
    "crop": "grano",
}

# Profondita' massima della proiezione per singolo binario: oltre il
# limit richiesto dalla UI le stime perdono di senso (le code reali
# vengono ricalcolate a ogni tick).
_MAX_STREAM_DEPTH = 24


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return (
        datetime.fromtimestamp(float(ts), timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _hourly(res: Any, key: str) -> float:
    """Produzione oraria lorda della risorsa (chiavi enum o str)."""
    prod = getattr(res, "production_per_hour", None) or {}
    for k, v in prod.items():
        if str(getattr(k, "value", k)) == key and v:
            return float(v)
    return 0.0


# ---------------------------------------------------------------------------
# Generazione dei candidati (streams per binario)
# ---------------------------------------------------------------------------

def _cand(
    *,
    slot: int,
    gid: int,
    name: str,
    frm: int,
    to: int,
    queue: str,
    source: str,
) -> dict:
    return {
        "slot": int(slot),
        "gid": int(gid),
        "name": str(name or GID_LABELS.get(gid, f"gid {gid}") if gid else name),
        "from_level": int(frm),
        "to_level": int(to),
        "queue": queue,
        "source": source,
    }


def _field_max(state: GameState, f: Field, capital: bool) -> int:
    return builder.field_max_level(state, f)


def _system_candidates(
    state: GameState,
    settings: Settings,
    sim_level: dict[int, int],
    queued: set[int],
) -> list[dict]:
    """Testate "Priorita' Sistema": storage quasi pieno, grain safety,
    anti-raid — nello stesso ordine delle regole del brain.

    Per ogni regola viene emesso solo il candidato di testa (le altre
    voci sono alternative per lo stesso slot coda).
    """
    out: list[dict] = []

    def _push(f: Field) -> None:
        gid = f.gid if f.gid > 0 else builder.field_gid(f)
        out.append(
            _cand(
                slot=f.slot_id,
                gid=gid,
                name=builder.field_label(f),
                frm=sim_level.get(f.slot_id, f.level),
                to=sim_level.get(f.slot_id, f.level) + 1,
                queue="dorf1" if f.is_resource_field else "dorf2",
                source="system",
            )
        )
        sim_level[f.slot_id] = sim_level.get(f.slot_id, f.level) + 1

    # 1. Storage quasi pieno (soglia jitter del ciclo, come la regola).
    jit = getattr(settings, "cycle_jitter", None) or {}
    needed = builder.storage_needed(state, float(jit.get("storage", 0.90)))
    if needed:
        interior = [
            f
            for f in builder.upgradeable_fields(state, state.interior_fields())
            if f.gid in needed and f.slot_id not in queued
        ]
        interior.sort(key=lambda f: (f.level, f.slot_id))
        if interior:
            _push(interior[0])
        else:
            newbies = builder.new_building_intents(state, needed)
            if newbies:
                _push(newbies[0])

    # 2. Grain Safety Net: campi grano (dorf1) poi Mulino/Forno (dorf2).
    if builder.crop_safety_needed(state, settings):
        for f in builder.grain_safety_intents(state)[:3]:
            if f.slot_id in queued:
                continue
            _push(f)

    # 3. Anti-Raid: Nascondigli sotto pressione raid (dorf2).
    if getattr(state, "under_raid_pressure", False):
        for f in builder.anti_raid_intents(state, settings)[:1]:
            if f.slot_id not in queued:
                _push(f)

    return out


def _focus_stream(
    state: GameState,
    settings: Settings,
    sim_level: dict[int, int],
    sim_gid: dict[int, int],
    queued: set[int],
    capital: bool,
    budget: int,
) -> list[dict]:
    """Espansione multi-livello della Coda Prioritaria (dorf2 + dorf1).

    Include tutti gli item della Coda Prioritaria con current_level < target_level
    (sia quelli a lv == 0 sia quelli a lv > 0), proiettando ogni singolo step
    fino al target o al budget.
    """
    vcfg = settings.village_config_for(state.village_name, state.village_id)
    if vcfg is None or not getattr(vcfg, "building_focus_enabled", True):
        return []
    plan = list(vcfg.building_queue_plan or vcfg.building_focus or [])
    if not plan:
        return []

    interiors = {f.slot_id: f for f in state.interior_fields()}
    res_fields = [f for f in state.resource_fields()]
    reserved_slots: set[int] = set()
    out: list[dict] = []

    items = builder._focus_items(plan)
    for it in items:
        if len(out) >= budget:
            break

        gids = (it.gid,) if it.gid > 0 else tuple(builder.gids_for_focus_key(it.key))
        if not gids:
            continue
        target = it.target_level

        # --- Step campo risorsa (dorf1): tutti i campi del tipo ---
        if len(gids) == 1 and gids[0] in FIELD_GID_TYPES:
            rtype = FIELD_GID_TYPES[gids[0]]
            while len(out) < budget:
                if it.slot_id is not None and 1 <= it.slot_id <= 18:
                    cands = [
                        f
                        for f in res_fields
                        if f.slot_id == it.slot_id
                        and sim_level.get(f.slot_id, f.level)
                        < _field_max(state, f, capital)
                        and (
                            target <= 0
                            or sim_level.get(f.slot_id, f.level) < target
                        )
                    ]
                else:
                    cands = [
                        f
                        for f in res_fields
                        if f.resource_type is rtype
                        and sim_level.get(f.slot_id, f.level)
                        < _field_max(state, f, capital)
                        and (
                            target <= 0
                            or sim_level.get(f.slot_id, f.level) < target
                        )
                    ]
                if not cands:
                    break
                f = min(
                    cands,
                    key=lambda x: (sim_level.get(x.slot_id, x.level), x.slot_id),
                )
                lvl = sim_level.get(f.slot_id, f.level)
                out.append(
                    _cand(
                        slot=f.slot_id,
                        gid=builder.field_gid(f),
                        name=builder.field_label(f),
                        frm=lvl,
                        to=lvl + 1,
                        queue="dorf1",
                        source="focus",
                    )
                )
                sim_level[f.slot_id] = lvl + 1
            continue

        # --- Step edificio interno (dorf2) ---
        slot_id, cur_lvl, status = builder.resolve_target_slot(
            it, state, reserved_slots
        )
        if (
            status
            in (
                "TARGET_REACHED",
                "INVALID_SLOT_OCCUPIED",
                "NO_FREE_SLOTS",
                "INVALID_ITEM",
            )
            or slot_id is None
        ):
            continue

        gid = it.gid if it.gid > 0 else builder._pick_group_gid(state, gids)
        sim_gid[slot_id] = gid
        target_limit = target if target > 0 else 20

        while len(out) < budget and sim_level.get(slot_id, cur_lvl) < target_limit:
            lvl = sim_level.get(slot_id, cur_lvl)
            sim_g = sim_gid.get(slot_id, gid)
            f_real = interiors.get(slot_id)
            name = (
                builder.field_label(f_real)
                if f_real is not None and f_real.gid > 0
                else GID_LABELS.get(sim_g, f"gid {sim_g}")
            )
            out.append(
                _cand(
                    slot=slot_id,
                    gid=sim_g,
                    name=name,
                    frm=lvl,
                    to=lvl + 1,
                    queue="dorf2",
                    source="focus",
                )
            )
            sim_level[slot_id] = lvl + 1

    return out


def _economy_stream(
    state: GameState,
    settings: Settings,
    sim_level: dict[int, int],
    queued: set[int],
    capital: bool,
    budget: int,
    crop_only: bool = False,
) -> list[dict]:
    """Fallback dorf1: campo piu' debole della risorsa piu' scarsa.

    Replica la selezione di ``rule_upgrade_weakest_field``/``economy_intents``
    simulando gli scatti di livello a ogni emissione.
    """
    fields = [
        f for f in state.resource_fields() if f.slot_id not in queued
    ]
    if not fields:
        return []
    role = settings.village_role_for(state.village_name, state.village_id)
    scarcest = (
        ResourceType.CROP if role == "croplog" else state.resources.scarcest()
    )
    out: list[dict] = []
    while len(out) < budget:
        cands = [
            f
            for f in fields
            if sim_level.get(f.slot_id, f.level)
            < _field_max(state, f, capital)
        ]
        if crop_only:
            cands = [
                f for f in cands if f.resource_type is ResourceType.CROP
            ]
        if not cands:
            break
        same = [f for f in cands if f.resource_type is scarcest]
        f = min(
            same or cands,
            key=lambda x: (sim_level.get(x.slot_id, x.level), x.slot_id),
        )
        lvl = sim_level.get(f.slot_id, f.level)
        out.append(
            _cand(
                slot=f.slot_id,
                gid=builder.field_gid(f),
                name=builder.field_label(f),
                frm=lvl,
                to=lvl + 1,
                queue="dorf1",
                source="economy",
            )
        )
        sim_level[f.slot_id] = lvl + 1
    return out


# ---------------------------------------------------------------------------
# Simulazione temporale
# ---------------------------------------------------------------------------

def _duration_s(gid: int, to_level: int, mb_level: int, speed: float) -> float:
    """Durata stimata del singolo livello (secondi di gioco)."""
    secs = level_time_hours(gid, max(1, to_level), max(1, mb_level)) * 3600.0
    if secs <= 0:
        secs = 1800.0  # fallback per gid non tabellato (~30 min)
    return secs / max(0.1, speed)


def _res_wait(
    cost: dict,
    stock: dict[str, float],
    committed: dict[str, float],
    rate: dict[str, float],
    caps: dict[str, int],
    now: float,
) -> tuple[float, dict[str, int], bool]:
    """(ready_ts, missing, cap_exceeded) per un costo di upgrade.

    ``committed`` tiene conto delle risorse gia' impegnate dagli step
    precedenti della proiezione (addebito all'avvio del lavoro).
    """
    cap_hit = any(
        caps.get(r, 0) > 0 and cost.get(r, 0) > caps[r] for r in _RES_KEYS
    )
    missing: dict[str, int] = {}
    worst = 0.0
    unreachable = False
    for r in _RES_KEYS:
        deficit = float(cost.get(r, 0)) - stock[r] + committed[r]
        if deficit <= 0:
            continue
        missing[r] = int(deficit)
        if rate[r] <= 0:
            unreachable = True  # produzione nulla/negativa: mai coperto
            continue
        worst = max(worst, deficit / rate[r])
    if unreachable:
        return float("inf"), missing, cap_hit
    return now + worst, missing, cap_hit


def _sched_entry(
    item: dict,
    *,
    start: float | None,
    duration: float,
    wait_reason: str | None,
    missing: dict[str, int],
) -> dict:
    end = (start + duration) if start is not None else None
    return {
        "status": "scheduled",
        "queue": item["queue"],
        "slot": item["slot"],
        "gid": item["gid"],
        "name": item["name"],
        "from_level": item["from_level"],
        "to_level": item["to_level"],
        "start_ts": start,
        "end_ts": end,
        "start_at": _iso(start),
        "end_at": _iso(end),
        "duration_s": int(duration),
        "remaining_s": None,
        "wait_reason": wait_reason,
        "missing": missing or None,
        "source": item["source"],
    }


def project_timeline(
    state: GameState,
    settings: Settings,
    limit: int = 8,
    now: float | None = None,
) -> dict:
    """Timeline attiva + proiettata del villaggio (funzione pura).

    Ritorna il contratto della spec::

        {"generated_at", "now_ts", "server_speed",
         "main_building_level", "parallel_build", "timeline": [...]}
    """
    now = float(now or getattr(state, "timestamp", 0.0) or time.time())
    res = state.resources
    speed = float(getattr(settings, "server_speed", 1.0) or 1.0)
    if speed <= 1.0:
        url = str(getattr(settings, "server_url", "") or "")
        m = re.search(r"[._/-]x([1-9]\d*)\b", url, re.I)
        if m:
            try:
                speed = float(m.group(1))
            except ValueError:
                pass
    speed = max(0.1, speed)
    parallel = builder.parallel_build_enabled(settings)
    capital = builder.is_capital(state)
    mb_level = max(
        (f.level for f in state.interior_fields()
         if f.gid == GID_MAIN_BUILDING),
        default=0,
    )

    stock = {r: float(getattr(res, r, 0) or 0) for r in _RES_KEYS}
    rate = {r: _hourly(res, r) for r in _RES_KEYS}
    # Grano: il netto (produzione - upkeep) e' il tasso corretto di
    # accumulo; fallback sul lordo se production.php non e' stata letta.
    if getattr(res, "crop_balance", None) is not None:
        rate["crop"] = float(res.crop_balance)
    caps = {
        "wood": int(getattr(res, "warehouse_capacity", 0) or 0),
        "clay": int(getattr(res, "warehouse_capacity", 0) or 0),
        "iron": int(getattr(res, "warehouse_capacity", 0) or 0),
        "crop": int(getattr(res, "granary_capacity", 0) or 0),
    }
    committed = {r: 0.0 for r in _RES_KEYS}
    sim_level = {f.slot_id: int(f.level) for f in state.fields}
    for q in state.build_queue:
        if q.slot_id and q.slot_id > 0 and q.level and q.level > 0:
            sim_level[q.slot_id] = max(sim_level.get(q.slot_id, 0), int(q.level))
    sim_gid = {f.slot_id: int(f.gid) for f in state.fields}
    queued = {
        q.slot_id for q in state.build_queue if q.slot_id > 0
    }

    # ---- Lavori in corso (dati reali del server) ----
    timeline: list[dict] = []
    for q in state.build_queue:
        end = now + max(0, int(q.remaining_seconds or 0))
        timeline.append(
            {
                "status": "in_progress",
                "queue": "dorf1" if q.is_resource_field else "dorf2",
                "slot": int(q.slot_id or 0),
                "gid": 0,
                "name": str(q.name or ""),
                "from_level": max(0, int(q.level or 0) - 1),
                "to_level": int(q.level or 0),
                "start_ts": None,
                "end_ts": end,
                "start_at": None,
                "end_at": _iso(end),
                "duration_s": None,
                "remaining_s": max(0, int(q.remaining_seconds or 0)),
                "wait_reason": None,
                "missing": None,
                "source": "queue",
            }
        )

    # ---- Binari coda: catena seriale per binario ----
    # In Travian la coda e' seriale: un lavoro inizia quando finisce il
    # precedente sullo stesso binario. I limiti max_*_queue controllano
    # solo quanti item possono essere accodati, non il parallelismo.
    # I Romani (o allow_parallel_build) hanno due binari indipendenti.
    def _last_end(items: list) -> float:
        ends = [
            now + max(0, int(q.remaining_seconds or 0)) for q in items
        ]
        return max(ends, default=now)

    if parallel:
        free_at = {
            "dorf1": _last_end(state.resource_queue()),
            "dorf2": _last_end(state.interior_queue()),
        }
    else:
        shared_end = _last_end(list(state.build_queue))
        free_at = {"dorf1": shared_end, "dorf2": shared_end}

    # ---- Streams di candidati ----
    grain_lock = builder.crop_safety_needed(state, settings)
    freeze = builder.expansion_freeze(state, settings)
    sys_items = _system_candidates(state, settings, sim_level, queued)
    focus_items: list[dict] = []
    econ_items: list[dict] = []
    if not freeze:
        if not grain_lock:
            focus_items = _focus_stream(
                state, settings, sim_level, sim_gid, queued, capital,
                budget=_MAX_STREAM_DEPTH,
            )
        econ_items = _economy_stream(
            state, settings, sim_level, queued, capital,
            budget=_MAX_STREAM_DEPTH,
            crop_only=grain_lock,
        )

    sim_mb = int(mb_level)

    # Chiave del binario di un candidato: con coda condivisa tutti gli
    # item competono per lo stesso "shared".
    def _bin(item: dict) -> str:
        return item["queue"] if parallel else "shared"

    if not parallel:
        free_at = {"shared": free_at["dorf1"]}

    def _evaluate(item: dict) -> tuple[float, str | None, dict]:
        """(start_ts, wait_reason, missing); inf = non schedulabile."""
        bin_free = free_at[_bin(item)]
        cost = level_cost(item["gid"], item["to_level"]) or {}
        ready, missing, cap_hit = _res_wait(
            cost, stock, committed, rate, caps, now
        )
        if cap_hit:
            return float("inf"), "storage_cap_exceeded", missing
        if ready == float("inf"):
            return float("inf"), "waiting_resources", missing
        start = max(bin_free, ready)
        reason = "waiting_resources" if ready > bin_free else (
            "queue_busy" if bin_free > now else None
        )
        return start, reason, missing

    def _commit(item: dict, start: float, dur: float) -> None:
        nonlocal sim_mb
        cost = level_cost(item["gid"], item["to_level"]) or {}
        for r in _RES_KEYS:
            committed[r] += float(cost.get(r, 0) or 0)
        free_at[_bin(item)] = start + dur
        if item["gid"] == GID_MAIN_BUILDING:
            # Il Municipio finito accelera i lavori schedulati dopo.
            sim_mb = max(sim_mb, item["to_level"])

    scheduled: list[dict] = []
    budget = max(0, int(limit or 8))

    if parallel:
        streams: dict[str, Iterator] = {
            "dorf1": iter(
                [c for c in sys_items if c["queue"] == "dorf1"]
                + econ_items
            ),
            "dorf2": iter(
                [c for c in sys_items if c["queue"] == "dorf2"]
                + focus_items
            ),
        }
        heads = {q: next(it, None) for q, it in streams.items()}
        while budget > 0 and any(heads.values()):
            best_q = ""
            best_start = float("inf")
            best_eval: tuple = (float("inf"), None, {})
            # Gli step bloccati (cap superata / risorse irraggiungibili)
            # non occupano lo slot: emessi e saltati, il binario avanza.
            progressed = True
            while progressed and budget > 0:
                progressed = False
                for q, item in heads.items():
                    if item is None:
                        continue
                    start, reason, missing = _evaluate(item)
                    if start == float("inf"):
                        dur = _duration_s(
                            item["gid"], item["to_level"], sim_mb, speed
                        )
                        scheduled.append(
                            _sched_entry(
                                item, start=None, duration=dur,
                                wait_reason=reason, missing=missing,
                            )
                        )
                        heads[q] = next(streams[q], None)
                        budget -= 1
                        progressed = True
                        break
                    if start < best_start:
                        best_q, best_start, best_eval = q, start, (
                            start, reason, missing,
                        )
            if not best_q or budget <= 0:
                break
            item = heads[best_q]
            start, reason, missing = best_eval
            dur = _duration_s(
                item["gid"], item["to_level"], sim_mb, speed
            )
            _commit(item, start, dur)
            scheduled.append(
                _sched_entry(
                    item, start=start, duration=dur,
                    wait_reason=reason, missing=missing,
                )
            )
            heads[best_q] = next(streams[best_q], None)
            budget -= 1
    else:
        # Binario singolo (popoli non Romani senza Plus): la sequenza
        # reale e' testa-sistema -> coda prioritaria -> economia.
        merged = sys_items + focus_items + econ_items
        for item in merged:
            if budget <= 0:
                break
            start, reason, missing = _evaluate(item)
            dur = _duration_s(
                item["gid"], item["to_level"], sim_mb, speed
            )
            if start != float("inf"):
                _commit(item, start, dur)
            scheduled.append(
                _sched_entry(
                    item,
                    start=start if start != float("inf") else None,
                    duration=dur,
                    wait_reason=reason,
                    missing=missing,
                )
            )
            budget -= 1

    # In-progress prima (per fine), poi i pianificati per inizio; gli
    # elementi non schedulabili restano in coda alla lista.
    scheduled.sort(
        key=lambda e: (
            e["start_ts"] is None,
            e["start_ts"] if e["start_ts"] is not None else 0.0,
        )
    )
    timeline.extend(scheduled)

    return {
        "generated_at": _iso(now),
        "now_ts": now,
        "server_speed": speed,
        "main_building_level": mb_level,
        "parallel_build": bool(parallel),
        "grain_lock": bool(grain_lock),
        "expansion_freeze": bool(freeze),
        "timeline": timeline,
    }
