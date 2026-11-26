"""Mini server HTTP per il Command Center live.

Espone su una porta dedicata (default 8610):

- ``GET  /api/dashboard-state?account=<id>`` — ultimo snapshot di
  telemetria scritto dall'orchestratore (``state/dashboard_state_*.json``);
- ``POST /api/command`` — accoda un comando rapido per l'agente
  (``{"action","village_name","params","account_id"}``);
- ``POST /api/ask`` — diagnostica AI (Gemini): risponde a una domanda in
  linguaggio naturale usando come contesto snapshot telemetria, config
  e ultime righe del Decision Audit Log;
- ``POST /api/strategy/compile`` — Strategy Compiler: traduce una
  strategia in linguaggio naturale in un patch accounts.json validato
  (Gemini Structured Outputs + schema dataclass) con diff old->new e
  ``unsupported_actions``; ``POST /api/strategy/apply`` lo persiste;
- ``GET  /command-center[/...]`` — serve i file statici di
  ``web/command_center`` (index.html).

Viene avviato in un thread daemon da ``app.py`` (dashboard Streamlit)
e opzionalmente da ``main.py``: l'eventuale bind fallito su porta gia'
occupata viene ignorato — vale il primo server attivo.
"""

from __future__ import annotations

import copy
import ctypes
import json
import logging
import mimetypes
import os
import shutil
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
def _headed_available() -> bool:
    """True se il browser puo' aprire una finestra visibile (login assistito).

    Windows ha sempre un desktop; su Linux serve DISPLAY/WAYLAND_DISPLAY
    oppure xvfb-run come fallback.
    """
    if os.name == "nt":
        return True
    return bool(
        os.environ.get("DISPLAY")
        or os.environ.get("WAYLAND_DISPLAY")
        or shutil.which("xvfb-run")
    )


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
        if not _headed_available():
            return {
                "status": "rejected",
                "reason": (
                    "display non disponibile (container headless): il "
                    "login manuale richiede una finestra browser. Usa "
                    "username/password in accounts.json per il login "
                    "automatico."
                ),
            }
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
    """Config serializzabile di un Settings (nessun Path oggetto).

    La password non esce mai in chiaro: solo il flag "impostata si/no"
    (utile per la diagnostica login)."""
    return {
        "account_id": acc.account_id,
        "server_url": acc.server_url,
        "username": acc.username,
        "password_set": bool(getattr(acc, "password", "") or ""),
        "tribe": acc.tribe,
        "server_speed": float(getattr(acc, "server_speed", 1.0) or 1.0),
        "headless": acc.headless,
        "strategy_profile": acc.strategy_profile,
        "loop_interval_min": acc.loop_interval_min,
        "loop_interval_max": acc.loop_interval_max,
        "action_delay_min": getattr(acc, "action_delay_min", None),
        "action_delay_max": getattr(acc, "action_delay_max", None),
        "max_build_queue": getattr(acc, "max_build_queue", None),
        "max_resource_queue": getattr(acc, "max_resource_queue", None),
        "max_interior_queue": getattr(acc, "max_interior_queue", None),
        "allow_parallel_build": getattr(acc, "allow_parallel_build", None),
        "auto_video_bonus": getattr(acc, "auto_video_bonus", None),
        "stealth_pauses": getattr(acc, "stealth_pauses", None),
        "module_pause_minutes": getattr(acc, "module_pause_minutes", None),
        "module_pause_max_minutes": getattr(
            acc, "module_pause_max_minutes", None
        ),
        "night_sleep_enabled": getattr(acc, "night_sleep_enabled", None),
        "night_sleep_start_hour": getattr(
            acc, "night_sleep_start_hour", None
        ),
        "night_sleep_hours_min": getattr(acc, "night_sleep_hours_min", None),
        "night_sleep_hours_max": getattr(acc, "night_sleep_hours_max", None),
        "session_conflict_pause_minutes": getattr(
            acc, "session_conflict_pause_minutes", None
        ),
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
        _ftype = str(fields[k].type)
        if v is None and ("None" in _ftype or "Optional" in _ftype):
            # null esplicito su campo Optional (es. feed_requested_resources):
            # resetta a None — _coerce_checked lo rifiuterebbe quando il
            # valore corrente e' una lista/dict/int/float/bool.
            new, ok, note = None, True, ""
        else:
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
        # Salvataggio scheda: gli step senza slot_id nel payload
        # (dashboard manda null se il pin non e' selezionato nel form)
        # ereditano lo slot gia' configurato — match per key/gid.
        for _fname in ("building_queue_plan", "building_focus"):
            if _fname in patch:
                patch[_fname] = _preserve_step_slots(
                    patch.get(_fname), getattr(cfg, _fname, None) or []
                )
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


# ---------------------------------------------------------------------------
# Chiedi all'Agente / Diagnostica AI (Gemini)
# ---------------------------------------------------------------------------
# Stessa mappatura dei modelli EOL di ai_scout_brain: la API v1beta li ha
# deprecati, la richiesta verrebbe rifiutata.
_ASK_EOL_MODELS = {
    "gemini-1.5-flash", "gemini-1.5-flash-latest",
    "gemini-1.5-pro", "gemini-1.5-pro-latest", "gemini-pro",
}
_ASK_DEFAULT_MODEL = "gemini-3.5-flash-lite"
_AUDIT_PROMPT_ROWS = 80
# Eventi audit scansionati per ricavare l'ultimo evento di ogni modulo
# (copre anche i moduli silenziosi, non solo la coda del prompt).
_AUDIT_MODULE_SCAN_ROWS = 400
# Righe di errore lette dal log testuale dell'agente (state/agent_*.log).
_MODULE_ERROR_TAIL = 12


def _gemini_generate_sync(prompt: str, api_key: str, model_name: str) -> str:
    """Chiamata sincrona a Gemini: SDK google-genai, fallback legacy."""
    if model_name in _ASK_EOL_MODELS:
        model_name = _ASK_DEFAULT_MODEL
    try:
        from google import genai as _genai  # type: ignore

        client = _genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model_name, contents=prompt
        )
        return response.text or ""
    except ImportError:
        import google.generativeai as _legacy  # type: ignore

        _legacy.configure(api_key=api_key)
        model = _legacy.GenerativeModel(model_name)
        response = model.generate_content(prompt)
        return response.text or ""


