"""Modulo Farming: invio periodico di raid dalle farm list del Punto di Raduno.

Design:
- uno scheduler per account mantiene il timestamp del prossimo raid per
  ogni villaggio in state/farming_<account>.json (persiste tra i riavvii);
- l'intervallo tra un ciclo e l'altro e' SEMPRE randomizzato con jitter
  uniforme tra interval_min_minutes e interval_max_minutes (requisito
  anti-ban: mai due cicli a distanza identica);
- prima dell'invio si verifica che il villaggio abbia truppe a casa
  (sensore read_troops_home); se il sensore non espone dati si tenta
  comunque l'invio (un raid vuoto fallisce lato gioco senza danni);
- il ritorno delle truppe cariche viene gestito dal normale ciclo di
  smaltimento surplus del brain (storage overflow -> mercato/truppe).
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BASE_DIR, Settings
from .models import Action, ActionType, GID_RALLY_POINT

if TYPE_CHECKING:
    from .actions import ActionExecutor
    from .browser import TravianBrowser
    from .sensors import GameObserver
    from .models import Field, Village

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Fallback: se il sensore truppe non legge dati, riprova comunque dopo
# un breve intervallo (l'invio fallito non ha effetti collaterali).
RETRY_MIN_SECONDS = 240.0
RETRY_MAX_SECONDS = 420.0

# Backoff lungo quando non esiste alcuna farm list (Gold Club attivo ma
# 0/100 liste): riprovare ogni pochi minuti non serve, le liste vanno
# create in-game. Controllo orario con jitter per limitare page load e
# warning ripetuti.
NO_LIST_RETRY_MIN_SECONDS = 3600.0
NO_LIST_RETRY_MAX_SECONDS = 5400.0

# Alias delle truppe "leggere da saccheggio" per verificare la presenza
# quando non e' configurata un'unita' specifica: le chiavi del sensore
# sono 'uN' (icona unit u1..u30 del CSS Travian).
_LIGHT_RAIDERS = {
    "u2",   # pretoriano? no -> legionario u1... dipende dalla tribu';
    "u1",   # legionario (romani)
    "u4",   # emissario? no. Usiamo un set ampio:
    "u5", "u6",
    "u11",  # clubswinger (teutoni)
    "u12", "u13", "u15", "u16",
    "u22",  # spadaccino (galli)
    "u24", "u25", "u26",
}

# Nome -> set di alias accettati per il check 'unità designata presente'
_UNIT_KEY_ALIASES: dict[str, set[str]] = {
    # --- ROMANI ---
    "legione": {"u1"}, "legionario": {"u1"}, "legionari": {"u1"},
    "pretoriano": {"u2"}, "pretoriani": {"u2"},
    "imperiano": {"u3"}, "imperiani": {"u3"},
    "equites legatus": {"u4"}, "esploratore romano": {"u4"}, "emissario": {"u4"},
    "equites imperatoris": {"u5"}, "cavaliere imperatoris": {"u5"},
    "equites cesaris": {"u6"}, "cavaliere cesaris": {"u6"},
    "ariete": {"u7"}, "ariete romano": {"u7"},
    "catapulta da fuoco": {"u8"}, "catapulta": {"u8"},
    "senatore": {"u9"}, "senatori": {"u9"},
    "colono": {"u10"}, "coloni": {"u10"},

    # --- TEUTONI ---
    "mazziere": {"u11"}, "mazzieri": {"u11"}, "combattente": {"u11"},
    "lanciere": {"u12"}, "lancieri": {"u12"},
    "ascia": {"u13"}, "guerriero con ascia": {"u13"},
    "spia": {"u14"}, "esploratore teutonico": {"u14"},
    "paladino": {"u15"}, "paladini": {"u15"},
    "cavaliere teutonico": {"u16"}, "teutonico": {"u16"},
    "ariete teutonico": {"u17"},
    "catapulta teutonica": {"u18"},
    "capotribù": {"u19"}, "comandante teutonico": {"u19"},
    "colono teutonico": {"u20"},

    # --- GALLI ---
    "falange": {"u21"}, "falangi": {"u21"},
    "spadaccino": {"u22"}, "spadaccini": {"u22"},
    "tracciatore": {"u23"}, "esploratore gallo": {"u23"},
    "fulmine": {"u24"}, "fulmine di teutates": {"u24"}, "teutates": {"u24"},
    "druido": {"u25"}, "cavaliere druido": {"u25"},
    "eduano": {"u26"}, "haeduan": {"u26"},
    "ariete gallo": {"u27"}, "trabucco": {"u27"},
    "catapulta gallo": {"u28"},
    "capotribù gallo": {"u29"}, "comandante": {"u29"},
    "colono gallo": {"u30"},

    # --- MACRO-GRUPPI GENERALI ---
    "cavaliere": {"u4", "u5", "u6", "u15", "u16", "u24", "u25", "u26"},
    "esploratore": {"u4", "u14", "u23"},
    "truppe leggere": set(_LIGHT_RAIDERS),
    "legger": set(_LIGHT_RAIDERS),
}


def find_rally_point(interior: list["Field"]) -> "Field | None":
    """Restituisce il campo del Punto di Raduno (gid 16) negli interni."""
    for f in interior or []:
        if f.gid == GID_RALLY_POINT:
            return f
    return None


@dataclass
class FarmingManager:
    """Scheduler dei raid per un account: timing jittered + stato su disco."""

    account_id: str
    state_path: Path | None = None
    next_due: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self.state_path = STATE_DIR / f"farming_{safe}.json"
        self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.state_path and self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.next_due = {
                    str(k): float(v) for k, v in (data.get("next_due") or {}).items()
                }
        except Exception:
            self.next_due = {}

    def _save(self) -> None:
        try:
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"next_due": self.next_due}, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Salvataggio stato farming fallito", exc_info=True)

    # ------------------------------------------------------------------
    # Scheduling con jitter (anti-ban)
    # ------------------------------------------------------------------
    def next_interval_seconds(self, settings: Settings) -> float:
        """Intervallo casuale uniforme tra min e max configurati, in secondi."""
        lo = max(60.0, float(settings.farming.interval_min_minutes) * 60.0)
        hi = max(lo, float(settings.farming.interval_max_minutes) * 60.0)
        return random.uniform(lo, hi)

    def is_due(self, village_key: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now >= self.next_due.get(village_key, 0.0)

    def schedule_next(self, village_key: str, settings: Settings, now: float | None = None) -> float:
        """Pianifica il prossimo raid e ritorna il timestamp pianificato."""
        now = now if now is not None else time.time()
        due = now + self.next_interval_seconds(settings)
        self.next_due[village_key] = due
        self._save()
        return due

    def _schedule_retry(self, village_key: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.next_due[village_key] = now + random.uniform(
            RETRY_MIN_SECONDS, RETRY_MAX_SECONDS
        )
        self._save()

    # ------------------------------------------------------------------
    # Verifica truppe
    # ------------------------------------------------------------------
    def _has_raiders(self, troops: dict[str, int], unit_pref: str) -> bool:
        """True se ci sono truppe da saccheggio a casa.

        troops: {u1: n, u5: n, ...} dal sensore. unit_pref: nome config
        ('' = qualsiasi truppa leggera). Se il sensore non ha letto nulla
        (dict vuoto) restituisce True: si tenta comunque l'invio.
        """
        if not troops:
            return True
        keys: set[str]
        if unit_pref:
            norm = unit_pref.strip().lower()
            keys = set()
            for alias, ukeys in _UNIT_KEY_ALIASES.items():
                if alias in norm or norm in alias:
                    keys.update(ukeys)
            if not keys:
                keys = set(_LIGHT_RAIDERS)
        else:
            keys = set(_LIGHT_RAIDERS)
        return any(troops.get(k, 0) > 0 for k in keys)

    # ------------------------------------------------------------------
    # Ciclo principale per villaggio
    # ------------------------------------------------------------------
    async def run_for_village(
        self,
        *,
        browser: "TravianBrowser",
        observer: "GameObserver",
        executor: "ActionExecutor",
        settings: Settings,
        village: "Village",
        rally_slot: int,
        prefix: str,
        now: float | None = None,
    ) -> bool:
        """Se scaduto e truppe disponibili, lancia un raid dalla farm list.

        Ritorna True se un raid e' stato avviato con successo.
        """
        cfg = settings.farming
        if not cfg.enabled:
            return False
        vcfg = settings.village_config_for(village.name, village.village_id)
        if vcfg is not None and not vcfg.farm_enabled:
            return False
        if not rally_slot:
            return False

        vkey = f"{village.name or village.village_id}"
        now = now if now is not None else time.time()
        if not self.is_due(vkey, now):
            return False

        # Verifica truppe a casa prima di lanciare il comando
        unit_pref = (vcfg.farm_unit if vcfg else "") or cfg.unit
        troops = await observer.read_troops_home(rally_slot, village.village_id)
        if troops and not self._has_raiders(troops, unit_pref):
            logger.info(
                "%s Farming: nessuna truppa da saccheggio a casa in '%s' "
                "(presenti: %s). Riprovo piu' tardi.",
                prefix, village.name, troops,
            )
            self._schedule_retry(vkey, now)
            return False

        action = Action(
            type=ActionType.SEND_RAID,
            params={
                "rally_slot": rally_slot,
                "farm_list": cfg.farm_list,
                "unit": unit_pref,
            },
            reason=(
                f"Farming automatico: raid dalla farm list "
                f"'{cfg.farm_list or '(prima disponibile)'}' "
                f"(villaggio {village.name})"
            ),
        )
        result = await executor.execute(action, village_id=village.village_id)
        if result.success:
            due = self.schedule_next(vkey, settings, now)
            wait_min = (due - now) / 60.0
            logger.info(
                "%s Farming: raid avviato da '%s'. Prossimo ciclo tra ~%.0f min.",
                prefix, village.name, wait_min,
            )
            return True

        logger.warning(
            "%s Farming: raid non avviato su '%s': %s. Retry programmato.",
            prefix, village.name, result.message,
        )
        if "nessuna farm list" in (result.message or "").lower():
            # Le liste non esistono: creazione manuale in-game richiesta.
            self.next_due[vkey] = now + random.uniform(
                NO_LIST_RETRY_MIN_SECONDS, NO_LIST_RETRY_MAX_SECONDS
            )
            self._save()
        else:
            self._schedule_retry(vkey, now)
        return False
