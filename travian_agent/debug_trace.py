"""Decision Trace Logger strutturato (debug pipeline end-to-end).

Canale di debug append-only, file-based come il resto della telemetria:

- ``state/debug_trace_<acc>.jsonl`` — un evento JSON per riga. Tipi:
  ``decision`` (radiografia completa di un ciclo del brain: inputs,
  regole valutate, pool candidati), ``decision_simulated`` (stesso
  oggetto prodotto dal dry-run ``POST /api/debug/simulate``),
  ``config_patch`` (diff old->new + warning del validator),
  ``sensor_anomaly`` (letture DOM incoerenti).

- ``GET /api/debug/trace`` (command_server) ne legge la coda per la
  vista Debug della dashboard; ``POST /api/debug/simulate`` produce
  una entry ``decision_simulated`` senza toccare il browser.

Nessuna eccezione deve mai propagarsi: il debug non puo' bloccare il
ciclo dell'agente.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import BASE_DIR

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Rotazione lazy: oltre la soglia il file viene compattato alla coda.
_MAX_BYTES = 512 * 1024
_KEEP_LINES = 300


def _safe_id(account_id: str) -> str:
    return "".join(
        c if c.isalnum() or c in "._-" else "_" for c in str(account_id or "")
    ) or "default"


def trace_path(account_id: str) -> Path:
    return STATE_DIR / f"debug_trace_{_safe_id(account_id)}.jsonl"


def append_trace(account_id: str, event_type: str, data: dict) -> None:
    """Accoda un evento al trace JSONL dell'account (mai eccezioni)."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": round(time.time(), 3),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "type": str(event_type or "event"),
            "account_id": str(account_id or ""),
        }
        if isinstance(data, dict):
            entry.update(data)
        path = trace_path(account_id)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if path.stat().st_size > _MAX_BYTES:
            _compact(path)
    except Exception:  # noqa: BLE001
        logger.debug("append_trace fallito", exc_info=True)


def _compact(path: Path) -> None:
    """Mantiene solo le ultime ``_KEEP_LINES`` righe del trace."""
    try:
        lines = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-_KEEP_LINES:]
        path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )
    except OSError:
        pass


def read_trace(
    account_id: str,
    limit: int = 50,
    types: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    """Ultime ``limit`` entry del trace (ordine cronologico)."""
    path = trace_path(account_id)
    try:
        lines = path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
    except OSError:
        return []
    wanted = set(types) if types else None
    out: list[dict] = []
    for line in lines[-max(1, int(limit) * 4):]:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if wanted and entry.get("type") not in wanted:
            continue
        out.append(entry)
    return out[-max(1, int(limit)):]


def sensor_anomaly(
    account_id: str,
    village: str,
    anomalies: list[str],
    context: dict | None = None,
) -> None:
    """Logga un alert SENSOR_ANOMALY (logger + trace JSONL).

    Da chiamare solo quando l'insieme delle anomalie cambia rispetto
    all'ultima segnalazione (dedup a carico del chiamante) per non
    spammare il file a ogni ciclo.
    """
    if not anomalies:
        return
    logger.warning(
        "SENSOR_ANOMALY [%s][%s] %s",
        account_id or "?",
        village or "?",
        "; ".join(anomalies),
    )
    append_trace(
        account_id,
        "sensor_anomaly",
        {
            "village": str(village or ""),
            "anomalies": list(anomalies),
            "context": dict(context or {}),
        },
    )


def inputs_snapshot(state: Any, settings: Any) -> dict:
    """Radiografia degli input del ciclo decisionale (risorse, coda, flag)."""
    res = getattr(state, "resources", None)
    prod = getattr(res, "production_per_hour", None) or {}
    try:
        from . import builder

        can_out, can_in = builder.queue_slots_free(state, settings)
        freeze = bool(builder.expansion_freeze(state, settings))
        crop_safety = bool(builder.crop_safety_needed(state, settings))
        role = settings.village_role_for(
            getattr(state, "village_name", ""), getattr(state, "village_id", 0)
        )
        vcfg = settings.village_config_for(
            getattr(state, "village_name", ""), getattr(state, "village_id", 0)
        )
        focus_enabled = bool(
            getattr(vcfg, "building_focus_enabled", True)
        ) if vcfg is not None else False
    except Exception:  # noqa: BLE001
        can_out = can_in = None
        freeze = crop_safety = False
        role = "?"
        focus_enabled = None
    return {
        "village": str(getattr(state, "village_name", "") or ""),
        "village_id": int(getattr(state, "village_id", 0) or 0),
        "resources": {
            "wood": int(getattr(res, "wood", 0) or 0),
            "clay": int(getattr(res, "clay", 0) or 0),
            "iron": int(getattr(res, "iron", 0) or 0),
            "crop": int(getattr(res, "crop", 0) or 0),
            "warehouse_cap": int(getattr(res, "warehouse_capacity", 0) or 0),
            "granary_cap": int(getattr(res, "granary_capacity", 0) or 0),
            "net_crop": getattr(res, "crop_balance", None),
        },
        "production_per_hour": {
            str(getattr(k, "value", k)): int(v or 0)
            for k, v in prod.items()
        },
        "queue": {
            "total": len(getattr(state, "build_queue", []) or []),
            "outer_free": can_out,
            "inner_free": can_in,
            "items": [
                {
                    "name": str(getattr(q, "name", "") or ""),
                    "level": int(getattr(q, "level", 0) or 0),
                    "slot_id": int(getattr(q, "slot_id", 0) or 0),
                    "remaining_s": int(
                        getattr(q, "remaining_seconds", 0) or 0
                    ),
                }
                for q in (getattr(state, "build_queue", []) or [])
            ],
        },
        "flags": {
            "freeze_active": freeze,
            "crop_safety": crop_safety,
            "building_focus_enabled": focus_enabled,
            "role": str(role or "standard"),
            "under_raid_pressure": bool(
                getattr(state, "under_raid_pressure", False)
            ),
            "single_village": bool(getattr(state, "single_village", False)),
            "is_capital": any(
                getattr(f, "gid", -1) == 26
                for f in (getattr(state, "fields", []) or [])
                if not getattr(f, "is_resource_field", True)
            ),
            "settlers_home": int(getattr(state, "settlers_home", 0) or 0),
            "incoming_attacks": int(
                getattr(state, "incoming_attacks", 0) or 0
            ),
        },
    }