def _fmt_ts(ts) -> str:
    """Epoch -> 'YYYY-MM-DD HH:MM:SS' locale; '' su valori invalidi/0."""
    try:
        val = float(ts or 0.0)
    except (TypeError, ValueError):
        return ""
    if val <= 0:
        return ""
    try:
        return datetime.fromtimestamp(val).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError):
        return ""


def _audit_events_for_prompt(
    account_id: str, snap: dict | None
) -> list[dict]:
    """Eventi del Decision Audit Log, dal piu' recente (DESC).

    Fonte primaria: tabella ``village_snapshots`` del DB (timestamp
    completi, storico fino a 7gg/5000 righe). Fallback: il tail
    ``audit_logs`` gia' serializzato nello snapshot dashboard.
    """
    try:
        from . import db

        events = db.decision_query(
            account_id, limit=_AUDIT_MODULE_SCAN_ROWS
        )
        if events:
            return events
    except Exception:  # noqa: BLE001
        pass
    out: list[dict] = []
    if isinstance(snap, dict):
        for r in (snap.get("audit_logs") or []):
            out.append(
                {
                    "ts": 0.0,
                    "time": str(r.get("time") or ""),
                    "village_name": str(r.get("village") or ""),
                    "module": str(r.get("module") or ""),
                    "decision": str(r.get("decision") or ""),
                    "outcome": str(r.get("outcome") or ""),
                    "reason": str(r.get("reason") or ""),
                }
            )
    return list(reversed(out))


def _ev_brief(ev: dict | None) -> dict | None:
    """Compatta un evento audit per il prompt (at/decision/outcome/...)."""
    if not ev:
        return None
    return {
        "at": ev.get("time") or _fmt_ts(ev.get("ts") or 0.0),
        "decision": str(ev.get("decision") or ""),
        "outcome": str(ev.get("outcome") or ""),
        "village": str(ev.get("village_name") or ev.get("village") or ""),
        "reason": str(ev.get("reason") or "")[:240],
    }


def _module_status(
    account_id: str, acc, events: list[dict], snap: dict | None
) -> dict:
    """Stato operativo dei subsystem per la diagnostica.

    Per ogni modulo: flag enabled da config, ``state_updated_at``
    (updated_ts del documento kv — proxy del last run), ultimo evento
    audit e ultimo BLOCKED. Per ``expansion`` anche il runtime persistito
    (cooldown scansione cropper, feed_target, CP, tile fondati) e per
    ``cropper_finder`` il conteggio dei candidati nel DB.
    """
    from . import db

    safe = "".join(
        c if c.isalnum() or c in "._-" else "_" for c in account_id
    )
    kv_keys = {
        "builder": f"build_plan_{account_id}.json",
        "scout": f"scout_{safe}.json",
        "farming": f"farming_{safe}.json",
        "hero": f"hero_{safe}.json",
        "expansion": f"expansion_{safe}.json",
        "defense": f"defense_{safe}.json",
        "replenishment": f"replenish_{safe}.json",
        "war": f"war_targets_{safe}.json",
    }
    kv_ts = db.kv_timestamps(list(kv_keys.values()))
    enabled = {
        "builder": bool(getattr(acc, "builder_enabled", True)),
        "scout": bool(getattr(acc.scout_brain, "enabled", False)),
        "farming": bool(getattr(acc.farming, "enabled", False)),
        "hero": bool(getattr(acc.hero, "enabled", False)),
        "expansion": bool(getattr(acc.expansion, "auto_expand", False)),
        "defense": bool(getattr(acc.defense, "enabled", False)),
        "replenishment": bool(
            getattr(acc.replenishment, "enabled", False)
        ),
    }

    # events e' DESC: la prima occorrenza per modulo e' la piu' recente.
    last_ev: dict[str, dict] = {}
    last_blocked: dict[str, dict] = {}
    for ev in events:
        mod = str(ev.get("module") or "")
        if not mod:
            continue
        last_ev.setdefault(mod, ev)
        if str(ev.get("outcome") or "") == "BLOCKED":
            last_blocked.setdefault(mod, ev)

    status: dict[str, dict] = {}
    for mod, key in kv_keys.items():
        entry: dict = {
            "enabled": enabled.get(mod),
            "state_updated_at": _fmt_ts(kv_ts.get(key, 0.0)),
            "last_audit_event": _ev_brief(last_ev.get(mod)),
        }
        blocked = last_blocked.get(mod)
        if blocked is not None:
            entry["last_blocked"] = _ev_brief(blocked)
        status[mod] = entry
    # Moduli audit senza documento kv (command_center, smithy, ...).
    for mod, ev in last_ev.items():
        if mod not in status:
            status[mod] = {
                "enabled": None,
                "state_updated_at": "",
                "last_audit_event": _ev_brief(ev),
            }

    # Doc kv dell'ExpansionManager: letto una volta per cropper_finder
    # (scan_stats) e per il blocco runtime (cooldown, feed_target, CP).
    try:
        raw = db.kv_read(kv_keys["expansion"])
        exp_doc = json.loads(raw) if raw else {}
        if not isinstance(exp_doc, dict):
            exp_doc = {}
    except Exception:  # noqa: BLE001
        exp_doc = {}

    # Cropper Finder: conteggi dei candidati nel DB + cooldown scansione
    # (gira dentro ExpansionManager -> doc expansion_<acc>.json).
    try:
        from .cropper import CropperStore

        cands = CropperStore().list_candidates(account_id)
        by_status: dict[str, int] = {}
        for c in cands:
            by_status[c.status] = by_status.get(c.status, 0) + 1
        status["cropper_finder"] = {
            "enabled": bool(
                getattr(acc.expansion, "cropper_scan", True)
            ),
            "scan_radius": int(
                getattr(acc.expansion, "scan_radius", 0) or 0
            ),
            "candidates_total": len(cands),
            "candidates_by_status": by_status,
            # Metriche ultima scansione persistite da ExpansionManager
            # (last_scan_at, tiles_read, map_views, details_checked,
            # cache_hits, candidates_found, free_deferred,
            # parser_suspect). Vuote se mai eseguita.
            "last_scan": exp_doc.get("scan_stats") or None,
            "candidates": [
                {
                    "id": c.id,
                    "x": c.x,
                    "y": c.y,
                    "type": c.type,
                    "status": c.status,
                    "bonus_crop_percent": c.bonus_crop_percent,
                    "distance_hours": c.distance_hours,
                }
                for c in cands[:25]
            ],
        }
    except Exception:  # noqa: BLE001
        logger.debug("Stato cropper_finder non disponibile", exc_info=True)

    # Runtime expansion: cooldown e obiettivi persistiti dal manager.
    rt: dict = {}
    for k in ("feed_target", "bootstrap_target", "founded_tiles"):
        if exp_doc.get(k) is not None:
            rt[k] = exp_doc.get(k)
    for k in (
        "next_map_scan",
        "next_found_attempt",
        "next_celebration_attempt",
        "next_cp_read",
    ):
        if exp_doc.get(k):
            rt[k] = _fmt_ts(exp_doc.get(k))
    for k in ("cp_current", "cp_required", "cp_daily"):
        if exp_doc.get(k):
            rt[k] = exp_doc.get(k)
    if rt:
        status.setdefault("expansion", {})["runtime"] = rt

    # Coloni addestrati: dallo snapshot (TRAIN_SETTLERS gira nel modulo
    # expansion — l'ultimo evento audit e' lo stesso canale).
    if isinstance(snap, dict):
        settlers = {
            str(v.get("name") or ""): int(
                (v.get("settlers") or {}).get("trained") or 0
            )
            for v in snap.get("villages") or []
        }
        settlers = {k: n for k, n in settlers.items() if n}
        status["settlers"] = {
            "trained_by_village": settlers,
            "trained_total": sum(settlers.values()),
            "last_audit_event": _ev_brief(last_ev.get("expansion")),
        }
    return status


