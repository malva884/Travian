"""Identificatore di build dell'agente per la telemetria.

``get_agent_version()`` restituisce una stringa compatta che cambia a
ogni modifica del codice (git short hash, con ``*`` se il worktree e'
sporco; fallback sha1 dei file core quando git non e' disponibile) piu'
l'orario di avvio del processo.  Il badge nella dashboard confronta
``agent.build`` con ``server.build``: se divergono l'agente sta
girando codice diverso dal command server (restart dimenticato).
"""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path

_BOOT_TS = datetime.now().strftime("%H:%M:%S")
_PKG_DIR = Path(__file__).resolve().parent
# File core la cui modifica deve cambiare il fingerprint senza git.
_CORE_FILES = (
    "orchestrator.py",
    "brain.py",
    "builder.py",
    "sensors.py",
    "telemetry.py",
)


def _git_build() -> str | None:
    """git short hash + '*' se il worktree ha modifiche non committate."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PKG_DIR.parent,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        ).strip()
        if not out:
            return None
        dirty = (
            subprocess.call(
                ["git", "diff", "--quiet", "HEAD"],
                cwd=_PKG_DIR.parent,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
            != 0
        )
        return f"{out}{'*' if dirty else ''}"
    except Exception:  # noqa: BLE001
        return None


def _files_build() -> str:
    """sha1 dei file core (fallback quando git non e' disponibile)."""
    h = hashlib.sha1()
    for name in _CORE_FILES:
        try:
            h.update((_PKG_DIR / name).read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:7]


def get_agent_version() -> dict:
    """``{"build": <hash>, "started_at": <HH:MM:SS>}``."""
    return {
        "build": _git_build() or _files_build(),
        "started_at": _BOOT_TS,
    }
