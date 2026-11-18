"""Mini server HTTP per il Command Center live.

Espone su una porta dedicata (default 8610):

- ``GET  /api/dashboard-state?account=<id>`` — ultimo snapshot di
  telemetria scritto dall'orchestratore (``state/dashboard_state_*.json``);
- ``POST /api/command`` — accoda un comando rapido per l'agente
  (``{"action","village_name","params","account_id"}``);
- ``GET  /command-center[/...]`` — serve i file statici di
  ``web/command_center`` (index.html).

Viene avviato in un thread daemon da ``app.py`` (dashboard Streamlit)
e opzionalmente da ``main.py``: l'eventuale bind fallito su porta gia'
occupata viene ignorato — vale il primo server attivo.
"""

from __future__ import annotations

import ctypes
import json
import logging
import mimetypes
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import ACCOUNTS_FILE, BASE_DIR
from . import debug_trace
from .telemetry import (
    SUPPORTED_COMMANDS,
    enqueue_command,
    latest_telemetry_path,
    telemetry_path,
)
from .version import get_agent_version

logger = logging.getLogger(__name__)

WEB_DIR = BASE_DIR / "web" / "command_center"
COMMAND_PORT = 8610
STATE_DIR = BASE_DIR / "state"

# Azioni di controllo worker gestite direttamente dal server (file flag
# + PID file, stesso meccanismo della dashboard Streamlit): NON vengono
# accodate all'agente perche' l'orchestratore le legge a ogni ciclo.
WORKER_ACTIONS = frozenset({
    "PAUSE_ALL",
    "RESUME_ALL",
    "STOP_PID",
    "START_ALL",
    "LOGIN",
})

_server_started = False
_start_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Controllo worker via file flag / PID file (condiviso con app.py e main.py)
# ---------------------------------------------------------------------------
def _load_accounts_safe() -> list:
    try:
        from .config import load_accounts

        return load_accounts()
    except Exception:  # noqa: BLE001
        return []


def _configured_account_ids() -> list[str]:
    return [str(a.account_id) for a in _load_accounts_safe()]


def _pid_alive(pid: int) -> bool:
    """True se il processo con quel PID esiste (senza inviare segnali)."""
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _running_agents() -> dict[str, int]:
    """account_id -> pid per i PID file vivi in state/agent_*.pid."""
    found: dict[str, int] = {}
    if not STATE_DIR.exists():
        return found
    for f in STATE_DIR.glob("agent_*.pid"):
        acc_id = f.stem[len("agent_"):]
        try:
            pid = int(f.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            f.unlink(missing_ok=True)
            continue
        if _pid_alive(pid):
            found[acc_id] = pid
        else:
            f.unlink(missing_ok=True)
    return found


def _worker_rows() -> dict:
    """Stato worker per la UI: pid, running, paused e sessione."""
    running = _running_agents()
    rows = []
    for acc_id in _configured_account_ids():
        pid = running.get(acc_id)
        session_path = STATE_DIR / f"session_{acc_id}.json"
        session_exists = session_path.exists()
        session_age = None
        if session_exists:
            try:
                session_age = max(
                    0, int(time.time() - session_path.stat().st_mtime)
                )
            except OSError:
                session_age = None
        rows.append({
            "account_id": acc_id,
            "pid": pid,
            "running": pid is not None,
            "paused": (STATE_DIR / f"pause_{acc_id}.flag").exists(),
            "session_valid": session_exists,
            "session_age": session_age,
        })
    return {
        "workers": rows,
        "running_count": sum(1 for r in rows if r["running"]),
        "paused": any(r["paused"] for r in rows),
    }


def _pause_flag(acc_id: str, paused: bool) -> None:
    f = STATE_DIR / f"pause_{acc_id}.flag"
    if paused:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        f.write_text(str(datetime.now()), encoding="utf-8")
    else:
        f.unlink(missing_ok=True)


def _stop_flag(acc_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"stop_{acc_id}.flag").write_text(
        str(datetime.now()), encoding="utf-8"
    )


def _start_agent(acc_id: str, options: dict | None = None) -> bool:
    """Avvia `main.py --account <id>` dedicato se non gia' in esecuzione.

    `options` (opzionali, dal pannello Controllo della dashboard):
    - `brain`: 'rules' | 'llm' (default rules)
    - `headless`: True (default) -> browser nascosto; False -> --headed
    - `once`: True -> --once (una sola iterazione, test rapido)
    """
    if acc_id in _running_agents():
        return False
    options = options or {}
    brain = str(options.get("brain") or "rules")
    cmd = [sys.executable, "main.py", "--account", acc_id, "--brain", brain]
    if not options.get("headless", True):
        cmd.append("--headed")
    if options.get("once"):
        cmd.append("--once")
    try:
        log_f = open(STATE_DIR / f"agent_{acc_id}.log", "a", encoding="utf-8")
        log_f.write(f"\n--- AVVIO AGENTE {acc_id}: {datetime.now()} ---\n")
        log_f.flush()
        subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            ),
        )
    except OSError as exc:
        logger.warning("Avvio agente %s fallito: %s", acc_id, exc)
        return False
    return True