def _focus_diagnostics(acc, snap: dict | None) -> dict:
    """skip_reasons + slot STUCK della Coda Prioritaria per villaggio.

    Ricalcola sul GameState ricostruito dallo snapshot con la stessa
    logica del builder runtime. Nota: ``focus_stuck_slots`` riflette il
    circuit-breaker in memoria del processo agente; se /api/ask gira in
    un processo diverso (dashboard) il set puo' essere vuoto — i motivi
    STUCK restano comunque visibili nell'audit log.
    """
    out: dict[str, dict] = {}
    if not isinstance(snap, dict):
        return out
    try:
        from .builder import focus_skip_reasons, focus_stuck_slots
    except Exception:  # noqa: BLE001
        return out
    for v in snap.get("villages") or []:
        name = str(v.get("name") or "")
        if not name:
            continue
        state = _state_from_snapshot(snap, name)
        if state is None:
            continue
        entry: dict = {}
        try:
            reasons = focus_skip_reasons(state, acc)
            if reasons:
                entry["skip_reasons"] = reasons
        except Exception:  # noqa: BLE001
            pass
        try:
            stuck = sorted(int(s) for s in focus_stuck_slots(state))
            if stuck:
                entry["stuck_slots"] = stuck
        except Exception:  # noqa: BLE001
            pass
        if entry:
            out[name] = entry
    return out


def _agent_log_errors(
    account_id: str, limit: int = _MODULE_ERROR_TAIL
) -> list[str]:
    """Ultime righe di errore dal log dell'agente (state/agent_<id>.log).

    Le eccezioni dei moduli ("Errore modulo ...") finiscono solo nel log
    testuale, non nel Decision Audit Log: questo tail copre il buco.
    """
    path = STATE_DIR / f"agent_{account_id}.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            fh.seek(max(0, size - 262144))
            tail = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    hits = [
        ln.strip()[:220]
        for ln in tail.splitlines()
        if any(
            k in ln.lower()
            for k in ("errore", "error", "traceback", "exception")
        )
    ]
    return hits[-limit:]


