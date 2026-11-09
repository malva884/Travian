"""Agente Python per giochi browser di strategia (Travian Legends e simili).

Architettura:
    - browser.py   -> il "driver": Playwright, login, delay umani
    - sensors.py   -> i "sensori": parsing JS Injection + DOM -> GameState / TravianParser
    - brain.py     -> il "cervello": regole deterministiche / stub LLM
    - actions.py   -> l'esecutore delle azioni decise
    - orchestrator.py -> gestione multi-account e resilienza 24/7
    - main.py      -> il loop temporale Sense -> Decide -> Act
"""

__version__ = "0.1.0"
__all__ = ["GameObserver", "TravianParser"]

try:
    from .sensors import GameObserver, TravianParser
except ImportError:
    GameObserver = None  # type: ignore
    TravianParser = None  # type: ignore
