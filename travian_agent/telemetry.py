"""Telemetria live per il Command Center web + coda comandi manuali.

Architettura file-based (nessun accoppiamento di processo):

- l'orchestratore scrive ``state/dashboard_state_<acc>.json`` alla fine
  di ogni ciclo ``monitor_all_villages``;
- il mini server HTTP (``command_server.py``) espone lo snapshot via
  ``GET /api/dashboard-state`` e accoda i comandi rapidi della UI su
  ``state/manual_commands_<acc>.json`` via ``POST /api/command``;
- l'agente svuota la coda comandi a ogni ciclo e consuma il flag
  ``state/force_tick_<acc>.flag`` per l'esecuzione immediata.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .building_prereqs import GID_TOWN_HALL
from .config import BASE_DIR, Settings
from .models import GID_PALACE, GameState, ResourceType, Village

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"
HISTORY_DIR = STATE_DIR / "history"

AUDIT_TAIL_LIMIT = 40
# Un comando manuale non consumato entro la TTL decade (l'agente era
# fermo o il ciclo e' piu' lungo del previsto).
COMMAND_TTL_SECONDS = 1800.0
COMMAND_QUEUE_MAX = 50

# Azioni esposte dal pulsantiera del Command Center.
SUPPORTED_COMMANDS = frozenset({
    "START_CELEBRATION",
    "MOVE_HERO",
    "TRIGGER_DODGE",
    "FORCE_TICK",
})

# status del candidato cropper -> label per la dashboard
_TARGET_STATUS_LABELS = {
    "pending": "PENDING",
    "approved": "FREE",
    "rejected": "REJECTED",
    "settling": "SETTLING",
    "settled": "SETTLED",
}

# status hero persistito da HeroManager -> label dashboard
_HERO_STATUS_LABELS = {
    "home": "IN_VILLAGE",
    "in_village": "IN_VILLAGE",
    "moving": "MOVING",
    "adventure": "ON_ADVENTURE",
    "away": "AWAY",
    "dead": "DEAD",
}


def _safe_id(account_id: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)


def telemetry_path(account_id: str) -> Path:
    return STATE_DIR / f"dashboard_state_{_safe_id(account_id)}.json"


def commands_path(account_id: str) -> Path:
    return STATE_DIR / f"manual_commands_{_safe_id(account_id)}.json"


def force_tick_path(account_id: str) -> Path:
    return STATE_DIR / f"force_tick_{_safe_id(account_id)}.flag"


def latest_telemetry_path() -> Path | None:
    """Snapshot piu' recente su disco (usato quando l'account non e' dato)."""
    try:
        files = sorted(
            STATE_DIR.glob("dashboard_state_*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    return files[0] if files else None


# ---------------------------------------------------------------------------
# Audit tail (history_<acc>.jsonl scritto da _record_decision)
# ---------------------------------------------------------------------------

def _audit_tail(account_id: str, limit: int = AUDIT_TAIL_LIMIT) -> list[dict]:
    path = HISTORY_DIR / f"history_{_safe_id(account_id)}.jsonl"
    try:
        lines = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in lines[-limit:]:
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(entry, dict):
            continue
        # Il JSONL e' condiviso con _append_history (righe 'action'
        # senza decision/module): le saltiamo o apparirebbero come
        # duplicati privi di modulo nella dashboard.
        if "decision" not in entry:
            continue
        ts = float(entry.get("ts") or 0.0)
        out.append(
            {
                "time": datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                if ts
                else "",
                "village": str(entry.get("village_name") or ""),
                "module": str(entry.get("module") or ""),
                "decision": str(entry.get("decision") or ""),
                "outcome": str(entry.get("outcome") or ""),
                "reason": str(entry.get("reason") or ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Comandi manuali (POST /api/command -> file di stato)
# ---------------------------------------------------------------------------

def enqueue_command(
    account_id: str,
    action: str,
    village_name: str = "",
    params: dict | None = None,
) -> dict:
    """Accoda un comando rapido della dashboard per il prossimo ciclo.

    ``FORCE_TICK`` viene materializzato come flag file consumato durante
    l'attesa di fine ciclo; gli altri comandi entrano nella coda JSON e
    vengono eseguiti dal dispatcher dell'orchestratore.
    """
    action = str(action or "").strip().upper()
    if action not in SUPPORTED_COMMANDS:
        return {
            "status": "rejected",
            "reason": f"azione '{action}' non supportata",
        }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if action == "FORCE_TICK":
        try:
            force_tick_path(account_id).write_text(
                str(time.time()), encoding="utf-8"
            )
        except OSError as exc:
            return {"status": "error", "reason": str(exc)}
        return {"status": "queued", "action": action, "village": village_name}

    path = commands_path(account_id)
    try:
        queue = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        queue = []
    if not isinstance(queue, list):
        queue = []
    queue.append(
        {
            "action": action,
            "village_name": str(village_name or ""),
            "params": dict(params or {}),
            "enqueued_ts": time.time(),
        }
    )
    queue = queue[-COMMAND_QUEUE_MAX:]
    try:
        path.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "reason": str(exc)}
    return {"status": "queued", "action": action, "village": village_name}


def drain_commands(account_id: str) -> list[dict]:
    """Legge e azzera la coda dei comandi manuali (TTL applicata)."""
    path = commands_path(account_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    try:
        path.write_text("[]", encoding="utf-8")
    except OSError:
        pass
    if not isinstance(raw, list):
        return []
    now = time.time()
    return [
        c
        for c in raw
        if isinstance(c, dict)
        and now - float(c.get("enqueued_ts") or 0.0) < COMMAND_TTL_SECONDS
    ]


# ---------------------------------------------------------------------------
# Snapshot telemetria (GET /api/dashboard-state)
# ---------------------------------------------------------------------------

def _prod(res: Any, rtype: ResourceType) -> int:
    prod = getattr(res, "production_per_hour", None) or {}
    for key in (rtype, rtype.value):
        try:
            val = prod.get(key)
        except AttributeError:
            val = None
        if val:
            return int(val)
    return 0


def _persisted_hero_snapshot(account_id: str) -> dict:
    """Ultimo snapshot eroe persistito (kv DB o file state/hero_<acc>.json).

    ``HeroManager.snapshot`` parte vuoto a ogni avvio del processo e si
    popola solo dopo il primo run del modulo: il fallback legge lo stato
    salvato nei cicli precedenti cosi' la dashboard non resta UNKNOWN.
    """
    safe = _safe_id(account_id)
    try:
        from . import db

        raw = db.kv_read(f"hero_{safe}.json")
        if raw:
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
    except Exception:  # noqa: BLE001
        pass
    try:
        data = json.loads(
            (STATE_DIR / f"hero_{safe}.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def _hero_payload(hero_mgr: Any, account_id: str = "") -> dict:
    snap: dict = {}
    try:
        snap = dict(getattr(hero_mgr, "snapshot", None) or {})
    except Exception:  # noqa: BLE001
        snap = {}
    if not snap and account_id:
        snap = _persisted_hero_snapshot(account_id)
    status = str(snap.get("status") or "").lower()
    return {
        "name": str(snap.get("name") or "Eroe"),
        "level": int(snap.get("level") or 0),
        "health": int(snap.get("health") or 0),
        "xp_percent": int(snap.get("xp_percent") or 0),
        "status": _HERO_STATUS_LABELS.get(status, status.upper() or "UNKNOWN"),
        "current_village": str(snap.get("current_village") or ""),
        "adventures": int(snap.get("adventures") or 0),
        "is_mounted": snap.get("is_mounted"),
        "resource_bonus_type": str(snap.get("resource_bonus_type") or ""),
        "resource_bonus_per_hour": int(
            snap.get("resource_bonus_per_hour") or 0
        ),
    }


def _village_payload(
    village: Village,
    state: GameState | None,
    role: str,
) -> dict:
    res = state.resources if state is not None else None
    queue = []
    if state is not None:
        for item in state.build_queue:
            queue.append(
                {
                    "name": item.name,
                    "target_level": item.level,
                    "seconds_remaining": item.remaining_seconds,
                }
            )
    interiors = state.interior_fields() if state is not None else []
    town_hall_lvl = max(
        (f.level for f in interiors if f.gid == GID_TOWN_HALL), default=0
    )
    role_raw = getattr(village.role, "value", village.role)
    is_capital = str(role_raw).lower() == "capital" or any(
        f.gid == GID_PALACE for f in interiors
    )
    attacks = int(getattr(state, "incoming_attacks", 0) or 0)
    return {
        "id": str(village.village_id or village.name),
        "name": village.name,
        "x": village.x,
        "y": village.y,
        "role": role,
        "is_capital": bool(is_capital),
        "resources": {
            "wood": int(getattr(res, "wood", 0) or 0),
            "clay": int(getattr(res, "clay", 0) or 0),
            "iron": int(getattr(res, "iron", 0) or 0),
            "crop": int(getattr(res, "crop", 0) or 0),
            "warehouse_cap": int(getattr(res, "warehouse_capacity", 0) or 0),
            "granary_cap": int(getattr(res, "granary_capacity", 0) or 0),
            "prod_wood": _prod(res, ResourceType.WOOD) if res else 0,
            "prod_clay": _prod(res, ResourceType.CLAY) if res else 0,
            "prod_iron": _prod(res, ResourceType.IRON) if res else 0,
            "net_crop": int(getattr(res, "crop_balance", None) or 0),
        },
        "incoming_attacks": [{"type": "attack"} for _ in range(attacks)],
        "building_queue": queue,
        "settlers": {
            "trained": int(getattr(state, "settlers_home", 0) or 0),
            "in_queue": 0,
            "seconds_remaining": 0,
        },
        "barracks_queue": [],
        "smithy_queue": None,
        "town_hall": {
            "level": town_hall_lvl,
            "celebration_active": False,
            "seconds_remaining": 0,
        },
        "merchants": {"available": 0, "total": 0, "active_shipments": []},
        "raids": {"enabled": False, "active_count": 0, "next_cycle_seconds": 0},
    }


def export_dashboard_state(
    settings: Settings,
    *,
    villages: list[Village],
    states_by_village: dict[str, GameState],
    expansion_mgr: Any = None,
    hero_mgr: Any = None,
) -> dict:
    """Aggrega lo stato del ciclo e lo scrive su dashboard_state_<acc>.json.

    Ritorna il payload serializzato; in caso di errore di I/O lo
    snapshot viene solo loggato (la telemetria non deve mai bloccare
    il ciclo dell'agente).
    """
    village_items: list[dict] = []
    for v in villages:
        key = v.name or str(v.village_id)
        state = states_by_village.get(key)
        # v.role puo' essere un VillageRole enum: .value -> label pulita
        role = str(getattr(v.role, "value", v.role) or "standard")
        try:
            cfg = settings.village_config_for(v.name, v.village_id)
            if cfg is not None and cfg.role:
                role = str(cfg.role)
        except Exception:  # noqa: BLE001
            pass
        village_items.append(_village_payload(v, state, role))

    # Flag raid dal config farming (disponibile solo a livello account)
    farming_enabled = bool(getattr(settings.farming, "enabled", False))
    for item in village_items:
        item["raids"]["enabled"] = farming_enabled

    targets: list[dict] = []
    try:
        # import tardivo: evita cicli expansion -> telemetry
        from .expansion import cropper_store

        crop_min = int(getattr(settings.expansion, "crop_min", 9) or 9)
        for cand in cropper_store(settings.account_id).list_candidates(
            settings.account_id
        ):
            ctype = str(getattr(cand, "type", "") or "")
            m = re.match(r"(\d+)", ctype)
            crop_fields = int(m.group(1)) if m else 0
            bonus = (
                getattr(cand, "bonus_crop_percent", 0)
                or getattr(cand, "bonus", 0)
                or 0
            )
            targets.append(
                {
                    "x": cand.x,
                    "y": cand.y,
                    "type": ctype,
                    "crop_fields": crop_fields,
                    "bonus": f"{bonus}%" if bonus else "",
                    "status": _TARGET_STATUS_LABELS.get(
                        str(cand.status or ""), "UNKNOWN"
                    ),
                    "below_min": 0 < crop_fields < crop_min,
                }
            )
        # I tile con piu' campi grano prima: il ripiego del bot deve
        # cadere sul 9c, non su un 6c sotto soglia.
        targets.sort(key=lambda t: t["crop_fields"], reverse=True)
    except Exception:  # noqa: BLE001
        targets = []

    # CP e feed target: ExpansionManager e' la fonte primaria, ma i
    # valori possono non essere ancora stati letti in questo processo —
    # in quel caso vale il GameState del ciclo (sensors.read_culture_points).
    cp_current = int(getattr(expansion_mgr, "cp_current", 0) or 0)
    cp_required = int(getattr(expansion_mgr, "cp_required", 0) or 0)
    cp_daily = int(getattr(expansion_mgr, "cp_daily", 0) or 0)
    feed_target = str(getattr(expansion_mgr, "feed_target", "") or "")
    for st in states_by_village.values():
        if not cp_current:
            cp_current = int(getattr(st, "culture_points", 0) or 0)
        if not cp_required:
            cp_required = int(getattr(st, "culture_points_required", 0) or 0)
        if not cp_daily:
            cp_daily = int(getattr(st, "culture_points_daily", 0) or 0)
        if not feed_target:
            feed_target = str(getattr(st, "feed_target", "") or "")
        if cp_current and cp_required and feed_target:
            break
    founder = str(settings.expansion.founder_village or feed_target or "")

    payload = {
        "timestamp": datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        "account_id": settings.account_id,
        "account": {
            "cp_current": cp_current,
            "cp_required": cp_required,
            "cp_daily_production": cp_daily,
            "founder_village": founder,
            "feed_target": feed_target,
            "expansion_targets": targets,
        },
        "hero": _hero_payload(hero_mgr, account_id=settings.account_id),
        "villages": village_items,
        "audit_logs": _audit_tail(settings.account_id),
    }

    path = telemetry_path(settings.account_id)
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
    except OSError:
        logger.debug("Scrittura snapshot telemetria fallita", exc_info=True)
    return payload