def _handle_ask_agent(body: dict) -> tuple[dict, int]:
    """POST /api/ask — chat diagnostica con contesto agente pre-assemblato.

    ``{"question", "account_id"?, "village"?, "history"? [{q,a}]}``: il
    prompt include stato worker, stato operativo dei moduli (ultimo
    evento audit, updated_ts del doc kv, ultimo BLOCKED, conteggio
    cropper_candidates), snapshot telemetria con eta' e diagnostica
    Coda Prioritaria (skip reasons + slot STUCK), config serializzata
    (segreti mascherati), tail errori del log agente e ultime
    ``_AUDIT_PROMPT_ROWS`` righe del Decision Audit Log dal DB.
    La chiave e' quella di scout_brain o GEMINI_API_KEY env.
    """
    from .config import load_accounts

    question = str(body.get("question") or "").strip()
    if not question:
        return {"status": "rejected", "reason": "domanda mancante"}, 400
    account_id = str(body.get("account_id") or "") or _default_account_id()
    if not account_id:
        return (
            {"status": "rejected", "reason": "account_id non risolvibile"},
            400,
        )
    village = str(body.get("village") or "").strip()

    acc = next(
        (a for a in load_accounts() if a.account_id == account_id), None
    )
    if acc is None:
        return (
            {"status": "rejected",
             "reason": f"account '{account_id}' non in accounts.json"},
            404,
        )

    api_key = (
        str(getattr(acc.scout_brain, "gemini_api_key", "") or "")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return (
            {
                "status": "rejected",
                "reason": "GEMINI_API_KEY non configurata: impostala in "
                "Farming & Eroe → AI Scout Brain o nel file .env",
            },
            400,
        )

    snap, _status = _read_snapshot(account_id)
    snap_ctx = (
        {k: v for k, v in snap.items() if k != "audit_logs"}
        if isinstance(snap, dict)
        else None
    )
    events = _audit_events_for_prompt(account_id, snap)
    # Le righe del prompt in ordine cronologico (events e' DESC).
    audit_rows = list(reversed(events[:_AUDIT_PROMPT_ROWS]))

    focus_diag = _focus_diagnostics(acc, snap)
    if isinstance(snap_ctx, dict):
        snap_ctx = dict(snap_ctx)
        # Eta' dello snapshot: chiave per distinguere "agente fermo"
        # da "modulo che non produce eventi".
        try:
            snap_ts = datetime.fromisoformat(str(snap.get("timestamp") or ""))
            snap_ctx["snapshot_age_seconds"] = max(
                0,
                int(
                    (
                        datetime.now(snap_ts.tzinfo) - snap_ts
                    ).total_seconds()
                ),
            )
        except (TypeError, ValueError):
            pass
        if focus_diag:
            snap_ctx["focus_queue_diagnostics"] = focus_diag

    module_status = _module_status(account_id, acc, events, snap)
    log_errors = _agent_log_errors(account_id)

    cfg = _account_config(acc)
    # La chiave non deve finire nel prompt: mascherata, ma la presenza
    # resta visibile (diagnostica "perche' lo scout va in fallback").
    sb_cfg = cfg.get("scout_brain")
    if isinstance(sb_cfg, dict):
        sb_cfg["gemini_api_key"] = "***" if sb_cfg.get("gemini_api_key") else ""

    history = [
        h for h in (body.get("history") or [])
        if isinstance(h, dict) and h.get("q")
    ][-6:]

    sections = [
        "Sei il modulo diagnostico AI di un agente Travian. Rispondi in "
        "italiano, in modo tecnico, diretto e azionabile. Nel contesto hai "
        "sezioni separate: STATO WORKER (processi/pause), STATO MODULI "
        "(enabled, state_updated_at = ultimo salvataggio del modulo, "
        "last_audit_event, last_blocked, conteggi cropper/coloni), "
        "SNAPSHOT (telemetria villaggi con snapshot_age_seconds e "
        "focus_queue_diagnostics = motivi di skip e slot STUCK della Coda "
        "Prioritaria), CONFIGURAZIONE, ERRORI LOG e DECISION AUDIT LOG. "
        "INCROCIA le fonti prima di rispondere: confronta i flag enabled/"
        "auto_expand/cropper_scan della config con i timestamp dei moduli "
        "e con gli eventi audit; se un modulo non ha eventi recenti "
        "verifica se e' disabilitato, se il worker e' fermo o in pausa, se "
        "lo snapshot e' vecchio o se l'ultimo BLOCKED/errore ne spiega il "
        "fermo. Cita sempre le evidenze usate (timestamp, decisioni, "
        "motivi). Rispondi 'non determinabile dai dati' solo dopo aver "
        "controllato TUTTE le sezioni, indicando quale dato manca o quale "
        "check manuale fare.",
        f"DATA/ORA SERVER: {datetime.now().isoformat(timespec='seconds')}",
        f"ACCOUNT: {account_id}"
        + (f" · VILLAGGIO FOCUS: {village}" if village else ""),
    ]
    if history:
        prev = "\n".join(
            f"Utente: {h['q']}\nAgente: {h.get('a', '')}" for h in history
        )
        sections.append(f"CONVERSAZIONE PRECEDENTE:\n{prev}")
    try:
        workers = _worker_rows()
    except Exception:  # noqa: BLE001
        workers = None
    if workers:
        sections.append(
            "STATO WORKER (processi agente, pause, sessioni):\n"
            + json.dumps(workers, ensure_ascii=False, indent=1)
        )
    sections.append(
        "STATO MODULI / BACKGROUND JOBS (enabled, ultimo salvataggio di "
        "stato, ultimo evento audit, ultimo BLOCKED, conteggi):\n"
        + json.dumps(module_status, ensure_ascii=False, indent=1)[:24000]
    )
    if snap_ctx is not None:
        sections.append(
            "INPUTS SNAPSHOT (ultima telemetria villaggi; "
            "snapshot_age_seconds = eta' del dato; "
            "focus_queue_diagnostics = skip reasons e slot STUCK):\n"
            + json.dumps(snap_ctx, ensure_ascii=False, indent=1)[:60000]
        )
    else:
        sections.append(
            "INPUTS SNAPSHOT: nessuna telemetria disponibile (l'agente "
            "non ha ancora completato un ciclo o non e' in esecuzione — "
            "incrocia con STATO WORKER)."
        )
    sections.append(
        "CONFIGURAZIONE AGENTE:\n"
        + json.dumps(cfg, ensure_ascii=False, indent=1)[:30000]
    )
    if log_errors:
        sections.append(
            "ULTIMI ERRORI DAL LOG AGENTE (state/agent_*.log):\n"
            + "\n".join(log_errors)
        )
    if audit_rows:
        audit_txt = "\n".join(
            f"[{r.get('time') or _fmt_ts(r.get('ts')) or '?'}] "
            f"{r.get('village_name') or r.get('village') or '?'} "
            f"{r.get('module', '?')} {r.get('decision', '?')} "
            f"({r.get('outcome') or '-'}): "
            f"{str(r.get('reason') or '')[:300]}"
            for r in audit_rows
        )
        sections.append(
            f"DECISION AUDIT LOG (ultime {len(audit_rows)} righe, "
            "cronologico):\n" + audit_txt
        )
    else:
        sections.append("DECISION AUDIT LOG: nessuna voce registrata.")
    sections.append(f"DOMANDA UTENTE: {question}")
    prompt = "\n\n".join(sections)

    model_name = str(
        getattr(acc.scout_brain, "gemini_model", "") or _ASK_DEFAULT_MODEL
    )
    used_model = (
        _ASK_DEFAULT_MODEL if model_name in _ASK_EOL_MODELS else model_name
    )
    try:
        answer = _gemini_generate_sync(prompt, api_key, model_name)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Diagnostica AI (ask) fallita", exc_info=True)
        return {"status": "error", "reason": f"Gemini: {exc}"}, 502
    if not answer.strip():
        return {"status": "error", "reason": "risposta vuota da Gemini"}, 502
    return (
        {
            "status": "ok",
            "answer": answer.strip(),
            "model": used_model,
            "context": {
                "account_id": account_id,
                "village": village,
                "snapshot": snap_ctx is not None,
                "audit_rows": len(audit_rows),
                "modules_tracked": len(module_status),
                "focus_diag_villages": sorted(focus_diag),
                "agent_log_errors": len(log_errors),
            },
        },
        200,
    )


# ---------------------------------------------------------------------------
# Strategy Compiler (Testo Strategico -> Config Account via Gemini)
# ---------------------------------------------------------------------------
# Sottoinsieme dei campi account delegabili alla strategia: credenziali
# e URL server non passano MAI per l'LLM.
_STRATEGY_ACCOUNT_FIELDS = _ACCOUNT_FIELDS - {
    "username", "password", "server_url",
}

# Schema di Structured Output (OpenAPI 3.0 subset, Gemini): il modello
# risponde con un oggetto {summary?, patch, unsupported_actions?} — la
# validazione vera avviene server-side sui dataclass di config.py.
_STRATEGY_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary": {"type": "STRING"},
        "patch": {
            "type": "OBJECT",
            "properties": {
                "account": {"type": "OBJECT"},
                "villages": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {
                            "village": {"type": "STRING"},
                            "patch": {"type": "OBJECT"},
                        },
                        "required": ["village", "patch"],
                    },
                },
                "farming": {"type": "OBJECT"},
                "scout_brain": {"type": "OBJECT"},
                "hero": {"type": "OBJECT"},
                "defense": {"type": "OBJECT"},
                "replenishment": {"type": "OBJECT"},
                "expansion": {"type": "OBJECT"},
            },
        },
        "unsupported_actions": {
            "type": "ARRAY", "items": {"type": "STRING"},
        },
    },
    "required": ["patch"],
}