def _handle_worker_action(action: str, body: dict) -> dict:
    """Esegue PAUSE/RESUME/STOP/START sui worker senza accodare all'agente.

    `params.target` puo' essere 'all' o un account_id singolo.
    """
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    target = str(params.get("target") or body.get("account_id") or "all")
    account_ids = _configured_account_ids()
    targets = account_ids if target in ("", "all") else [target]
    if not targets:
        return {"status": "rejected", "reason": "nessun account configurato"}

    if action == "PAUSE_ALL":
        for acc in targets:
            _pause_flag(acc, True)
        return {"status": "queued", "action": action, "targets": targets}
    if action == "RESUME_ALL":
        for acc in targets:
            _pause_flag(acc, False)
        return {"status": "queued", "action": action, "targets": targets}
    if action == "STOP_PID":
        stopped = [a for a in targets if a in _running_agents() or a in account_ids]
        for acc in stopped:
            _stop_flag(acc)
        return {"status": "queued", "action": action, "targets": stopped}
    if action == "START_ALL":
        started = [a for a in targets if _start_agent(a, params)]
        return {
            "status": "queued",
            "action": action,
            "targets": started,
            "skipped": [a for a in targets if a not in started],
        }
    if action == "LOGIN":
        # Login assistito: apre main.py --login in console separata per
        # catturare cookie/sessione dell'account (spec pagina Controllo).
        opened = []
        for acc in targets:
            try:
                subprocess.Popen(
                    [sys.executable, "main.py", "--login", "--account", acc],
                    cwd=str(BASE_DIR),
                    creationflags=(
                        subprocess.CREATE_NEW_CONSOLE
                        if sys.platform == "win32"
                        else 0
                    ),
                )
                opened.append(acc)
            except OSError as exc:
                logger.warning("Login assistito %s fallito: %s", acc, exc)
        return {
            "status": "queued",
            "action": action,
            "targets": opened,
        }
    return {"status": "rejected", "reason": f"azione '{action}' non gestita"}


# ---------------------------------------------------------------------------
# Config API: lettura/scrittura accounts.json (hot-reload dall'agente)
# ---------------------------------------------------------------------------
_CONFIG_SECTIONS = {
    "farming": "farming",
    "scout_brain": "scout_brain",
    "hero": "hero",
    "defense": "defense",
    "replenishment": "replenishment",
    "expansion": "expansion",
}

_ACCOUNT_FIELDS = {
    "username", "password", "server_url", "headless", "tribe",
    "strategy_profile", "loop_interval_min", "loop_interval_max",
    "action_delay_min", "action_delay_max", "max_build_queue",
    "max_resource_queue", "max_interior_queue", "allow_parallel_build",
    "auto_video_bonus", "min_net_crop_threshold", "builder_enabled",
}


def _account_config(acc) -> dict:
    """Config serializzabile di un Settings (nessun Path oggetto)."""
    return {
        "account_id": acc.account_id,
        "server_url": acc.server_url,
        "username": acc.username,
        "tribe": acc.tribe,
        "server_speed": float(getattr(acc, "server_speed", 1.0) or 1.0),
        "headless": acc.headless,
        "strategy_profile": acc.strategy_profile,
        "loop_interval_min": acc.loop_interval_min,
        "loop_interval_max": acc.loop_interval_max,
        "min_net_crop_threshold": acc.min_net_crop_threshold,
        "builder_enabled": acc.builder_enabled,
        "farming": asdict(acc.farming),
        "scout_brain": asdict(acc.scout_brain),
        "hero": asdict(acc.hero),
        "defense": asdict(acc.defense),
        "replenishment": asdict(acc.replenishment),
        "expansion": asdict(acc.expansion),
        "villages": {k: asdict(v) for k, v in acc.villages.items()},
        "village_roles": dict(acc.village_roles),
    }


def _coerce_value(new, cur):
    """Converte il valore JSON nel tipo del campo esistente."""
    if isinstance(cur, bool):
        if isinstance(new, bool):
            return new
        return str(new).strip().lower() in ("1", "true", "yes", "on")
    if isinstance(cur, int) and not isinstance(cur, bool):
        try:
            return int(float(new))
        except (TypeError, ValueError):
            return cur
    if isinstance(cur, float):
        try:
            return float(new)
        except (TypeError, ValueError):
            return cur
    if isinstance(cur, list):
        return list(new) if isinstance(new, (list, tuple)) else cur
    if isinstance(cur, dict):
        return dict(new) if isinstance(new, dict) else cur
    if cur is None:
        return new
    return new


