"""Agente Python per giochi browser di strategia (Travian Legends e simili).

Architettura:
    - browser.py   -> il "driver": Playwright, login, delay umani
    - sensors.py   -> i "sensori": parsing JS Injection + DOM -> GameState / TravianParser
    - brain.py     -> il "cervello": regole deterministiche / stub LLM
    - actions.py   -> l'esecutore delle azioni decise
    - orchestrator.py -> gestione multi-account e resilienza 24/7
    - main.py      -> il loop temporale Sense -> Decide -> Act
"""

import sys

# Workaround Windows (WinError 10054): quando il processo Chromium chiude
# bruscamente la pipe, ProactorEventLoop esegue socket.shutdown() su una
# connessione gia' morta e asyncio logga "Exception in callback
# _call_connection_lost" con ConnectionResetError. E' rumore non bloccante
# (la connessione e' gia' chiusa): lo sopprimiamo qui, a livello di package,
# cosi' la patch e' attiva in QUALUNQUE processo che importi travian_agent.*
# (dashboard Streamlit inclusa), non solo in chi importa browser.py.
if sys.platform.startswith("win"):
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport

        _orig_call_connection_lost = (
            _ProactorBasePipeTransport._call_connection_lost
        )

        def _safe_call_connection_lost(self, exc):  # type: ignore[no-untyped-def]
            try:
                _orig_call_connection_lost(self, exc)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass

        _ProactorBasePipeTransport._call_connection_lost = (
            _safe_call_connection_lost
        )
    except Exception:  # noqa: BLE001 - patch opzionale, best effort
        pass

__version__ = "0.1.0"
__all__ = ["GameObserver", "TravianParser"]

try:
    from .sensors import GameObserver, TravianParser
except ImportError:
    GameObserver = None  # type: ignore
    TravianParser = None  # type: ignore