def _gemini_generate_json_sync(
    prompt: str, api_key: str, model_name: str, schema: dict | None = None
) -> str:
    """Variante JSON di ``_gemini_generate_sync`` con Structured Outputs.

    Il nuovo SDK (google-genai) accetta ``response_mime_type`` +
    ``response_schema``; su SDK assente/vecchio o schema rifiutato si
    ripiega sulla chiamata testuale (il prompt chiede comunque JSON).
    """
    if model_name in _ASK_EOL_MODELS:
        model_name = _ASK_DEFAULT_MODEL
    if schema:
        try:
            from google import genai as _genai  # type: ignore

            client = _genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": schema,
                },
            )
            return response.text or ""
        except ImportError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug(
                "Structured output rifiutato: fallback su JSON testuale",
                exc_info=True,
            )
    return _gemini_generate_sync(prompt, api_key, model_name)


def _preserve_step_slots(new_steps, old_steps) -> list:
    """Ripesca lo ``slot_id`` configurato quando il patch non lo riporta.

    Il salvataggio dalla dashboard rimpiazza l'intera lista step e manda
    ``slot_id: null`` per gli step senza pin esplicito nel form: senza
    questo merge il valore gia' configurato (Focus Manuale su slot
    preciso) verrebbe azzerato a ogni salvataggio della scheda.
    Match per key o gid; ogni slot eredita una sola volta (niente
    doppioni su step duplicati).
    """
    if not isinstance(new_steps, list):
        return new_steps

    def _sid(step: dict) -> int:
        try:
            return int(step.get("slot_id") or step.get("slot") or 0)
        except (TypeError, ValueError):
            return 0

    key_map: dict[str, int] = {}
    gid_map: dict[int, int] = {}
    for old in old_steps or []:
        if not isinstance(old, dict):
            continue
        sid = _sid(old)
        if sid <= 0:
            continue
        okey = str(old.get("key") or "").strip().lower()
        if okey:
            key_map.setdefault(okey, sid)
        try:
            ogid = int(old.get("gid") or 0)
        except (TypeError, ValueError):
            ogid = 0
        if ogid > 0:
            gid_map.setdefault(ogid, sid)

    out: list = []
    for st in new_steps:
        if not isinstance(st, dict) or _sid(st) > 0:
            out.append(st)
            continue
        sid = 0
        nkey = str(st.get("key") or "").strip().lower()
        if nkey and nkey in key_map:
            sid = key_map.pop(nkey)
        else:
            try:
                ngid = int(st.get("gid") or 0)
            except (TypeError, ValueError):
                ngid = 0
            if ngid > 0 and ngid in gid_map:
                sid = gid_map.pop(ngid)
        if sid:
            st = dict(st)
            st["slot_id"] = sid
        out.append(st)
    return out