def _coerce_checked(new, cur) -> tuple:
    """Come ``_coerce_value`` ma ritorna (valore, ok, nota_errore).

    Lo schema validator lo usa per segnalare le conversioni di tipo
    fallite, che altrimenti silenzierebbero il mantenimento del vecchio
    valore.
    """
    if isinstance(cur, bool):
        if isinstance(new, bool):
            return new, True, ""
        s = str(new).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True, True, ""
        if s in ("0", "false", "no", "off"):
            return False, True, ""
        return cur, False, f"'{new}' non interpretabile come bool"
    if isinstance(cur, int) and not isinstance(cur, bool):
        try:
            return int(float(new)), True, ""
        except (TypeError, ValueError):
            return cur, False, f"'{new}' non convertibile in int"
    if isinstance(cur, float):
        try:
            return float(new), True, ""
        except (TypeError, ValueError):
            return cur, False, f"'{new}' non convertibile in float"
    if isinstance(cur, list):
        if isinstance(new, (list, tuple)):
            return list(new), True, ""
        return cur, False, f"'{new}' non e' una lista"
    if isinstance(cur, dict):
        if isinstance(new, dict):
            return dict(new), True, ""
        return cur, False, f"'{new}' non e' un oggetto"
    if cur is None:
        return new, True, ""
    return new, True, ""


def _apply_fields(obj, patch: dict) -> dict:
    """Applica il patch ai soli campi del dataclass; ritorna le chiavi usate."""
    applied, _diff, _warnings = _apply_fields_checked(obj, patch)
    return applied


def _apply_fields_checked(
    obj, patch: dict, allowed: set | None = None
) -> tuple[dict, dict, list[str]]:
    """Applica il patch validando contro il dataclass target.

    Ritorna ``(applied, diff, warnings)``:
    - ``applied``: chiave -> nuovo valore effettivamente impostato;
    - ``diff``: chiave -> {"old", "new"} per i soli campi cambiati;
    - ``warnings``: chiavi sconosciute (prima ignorate in silenzio) e
      conversioni di tipo fallite. ``allowed`` restringe le chiavi
      ammesse (es. sezione 'account' -> _ACCOUNT_FIELDS).
    """
    applied: dict = {}
    diff: dict = {}
    warnings: list[str] = []
    fields = getattr(obj, "__dataclass_fields__", {})
    for k, v in patch.items():
        if k not in fields or (allowed is not None and k not in allowed):
            warnings.append(f"chiave '{k}' sconosciuta: ignorata")
            continue
        cur = getattr(obj, k)
        new, ok, note = _coerce_checked(v, cur)
        if not ok:
            warnings.append(
                f"campo '{k}': {note} (mantenuto {cur!r})"
            )
            continue
        setattr(obj, k, new)
        applied[k] = new
        if new != cur:
            diff[k] = {"old": cur, "new": new}
    return applied, diff, warnings