def _clean_queue_steps(
    steps, warnings: list[str], field_name: str
) -> list[dict]:
    """Normalizza gli step {key|gid, target_level, priority} della coda.

    Ogni step deve risolversi a un gid valido (FOCUS_KEY_GIDS o gid
    numerico): gli step non interpretabili vengono scartati con warning
    invece di corrompere la coda prioritaria del villaggio.
    """
    from .building_prereqs import gids_for_focus_key

    out: list[dict] = []
    for i, raw in enumerate(steps or []):
        s = {"key": raw} if isinstance(raw, str) else raw
        if not isinstance(s, dict):
            warnings.append(
                f"{field_name}[{i}]: step non oggetto — scartato"
            )
            continue
        key = str(s.get("key") or "").strip().lower()
        try:
            gid_i = int(s.get("gid") or 0)
        except (TypeError, ValueError):
            gid_i = 0
        if key and not gids_for_focus_key(key):
            warnings.append(
                f"{field_name}[{i}]: chiave '{key}' sconosciuta — scartato"
            )
            continue
        if not key and gid_i <= 0:
            warnings.append(
                f"{field_name}[{i}]: step senza key/gid — scartato"
            )
            continue
        step: dict = {}
        if key:
            step["key"] = key
        if gid_i > 0:
            step["gid"] = gid_i
        try:
            step["target_level"] = max(
                0, min(int(s.get("target_level") or s.get("target") or 0), 100)
            )
        except (TypeError, ValueError):
            step["target_level"] = 0
        try:
            step["priority"] = int(s.get("priority") or i + 1)
        except (TypeError, ValueError):
            step["priority"] = i + 1
        try:
            if int(s.get("slot_id") or 0) > 0:
                step["slot_id"] = int(s["slot_id"])
        except (TypeError, ValueError):
            pass
        if s.get("allow_duplicate_building"):
            step["allow_duplicate_building"] = True
        out.append(step)
    return out


def _sanitize_village_patch(
    patch: dict, warnings: list[str], vname: str, cfg=None
) -> dict:
    """Pulisce il patch villaggio: ruolo valido, step coda e mappe OK.

    Ritorna un patch ridotto ai soli campi VillageConfig ammissibili;
    le chiavi extra finiscono in ``warnings`` (elencate poi come
    unsupported_actions lato UI).
    """
    from .config import VillageConfig
    from .models import VillageRole
    from .presets import resolve_role

    out = dict(patch or {})
    valid_fields = set(VillageConfig.__dataclass_fields__)
    for k in list(out):
        if k not in valid_fields:
            warnings.append(
                f"villaggio {vname}: campo '{k}' non supportato — scartato"
            )
            out.pop(k)

    role = out.get("role")
    if role is not None:
        resolved = resolve_role(str(role))
        valid_roles = {r.value for r in VillageRole}
        if resolved not in valid_roles:
            warnings.append(
                f"villaggio {vname}: ruolo '{role}' non valido — scartato"
            )
            out.pop("role")
        else:
            out["role"] = resolved

    for fname in ("building_queue_plan", "building_focus"):
        if fname in out:
            # Patch senza slot_id: eredita il pin gia' configurato sullo
            # step corrispondente (key/gid) — vedi _handle_config_patch.
            out[fname] = _preserve_step_slots(
                out.get(fname), getattr(cfg, fname, None) or []
            )
            out[fname] = _clean_queue_steps(
                out.get(fname), warnings, f"{vname}.{fname}"
            )

    if "resource_weights" in out:
        weights = out.get("resource_weights")
        clean: dict[str, float] = {}
        if isinstance(weights, dict):
            for k, v in weights.items():
                key = str(k).strip().lower()
                if key not in ("wood", "clay", "iron", "crop"):
                    warnings.append(
                        f"villaggio {vname}: resource_weights '{k}' "
                        "non una risorsa — scartato"
                    )
                    continue
                try:
                    w = float(v)
                except (TypeError, ValueError):
                    continue
                if w > 0:
                    clean[key] = w
        elif weights:
            warnings.append(
                f"villaggio {vname}: resource_weights non oggetto — scartato"
            )
        out["resource_weights"] = clean

    if "troop_targets" in out:
        targets = out.get("troop_targets")
        clean_t: dict[str, int] = {}
        if isinstance(targets, dict):
            for k, v in targets.items():
                try:
                    qty = int(v or 0)
                except (TypeError, ValueError):
                    continue
                if qty > 0:
                    clean_t[str(k)] = qty
        elif targets:
            warnings.append(
                f"villaggio {vname}: troop_targets non oggetto — scartato"
            )
        out["troop_targets"] = clean_t
    return out


def _apply_strategy_patch(acc, patch: dict) -> tuple[dict, dict, list[str]]:
    """Applica un patch strategia a un Settings (muta ``acc``).

    Stesso percorso di ``_handle_config_patch`` (``_apply_fields_checked``
    sui dataclass) cosi' compile e apply condividono identiche regole di
    validazione. Ritorna ``(validated_patch, diff, warnings)`` dove
    ``validated_patch`` rispecchia l'input ma solo coi campi applicati —
    e' cio' che la UI rispedisce a ``/api/strategy/apply``.
    """
    from .config import ExpansionTarget, VillageConfig

    warnings: list[str] = []
    diff: dict = {}
    validated: dict = {}

    acc_patch = patch.get("account")
    if isinstance(acc_patch, dict) and acc_patch:
        applied, df, wn = _apply_fields_checked(
            acc, acc_patch, allowed=_STRATEGY_ACCOUNT_FIELDS
        )
        if applied:
            validated["account"] = applied
        if df:
            diff["account"] = df
        warnings.extend(f"account: {w}" for w in wn)

    for section, attr in _CONFIG_SECTIONS.items():
        sub = patch.get(section)
        if not isinstance(sub, dict) or not sub:
            continue
        cfg = getattr(acc, attr)
        sub = dict(sub)
        if section == "expansion" and "target_slots" in sub:
            slots = [
                ExpansionTarget(
                    x=int(t.get("x", 0)),
                    y=int(t.get("y", 0)),
                    note=str(t.get("note", "")),
                )
                for t in (sub.get("target_slots") or [])
                if isinstance(t, dict)
            ]
            old = [asdict(t) for t in cfg.target_slots]
            cfg.target_slots = slots
            new = [asdict(t) for t in slots]
            if new != old:
                diff.setdefault("expansion", {})["target_slots"] = {
                    "old": old, "new": new,
                }
            validated.setdefault("expansion", {})["target_slots"] = new
            sub.pop("target_slots")
        applied, df, wn = _apply_fields_checked(cfg, sub)
        if applied:
            validated.setdefault(section, {}).update(applied)
        if df:
            diff.setdefault(section, {}).update(df)
        warnings.extend(f"{section}: {w}" for w in wn)

    for item in patch.get("villages") or []:
        if not isinstance(item, dict):
            continue
        vname = str(item.get("village") or "").strip()
        vp = item.get("patch")
        if not vname or not isinstance(vp, dict):
            continue
        cfg = acc.villages.get(vname)
        if cfg is None:
            cfg = next(
                (c for c in acc.villages.values() if c.name == vname), None
            )
        if cfg is None:
            cfg = VillageConfig(name=vname)
            acc.villages[vname] = cfg
            warnings.append(
                f"villaggio '{vname}' non in config: creato ex-novo"
            )
        vp_clean = _sanitize_village_patch(vp, warnings, vname, cfg)
        applied, df, wn = _apply_fields_checked(cfg, vp_clean)
        if applied:
            # Le personalizzazioni esplicite (strategia compresa) non
            # vengono sovrascritte dai preset di ruolo automatici.
            cfg.custom_modified = True
            validated.setdefault("villages", []).append(
                {"village": vname, "patch": applied}
            )
        if df:
            diff[f"villaggio:{vname}"] = df
        warnings.extend(f"villaggio {vname}: {w}" for w in wn)
    return validated, diff, warnings


def _strategy_prompt(acc, text: str, snap: dict | None) -> str:
    """Prompt del compilatore: schema config + catalogo chiavi valide."""
    from .building_prereqs import FOCUS_KEY_GIDS, GID_LABELS
    from .models import STATIC_TROOP_LABELS, VillageRole
    from .presets import ROLE_PRESETS, TRIBE_UNIT_SLOTS

    cfg = _account_config(acc)
    sb_cfg = cfg.get("scout_brain")
    if isinstance(sb_cfg, dict):
        sb_cfg["gemini_api_key"] = "***" if sb_cfg.get("gemini_api_key") else ""

    villages_ser = cfg.get("villages") or {}
    village_names = sorted(
        {str(v.get("name") or k) for k, v in villages_ser.items()
         if isinstance(v, dict)}
        | {str(k) for k in villages_ser}
    )
    if isinstance(snap, dict):
        for v in snap.get("villages") or []:
            if isinstance(v, dict) and v.get("name"):
                if str(v["name"]) not in village_names:
                    village_names.append(str(v["name"]))

    field_catalog = {
        "account_fields": sorted(_STRATEGY_ACCOUNT_FIELDS),
        "village_fields": [
            "role", "building_queue_plan", "building_focus",
            "building_focus_enabled", "resource_weights",
            "troop_targets", "allowed_units", "priority_units",
            "farm_enabled", "farm_unit", "farm_unit_qty",
            "target_village", "min_net_crop_threshold", "max_crannies",
            "is_bootstrap", "is_reclaim_source", "defense_enabled",
            "market_slot", "smithy_auto_upgrade",
            "troop_replenishment_preset",
            "reclaim_village_x", "reclaim_village_y", "resource_keep",
        ],
        "farming": sorted(cfg["farming"]),
        "scout_brain": sorted(cfg["scout_brain"]),
        "hero": sorted(cfg["hero"]),
        "defense": sorted(cfg["defense"]),
        "replenishment": sorted(cfg["replenishment"]),
        "expansion": sorted(cfg["expansion"]),
        "roles": sorted({r.value for r in VillageRole} | set(ROLE_PRESETS)),
        "building_focus_keys": sorted(FOCUS_KEY_GIDS),
        "gid_labels": GID_LABELS,
        "resource_weight_keys": ["wood", "clay", "iron", "crop"],
        "troop_abstract_slots": sorted(TRIBE_UNIT_SLOTS),
        "troop_t_keys": sorted(STATIC_TROOP_LABELS),
    }

    return "\n\n".join([
        "Sei il Strategy Compiler di un agente Travian. Traduci la "
        "strategia in linguaggio naturale in un patch di configurazione "
        "accounts.json. Rispondi SOLO con l'oggetto JSON richiesto: "
        '{"summary": str, "patch": {...}, "unsupported_actions": [str]}',
        "REGOLE:\n"
        "- patch.account: solo i campi di 'account_fields'.\n"
        "- patch.villages: array {village, patch} — 'village' deve essere "
        "un nome/newdid tra i VILLAGGI NOTI; patch usa solo 'village_fields'.\n"
        "- building_queue_plan/building_focus: lista ordinata di step "
        '{"key"|"gid", "target_level", "priority"} — key da '
        "'building_focus_keys' (gid da 'gid_labels').\n"
        "- resource_weights: {wood,clay,iron,crop} float>0 (peso alto = "
        "priorita' nel selettore campi dorf1).\n"
        "- AUTO_CELEBRATION / feste automatiche al Municipio -> "
        "expansion.auto_celebrate=true.\n"
        "- HERO_AUTO_STATS / gestione punti abilita' -> hero.enabled=true "
        "+ hero.attribute_focus; cura con unguenti (HERO_OINTMENT) -> "
        "hero.heal_threshold.\n"
        "- Anti-raid/crannies -> village.max_crannies o ruolo 'defensive'.\n"
        "- Corsa al 2o villaggio / coloni -> expansion.auto_expand, "
        "expansion.auto_celebrate, ruolo 'early_cropper_founder' o "
        "'chiefing', troop_targets con 'settler'.\n"
        "- Ogni richiesta NON mappabile sui campi elencati va in "
        "'unsupported_actions' (una riga per azione).\n"
        "- NON inventare nomi di campi: solo quelli del CATALOGO.",
        f"CATALOGO CAMPI/CHIAVI VALIDE:\n{json.dumps(field_catalog, ensure_ascii=False)}",
        f"VILLAGGI NOTI: {json.dumps(village_names, ensure_ascii=False)}",
        "CONFIG ATTUALE (per contesto/diff):\n"
        + json.dumps(cfg, ensure_ascii=False, indent=1)[:25000],
        f"STRATEGIA UTENTE:\n{text}",
    ])