def _handle_config_patch(body: dict) -> tuple[dict, int]:
    """POST /api/config — sezioni: village | account | <modulo>.

    {"account_id", "section", "patch", "village"?} — la scrittura passa
    per save_accounts (atomic tmp+rename + .bak). L'agente rilegge
    accounts.json a ogni ciclo: niente riavvio.
    """
    from .config import ExpansionTarget, Settings, VillageConfig, load_accounts, save_accounts

    account_id = str(body.get("account_id") or "")
    section = str(body.get("section") or "").strip().lower()
    patch = body.get("patch")
    if not isinstance(patch, dict):
        return {"status": "rejected", "reason": "patch mancante"}, 400

    accounts = load_accounts()

    if section == "account_new":
        # Crea un nuovo account: account_id univoco obbligatorio.
        new_id = str(patch.get("account_id") or "").strip()
        server_url = str(patch.get("server_url") or "").strip()
        if not new_id or not server_url:
            return (
                {"status": "rejected",
                 "reason": "account_id e server_url obbligatori"},
                400,
            )
        if any(a.account_id == new_id for a in accounts):
            return (
                {"status": "rejected",
                 "reason": f"account '{new_id}' esiste gia'"},
                409,
            )
        new_acc = Settings(
            server_url=server_url.rstrip("/"),
            account_id=new_id,
            username=str(patch.get("username") or ""),
            password=str(patch.get("password") or ""),
            tribe=str(patch.get("tribe") or "").strip().lower(),
        )
        accounts.append(new_acc)
        save_accounts(accounts)
        # Compat: se e' il primo account, aggiorna anche .env
        if len(accounts) == 1:
            try:
                from .env import save_env_settings
                save_env_settings({
                    "TRAVIAN_SERVER_URL": new_acc.server_url,
                    "TRAVIAN_USERNAME": new_acc.username,
                    "TRAVIAN_PASSWORD": new_acc.password,
                })
            except Exception:  # noqa: BLE001
                pass
        return {"status": "saved", "account_id": new_id, "section": section}, 200

    if section == "account_delete":
        # Rimozione esplicita: consentito svuotare la lista (spec Account).
        victim = next(
            (a for a in accounts if a.account_id == account_id), None
        )
        if victim is None:
            return (
                {"status": "rejected",
                 "reason": f"account '{account_id}' non trovato"},
                404,
            )
        remaining = [a for a in accounts if a.account_id != account_id]
        save_accounts(remaining, allow_empty=True)
        try:
            sf = getattr(victim, "state_file", None)
            if sf is not None and sf.exists():
                sf.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return {
            "status": "deleted",
            "account_id": account_id,
            "section": section,
        }, 200

    acc = next((a for a in accounts if a.account_id == account_id), None)
    if acc is None:
        return (
            {"status": "rejected", "reason": f"account '{account_id}' non trovato"},
            404,
        )

    applied: dict = {}
    diff: dict = {}
    warnings: list[str] = []
    patch_village = ""
    if section == "village":
        vname = str(body.get("village") or "").strip()
        if not vname:
            return {"status": "rejected", "reason": "village mancante"}, 400
        patch_village = vname
        cfg = acc.villages.get(vname)
        if cfg is None:
            cfg = next(
                (c for c in acc.villages.values() if c.name == vname), None
            )
        if cfg is None:
            cfg = VillageConfig(name=vname)
            acc.villages[vname] = cfg
        applied, diff, warnings = _apply_fields_checked(cfg, patch)
        # le personalizzazioni manuali non vengono sovrascritte dai preset
        cfg.custom_modified = True
    elif section == "account":
        applied, diff, warnings = _apply_fields_checked(
            acc, patch, allowed=_ACCOUNT_FIELDS
        )
    elif section in _CONFIG_SECTIONS:
        cfg = getattr(acc, _CONFIG_SECTIONS[section])
        if section == "expansion" and "target_slots" in patch:
            # lista di ExpansionTarget, non dict generici
            old_slots = [asdict(t) for t in cfg.target_slots]
            cfg.target_slots = [
                ExpansionTarget(
                    x=int(t.get("x", 0)),
                    y=int(t.get("y", 0)),
                    note=str(t.get("note", "")),
                )
                for t in (patch.get("target_slots") or [])
                if isinstance(t, dict)
            ]
            applied["target_slots"] = [asdict(t) for t in cfg.target_slots]
            if applied["target_slots"] != old_slots:
                diff["target_slots"] = {
                    "old": old_slots,
                    "new": applied["target_slots"],
                }
            patch = {k: v for k, v in patch.items() if k != "target_slots"}
        _app, _dif, _warn = _apply_fields_checked(cfg, patch)
        applied.update(_app)
        diff.update(_dif)
        warnings.extend(_warn)
    else:
        return (
            {"status": "rejected", "reason": f"sezione '{section}' sconosciuta"},
            400,
        )

    if not applied:
        # Patch con soli campi sconosciuti (es. server avviato con codice
        # vecchio: il dataclass non ha ancora il campo). Rifiutare e'
        # piu' onesto che un 'saved' che non ha persistito nulla.
        for w in warnings:
            logger.warning(
                "Config patch %s/%s: %s", account_id, section, w
            )
        return (
            {
                "status": "rejected",
                "reason": "nessun campo valido nel patch: riavvia "
                "l'app per caricare il codice aggiornato",
                "warnings": warnings,
            },
            400,
        )

    # Config Diff & Schema Validator (debug trace): chiavi sconosciute
    # inviate dalla UI e coercion fallite non devono piu' sparire nel
    # silenzio — WARNING nel log + evento strutturato su JSONL.
    for w in warnings:
        logger.warning(
            "Config patch %s/%s: %s", account_id, section, w
        )
    try:
        debug_trace.append_trace(
            account_id,
            "config_patch",
            {
                "section": section,
                "village": patch_village,
                "diff": diff,
                "applied": applied,
                "warnings": warnings,
            },
        )
    except Exception:  # noqa: BLE001
        pass

    save_accounts(accounts)

    if section == "expansion":
        # Mirror runtime per l'agente (spec: state/expansion_config.json).
        try:
            _exp_cfg_path = STATE_DIR / "expansion_config.json"
            _exp_cfg_path.parent.mkdir(parents=True, exist_ok=True)
            _exp_cfg: dict = {}
            if _exp_cfg_path.exists():
                _exp_cfg = json.loads(
                    _exp_cfg_path.read_text(encoding="utf-8")
                )
                if not isinstance(_exp_cfg, dict):
                    _exp_cfg = {}
            _primary = (
                acc.expansion.target_slots[0]
                if acc.expansion.target_slots
                else None
            )
            _exp_cfg[account_id] = {
                "auto_expansion_enabled": bool(acc.expansion.auto_expand),
                "source_village": acc.expansion.founder_village,
                "expansion_slot": int(acc.expansion.expansion_slot),
                "target_coordinates": {
                    "x": _primary.x if _primary else 0,
                    "y": _primary.y if _primary else 0,
                },
            }
            _exp_cfg_path.write_text(
                json.dumps(_exp_cfg, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001
            logger.debug("Mirror expansion_config.json fallito", exc_info=True)

    return {
        "status": "saved",
        "account_id": account_id,
        "section": section,
        "applied": applied,
        "diff": diff,
        "warnings": warnings,
    }, 200


# ---------------------------------------------------------------------------
# Farm Targets & Cropper Candidates (human-in-the-loop, spec Farming/Espansione)
# ---------------------------------------------------------------------------
def _farm_targets_payload(account_id: str) -> dict:
    from .farm_store import farm_target_store

    if not account_id:
        return {"targets": []}
    store = farm_target_store(account_id)
    rows = store.list_targets()
    counts = {"pending": 0, "approved": 0, "rejected": 0, "blacklisted": 0}
    out = []
    for t in rows:
        counts[t.status] = counts.get(t.status, 0) + 1
        out.append({
            "id": t.id, "x": t.x, "y": t.y, "name": t.name,
            "is_oasis": t.is_oasis, "population": t.population,
            "estimated_loot": t.estimated_loot,
            "last_loot_ratio": t.last_loot_ratio,
            "distance": t.distance, "nearest_village": t.nearest_village,
            "status": t.status, "manual": t.manual,
            "last_raid_ts": t.last_raid_ts, "last_outcome": t.last_outcome,
            "fail_reason": t.fail_reason,
        })
    return {"targets": out, "counts": counts}


def _handle_farm_target_patch(body: dict) -> tuple[dict, int]:
    """Azioni sui Custom Farm Targets: approve/reject/restore/add."""
    from .farm_store import farm_target_store

    account_id = str(body.get("account_id") or "").strip()
    if not account_id:
        return {"status": "rejected", "reason": "account_id mancante"}, 400
    op = str(body.get("op") or "").strip().lower()
    store = farm_target_store(account_id)

    if op == "add":
        try:
            x = int(body.get("x", 0))
            y = int(body.get("y", 0))
        except (TypeError, ValueError):
            return {"status": "rejected", "reason": "x/y non validi"}, 400
        if store.status_at(x, y) is not None:
            return (
                {"status": "rejected",
                 "reason": f"({x}|{y}) gia' in catalogo"},
                409,
            )
        tgt = store.add_manual_target(
            x, y,
            name=str(body.get("name") or "").strip(),
            is_oasis=bool(body.get("is_oasis")),
            status="approved",
            nearest_village=str(body.get("nearest_village") or ""),
        )
        if tgt is None:
            return {"status": "rejected", "reason": "duplicato"}, 409
        return {
            "status": "saved", "op": op,
            "target": asdict(tgt),
        }, 200

    tid = str(body.get("target_id") or "").strip()
    if not tid:
        return {"status": "rejected", "reason": "target_id mancante"}, 400
    status_map = {
        "approve": "approved", "reject": "rejected",
        "blacklist": "blacklisted", "restore": "pending",
    }
    new_status = status_map.get(op)
    if new_status is None:
        return {"status": "rejected", "reason": f"op '{op}' non valida"}, 400
    if not store.set_status(tid, new_status):
        return {"status": "rejected", "reason": "target non trovato"}, 404
    return {"status": "saved", "op": op, "target_id": tid}, 200


def _cropper_candidates_payload(account_id: str) -> dict:
    from .cropper import CropperStore

    if not account_id:
        return {"candidates": []}
    store = CropperStore()
    rows = store.list_candidates(account_id)
    out = []
    for c in rows:
        out.append({
            "id": c.id, "x": c.x, "y": c.y, "type": c.type,
            "bonus_crop_percent": c.bonus_crop_percent,
            "distance_hours": c.distance_hours,
            "nearest_village": c.nearest_village,
            "oases": c.oases, "status": c.status,
        })
    return {"candidates": out}


def _handle_cropper_patch(body: dict) -> tuple[dict, int]:
    """Azioni sui cropper candidates: approve/reject."""
    from .cropper import CropperStore

    account_id = str(body.get("account_id") or "").strip()
    if not account_id:
        return {"status": "rejected", "reason": "account_id mancante"}, 400
    op = str(body.get("op") or "").strip().lower()
    cid = str(body.get("candidate_id") or "").strip()
    if not cid:
        return {"status": "rejected", "reason": "candidate_id mancante"}, 400
    store = CropperStore()
    status_map = {"approve": "approved", "reject": "rejected"}
    new_status = status_map.get(op)
    if new_status is None:
        return {"status": "rejected", "reason": f"op '{op}' non valida"}, 400
    if not store.set_status(account_id, cid, new_status):
        return {"status": "rejected", "reason": "candidato non trovato"}, 404
    return {"status": "saved", "op": op, "candidate_id": cid}, 200


def _read_snapshot(account_id: str = "") -> tuple[dict | None, int]:
    """Snapshot JSON per l'account richiesto o il piu' recente."""
    path = None
    if account_id:
        candidate = telemetry_path(account_id)
        if candidate.exists():
            path = candidate
    if path is None:
        path = latest_telemetry_path()
    if path is None:
        return None, 503
    try:
        return json.loads(path.read_text(encoding="utf-8")), 200
    except (OSError, ValueError):
        return None, 500


# ---------------------------------------------------------------------------
# Dry-Run "Simula Ciclo" (debug pipeline): brain.decide senza browser
# ---------------------------------------------------------------------------
_RES_GID_TO_TYPE = {1: "wood", 2: "clay", 3: "iron", 4: "crop"}


def _state_from_snapshot(snap: dict, village_name: str = ""):
    """Ricostruisce un GameState dal payload dashboard_state_<acc>.json.

    Approssimazioni note: gli item di coda non hanno slot_id nello
    snapshot (0 = sconosciuto) e incoming_moves sono riassunti dal
    conteggio attacchi. Sufficiente per valutare le regole del brain.
    """
    from .models import (
        BuildQueueItem,
        Field,
        GameState,
        ResourceType,
        Resources,
    )

    villages = snap.get("villages") or []
    target = next(
        (
            v for v in villages
            if not village_name
            or str(v.get("name") or "") == village_name
            or str(v.get("id") or "") == village_name
        ),
        villages[0] if villages else None,
    )
    if target is None:
        return None

    state = GameState()
    vres = target.get("resources") or {}
    prod_map = {
        "wood": ResourceType.WOOD,
        "clay": ResourceType.CLAY,
        "iron": ResourceType.IRON,
    }
    state.resources = Resources(
        wood=int(vres.get("wood") or 0),
        clay=int(vres.get("clay") or 0),
        iron=int(vres.get("iron") or 0),
        crop=int(vres.get("crop") or 0),
        warehouse_capacity=int(vres.get("warehouse_cap") or 0),
        granary_capacity=int(vres.get("granary_cap") or 0),
        production_per_hour={
            prod_map[k]: int(vres.get(f"prod_{k}") or 0) for k in prod_map
        },
        crop_balance=(
            int(vres["net_crop"]) if vres.get("net_crop") is not None else None
        ),
    )

    # "fields" = i 18 campi dorf1 (fonte primaria, spec slot/gid/level);
    # "slots" copre gli interni dorf2 e gli snapshot pre-'fields'.
    field_rows = list(target.get("fields") or [])
    slot_rows = list(target.get("slots") or [])
    if field_rows:
        seen = {int(s.get("slot") or 0) for s in field_rows}
        slot_rows = field_rows + [
            s for s in slot_rows if int(s.get("slot") or 0) not in seen
        ]
    for s in slot_rows:
        slot_id = int(s.get("slot") or s.get("slot_id") or 0)
        gid = int(s.get("gid", -1) if s.get("gid") is not None else -1)
        rtype = None
        if slot_id <= 18:
            # Campi dorf1: gid risorsa 1-4 (letto dal DOM dei sensori),
            # il resource_type ne deriva per la logica economica.
            rtype = (
                ResourceType(_RES_GID_TO_TYPE[gid])
                if gid in _RES_GID_TO_TYPE
                else None
            )
        state.fields.append(
            Field(
                slot_id=slot_id,
                name=str(s.get("name") or ""),
                level=int(s.get("level") or 0),
                resource_type=rtype,
                gid=gid,
            )
        )

    for q in target.get("building_queue") or []:
        q_slot = int(q.get("slot_id") or q.get("slot") or 0)
        q_gid = int(q.get("gid") or 0)
        q_outer = q.get("is_resource_field")
        if q_outer is None:
            # Snapshot vecchi senza il flag: binario da gid/slot,
            # mai dalla posizione nell'array.
            if q_gid > 0:
                q_outer = q_gid <= 4
            else:
                q_outer = 1 <= q_slot <= 18
        state.build_queue.append(
            BuildQueueItem(
                name=str(q.get("name") or ""),
                level=int(q.get("target_level") or 0),
                remaining_seconds=int(q.get("seconds_remaining") or 0),
                is_resource_field=bool(q_outer),
                slot_id=q_slot,
                gid=q_gid,
            )
        )

    state.incoming_attacks = len(target.get("incoming_attacks") or [])
    state.village_name = str(target.get("name") or "")
    try:
        state.village_id = int(target.get("id") or 0)
    except (TypeError, ValueError):
        state.village_id = 0
    state.settlers_home = int(
        (target.get("settlers") or {}).get("trained") or 0
    )
    state.single_village = len(villages) <= 1
    state.under_raid_pressure = any(
        str(m.get("type") or "") in ("attack", "raid", "unknown")
        for m in (target.get("incoming_attacks") or [])
        if isinstance(m, dict)
    )

    acc = snap.get("account") or {}
    state.culture_points = int(acc.get("cp_current") or 0)
    state.culture_points_required = int(acc.get("cp_required") or 0)
    state.culture_points_daily = int(acc.get("cp_daily_production") or 0)
    state.feed_target = str(acc.get("feed_target") or "")

    for peer in villages:
        if peer is target:
            continue
        pres = peer.get("resources") or {}
        state.peer_villages[str(peer.get("name") or "")] = Resources(
            wood=int(pres.get("wood") or 0),
            clay=int(pres.get("clay") or 0),
            iron=int(pres.get("iron") or 0),
            crop=int(pres.get("crop") or 0),
            warehouse_capacity=int(pres.get("warehouse_cap") or 0),
            granary_capacity=int(pres.get("granary_cap") or 0),
        )
    return state


def _handle_debug_simulate(body: dict) -> tuple[dict, int]:
    """POST /api/debug/simulate — dry-run di brain.decide su snapshot.

    {"account_id", "village"?} -> esegue l'intero albero decisionale in
    memoria (nessun browser) e ritorna il trace completo: inputs,
    regole valutate con motivi di skip, pool candidati, azione finale.
    """
    from .brain import RuleBasedBrain
    from .config import load_accounts

    account_id = str(body.get("account_id") or "") or _default_account_id()
    village = str(body.get("village") or "").strip()
    if not account_id:
        return {"status": "rejected", "reason": "account_id mancante"}, 400

    snap, _status = _read_snapshot(account_id)
    if snap is None:
        return (
            {
                "status": "rejected",
                "reason": "nessuno snapshot: serve almeno un ciclo "
                "dell'agente per simulare",
            },
            503,
        )
    state = _state_from_snapshot(snap, village)
    if state is None:
        return (
            {
                "status": "rejected",
                "reason": f"villaggio '{village}' non trovato nello snapshot",
            },
            404,
        )

    settings = next(
        (a for a in load_accounts() if a.account_id == account_id), None
    )
    if settings is None:
        return (
            {"status": "rejected",
             "reason": f"account '{account_id}' non in accounts.json"},
            404,
        )

    brain = RuleBasedBrain(settings)
    brain.trace_tag = "simulate"
    try:
        action = brain.decide(state)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Simula ciclo fallita", exc_info=True)
        return {"status": "error", "reason": str(exc)}, 500

    return (
        {
            "status": "ok",
            "account_id": account_id,
            "village": state.village_name,
            "simulated": True,
            "server_version": get_agent_version(),
            "action": {
                "type": action.type.value,
                "reason": action.reason,
                "params": action.params,
            },
            "trace": brain.last_trace,
        },
        200,
    )


def _default_account_id() -> str:
    """Account id dello snapshot piu' recente (per i POST senza campo).

    Fallback su accounts.json quando l'agente non ha ancora prodotto
    il primo snapshot: i comandi (es. FORCE_TICK) devono funzionare
    anche a freddo.
    """
    path = latest_telemetry_path()
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            acc = str(data.get("account_id") or "")
            if acc:
                return acc
        except (OSError, ValueError):
            pass
    try:
        from .config import load_accounts

        accounts = load_accounts()
        if accounts:
            return str(accounts[0].account_id or "")
    except Exception:
        pass
    return ""


class _CommandCenterHandler(BaseHTTPRequestHandler):
    server_version = "TravianCommandCenter/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("command_server: " + fmt, *args)

    # ------------------------------------------------------------------
    # Helpers risposta
    # ------------------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type"
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, status=404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Verbi HTTP
    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/api/workers":
            self._send_json(_worker_rows())
            return
        if route == "/api/accounts":
            self._send_json({
                "accounts": [
                    {
                        "account_id": a.account_id,
                        "server_url": a.server_url,
                        "tribe": a.tribe,
                        "username": a.username,
                        "strategy_profile": a.strategy_profile,
                        "headless": a.headless,
                    }
                    for a in _load_accounts_safe()
                ]
            })
            return
        if route == "/api/accounts.json":
            if ACCOUNTS_FILE.exists():
                self._send_file(ACCOUNTS_FILE)
            else:
                self._send_json({"error": "accounts.json non trovato"}, status=404)
            return
        if route == "/api/config":
            account = (parse_qs(parsed.query).get("account") or [""])[0]
            accs = _load_accounts_safe()
            target = next(
                (a for a in accs if a.account_id == account),
                accs[0] if accs else None,
            )
            if target is None:
                self._send_json(
                    {"status": "empty", "reason": "nessun account configurato"},
                    status=404,
                )
            else:
                self._send_json(_account_config(target))
            return
        if route == "/api/farm-targets":
            account = (parse_qs(parsed.query).get("account") or [""])[0]
            self._send_json(_farm_targets_payload(account))
            return
        if route == "/api/cropper-candidates":
            account = (parse_qs(parsed.query).get("account") or [""])[0]
            self._send_json(_cropper_candidates_payload(account))
            return
        if route == "/api/dashboard-state":
            account = (parse_qs(parsed.query).get("account") or [""])[0]
            payload, status = _read_snapshot(account)
            if payload is None:
                self._send_json(
                    {
                        "status": "waiting",
                        "reason": "nessuno snapshot disponibile: "
                        "l'agente non ha ancora completato un ciclo",
                    },
                    status=status,
                )
            else:
                payload["server_version"] = get_agent_version()
                self._send_json(payload)
            return
        if route in ("", "/", "/command-center"):
            self._send_file(WEB_DIR / "index.html")
            return
        if route.startswith("/command-center"):
            rel = route[len("/command-center"):].lstrip("/") or "index.html"
            candidate = (WEB_DIR / rel).resolve()
            try:
                candidate.relative_to(WEB_DIR.resolve())
            except ValueError:
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_file(candidate)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/")
        if route == "/api/debug/simulate":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            try:
                raw = self.rfile.read(max(0, length))
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send_json({"error": "invalid json"}, status=400)
                return
            payload, status = _handle_debug_simulate(body)
            self._send_json(payload, status=status)
            return
        if route == "/api/config":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            try:
                raw = self.rfile.read(max(0, length))
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send_json({"error": "invalid json"}, status=400)
                return
            payload, status = _handle_config_patch(body)
            self._send_json(payload, status=status)
            return
        if route in ("/api/farm-targets", "/api/cropper-candidates"):
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            try:
                raw = self.rfile.read(max(0, length))
                body = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._send_json({"error": "invalid json"}, status=400)
                return
            if route == "/api/farm-targets":
                payload, status = _handle_farm_target_patch(body)
            else:
                payload, status = _handle_cropper_patch(body)
            self._send_json(payload, status=status)
            return
        if route != "/api/command":
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(max(0, length))
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid json"}, status=400)
            return
        action = str(body.get("action") or "").strip().upper()
        if action in WORKER_ACTIONS:
            self._send_json(_handle_worker_action(action, body))
            return
        if action not in SUPPORTED_COMMANDS:
            self._send_json(
                {
                    "status": "rejected",
                    "reason": f"azione '{action}' non supportata",
                    "supported": sorted(SUPPORTED_COMMANDS),
                },
                status=400,
            )
            return
        account_id = str(body.get("account_id") or "") or _default_account_id()
        if not account_id:
            self._send_json(
                {"status": "rejected", "reason": "account_id non risolvibile"},
                status=400,
            )
            return
        result = enqueue_command(
            account_id,
            action,
            village_name=str(body.get("village_name") or ""),
            params=body.get("params") if isinstance(body.get("params"), dict) else {},
        )
        status = 200 if result.get("status") == "queued" else 400
        self._send_json(result, status=status)


def start_command_server(port: int = COMMAND_PORT) -> bool:
    """Avvia il server in un thread daemon. True se il bind e' riuscito.

    Idempotente: una seconda chiamata nello stesso processo e' un no-op;
    su porta occupata (altro processo) ritorna False senza eccezioni.
    """
    global _server_started
    with _start_lock:
        if _server_started:
            return True
        try:
            server = ThreadingHTTPServer(
                ("127.0.0.1", port), _CommandCenterHandler
            )
        except OSError as exc:
            logger.warning(
                "Command Center: porta %d non disponibile (%s)", port, exc
            )
            return False
        thread = threading.Thread(
            target=server.serve_forever,
            name="command-center-http",
            daemon=True,
        )
        thread.start()
        _server_started = True
        logger.info(
            "Command Center live su http://127.0.0.1:%d/command-center/",
            port,
        )
        return True