def _handle_strategy_compile(body: dict) -> tuple[dict, int]:
    """POST /api/strategy/compile — testo -> patch validato + diff.

    ``{"account_id", "text"}``: Gemini produce il patch via Structured
    Outputs (JSON Schema), poi ``_apply_strategy_patch`` lo valida su
    una copia dell'account — la risposta mostra solo cio' che
    sopravviverebbe alla scrittura reale, con diff old->new.
    """
    from .ai_scout_brain import _extract_json_from_text
    from .config import load_accounts

    text = str(body.get("text") or body.get("strategy") or "").strip()
    if not text:
        return {"status": "rejected", "reason": "testo strategia mancante"}, 400
    account_id = str(body.get("account_id") or "") or _default_account_id()
    if not account_id:
        return (
            {"status": "rejected", "reason": "account_id non risolvibile"},
            400,
        )
    acc = next(
        (a for a in load_accounts() if a.account_id == account_id), None
    )
    if acc is None:
        return (
            {"status": "rejected",
             "reason": f"account '{account_id}' non in accounts.json"},
            404,
        )
    api_key = (
        str(getattr(acc.scout_brain, "gemini_api_key", "") or "")
        or os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or ""
    ).strip()
    if not api_key:
        return (
            {
                "status": "rejected",
                "reason": "GEMINI_API_KEY non configurata: impostala in "
                "Farming & Eroe → AI Scout Brain o nel file .env",
            },
            400,
        )

    snap, _status = _read_snapshot(account_id)
    prompt = _strategy_prompt(acc, text, snap if isinstance(snap, dict) else None)
    model_name = str(
        getattr(acc.scout_brain, "gemini_model", "") or _ASK_DEFAULT_MODEL
    )
    used_model = (
        _ASK_DEFAULT_MODEL if model_name in _ASK_EOL_MODELS else model_name
    )
    try:
        raw = _gemini_generate_json_sync(
            prompt, api_key, model_name, _STRATEGY_RESPONSE_SCHEMA
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Strategy Compiler (compile) fallito", exc_info=True)
        return {"status": "error", "reason": f"Gemini: {exc}"}, 502
    if not raw.strip():
        return {"status": "error", "reason": "risposta vuota da Gemini"}, 502
    try:
        data = _extract_json_from_text(raw)
    except (ValueError, TypeError):
        return (
            {
                "status": "error",
                "reason": "output Gemini non JSON valido",
                "raw_excerpt": raw[:500],
            },
            502,
        )
    if not isinstance(data, dict) or not isinstance(data.get("patch"), dict):
        return (
            {"status": "error", "reason": "patch mancante nella risposta AI"},
            502,
        )

    sandbox = copy.deepcopy(acc)
    validated, diff, warnings = _apply_strategy_patch(
        sandbox, data["patch"]
    )
    unsupported = [
        str(u) for u in (data.get("unsupported_actions") or [])
    ][:50]
    # Warning di validazione = richieste parzialmente non supportate:
    # riportate a fianco di unsupported_actions per la UI.
    try:
        debug_trace.append_trace(
            account_id,
            "strategy_compile",
            {
                "summary": str(data.get("summary") or ""),
                "diff": diff,
                "warnings": warnings,
                "unsupported_actions": unsupported,
            },
        )
    except Exception:  # noqa: BLE001
        pass
    return (
        {
            "status": "ok",
            "summary": str(data.get("summary") or ""),
            "model": used_model,
            "patch": validated,
            "diff": diff,
            "warnings": warnings,
            "unsupported_actions": unsupported,
        },
        200,
    )


def _handle_strategy_apply(body: dict) -> tuple[dict, int]:
    """POST /api/strategy/apply — salva il patch validato dalla compile.

    ``{"account_id", "patch"}``: il patch (gia' mostrato come diff al
    passo compile) ripassa per ``_apply_strategy_patch`` sul Settings
    reale e viene persistito con save_accounts (tmp+rename+.bak);
    l'agente hot-reloada accounts.json al ciclo successivo.
    """
    from .config import load_accounts, save_accounts

    account_id = str(body.get("account_id") or "") or _default_account_id()
    patch = body.get("patch")
    if not isinstance(patch, dict) or not patch:
        return {"status": "rejected", "reason": "patch mancante"}, 400
    accounts = load_accounts()
    acc = next((a for a in accounts if a.account_id == account_id), None)
    if acc is None:
        return (
            {"status": "rejected",
             "reason": f"account '{account_id}' non trovato"},
            404,
        )
    _validated, diff, warnings = _apply_strategy_patch(acc, patch)
    if not diff:
        return (
            {
                "status": "rejected",
                "reason": "nessun campo valido nel patch",
                "warnings": warnings,
            },
            400,
        )
    for w in warnings:
        logger.warning("Strategy apply %s: %s", account_id, w)
    save_accounts(accounts)
    try:
        debug_trace.append_trace(
            account_id,
            "strategy_apply",
            {"diff": diff, "warnings": warnings},
        )
    except Exception:  # noqa: BLE001
        pass
    return (
        {
            "status": "saved",
            "account_id": account_id,
            "diff": diff,
            "warnings": warnings,
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
                "headed_available": _headed_available(),
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
        if route in ("/api/strategy/compile", "/api/strategy/apply"):
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
            if route == "/api/strategy/compile":
                payload, status = _handle_strategy_compile(body)
            else:
                payload, status = _handle_strategy_apply(body)
            self._send_json(payload, status=status)
            return
        if route == "/api/ask":
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
            payload, status = _handle_ask_agent(body)
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
        # Su deploy remoti (Coolify) il reverse proxy non raggiunge il
        # loopback del container: TRAVIAN_API_BIND=0.0.0.0 espone la
        # porta 8610 all'esterno. Default 127.0.0.1 per uso locale.
        host = os.getenv("TRAVIAN_API_BIND", "127.0.0.1").strip() or "127.0.0.1"
        try:
            server = ThreadingHTTPServer(
                (host, port), _CommandCenterHandler
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
            "Command Center live su http://%s:%d/command-center/",
            host,
            port,
        )
        return True
