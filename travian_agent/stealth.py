"""Euristica anti-rilevamento comportamentale.

Tre meccanismi indipendenti:

1. ``ModulePauseRegistry``: quando un modulo va in SKIPPED o BLOCKED
   (es. deficit truppe con risorse insufficienti, slot occupato), viene
   messo in pausa per 15-30 minuti invece di riprovare al ciclo dopo.
   La pausa si sblocca in anticipo se lo stato delle risorse cambia.

2. ``human_cycle_interval``: intervallo tra cicli con distribuzione non
   uniforme (triangolare) e occasionali "distrazioni" piu' lunghe, per
   evitare la regolarita' metrica tipica dei bot.

3. ``nightly_window`` / ``night_sleep_remaining``: finestra di sonno
   quotidiana di 4-6 ore, con inizio e durata randomizzati per giorno
   (deterministici entro la stessa data, cosi' un riavvio non la cambia).
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Pausa del modulo dopo SKIPPED/BLOCKED (spec: 15-30 minuti).
MODULE_PAUSE_MIN_SECONDS = 15.0 * 60.0
MODULE_PAUSE_MAX_SECONDS = 30.0 * 60.0

# Sblocco anticipato della pausa se una risorsa varia oltre questa soglia
# (relativa) o il valore assoluto minimo: "finché non cambia lo stato
# delle risorse" (spec).
_RESOURCE_CHANGE_RATIO = 0.15
_RESOURCE_CHANGE_FLOOR = 200


def resource_snapshot(resources: Any) -> dict | None:
    """Estrae {wood, clay, iron, crop} da un oggetto Resources/dict."""
    if resources is None:
        return None
    if isinstance(resources, dict):
        keys = ("wood", "clay", "iron", "crop")
        if not any(k in resources for k in keys):
            return None
        return {k: int(resources.get(k) or 0) for k in keys}
    try:
        return {
            "wood": int(getattr(resources, "wood", 0) or 0),
            "clay": int(getattr(resources, "clay", 0) or 0),
            "iron": int(getattr(resources, "iron", 0) or 0),
            "crop": int(getattr(resources, "crop", 0) or 0),
        }
    except Exception:
        return None


def resources_changed(snapshot: dict | None, resources: Any) -> bool:
    """True se le risorse attuali differiscono dallo snapshot oltre soglia.

    Usato per lo sblocco anticipato delle pause da deficit risorse: se i
    magazzini si sono riempiti/svuotati sensibilmente, la condizione che
    aveva causato il blocco probabilmente non vale piu'.
    """
    cur = resource_snapshot(resources)
    if snapshot is None or cur is None:
        return False
    for key, prev in snapshot.items():
        delta = abs(cur.get(key, 0) - prev)
        if delta >= max(_RESOURCE_CHANGE_FLOOR, _RESOURCE_CHANGE_RATIO * max(prev, 1)):
            return True
    return False


class ModulePauseRegistry:
    """Pause per (account, villaggio, modulo, decisione).

    Chiave decisione=None nel check = "qualunque decisione del modulo".
    Le pause sono in memoria: un riavvio le azzera, ma al primo ciclo il
    modulo rivaluta e si ri-pausa se la condizione di blocco persiste.
    """

    def __init__(self) -> None:
        # (account_id, village_key, module, decision) -> (resume_ts, snapshot)
        self._pauses: dict[tuple[str, str, str, str], tuple[float, dict | None]] = {}

    def register(
        self,
        account_id: str,
        village_key: str,
        module: str,
        decision: str,
        *,
        resources: Any = None,
        min_seconds: float = MODULE_PAUSE_MIN_SECONDS,
        max_seconds: float = MODULE_PAUSE_MAX_SECONDS,
        now: float | None = None,
    ) -> float:
        """Mette in pausa il modulo; ritorna il timestamp di ripresa."""
        now = time.time() if now is None else now
        if max_seconds < min_seconds:
            min_seconds, max_seconds = max_seconds, min_seconds
        resume = now + random.uniform(min_seconds, max_seconds)
        key = (account_id, village_key, module, decision)
        self._pauses[key] = (resume, resource_snapshot(resources))
        if len(self._pauses) > 500:
            self._prune(now)
        logger.info(
            "[%s] Modulo '%s/%s' del villaggio %s in pausa fino a %s "
            "(SKIPPED/BLOCKED: non riprovo subito).",
            account_id, module, decision, village_key or "-",
            datetime.fromtimestamp(resume).strftime("%H:%M:%S"),
        )
        return resume

    def remaining(
        self,
        account_id: str,
        village_key: str,
        module: str,
        decision: str | None = None,
        *,
        resources: Any = None,
        now: float | None = None,
    ) -> float:
        """Secondi residui di pausa; 0 se il modulo puo' essere eseguito.

        Sblocco anticipato se ``resources`` differisce sensibilmente dallo
        snapshot preso al momento della pausa.
        """
        now = time.time() if now is None else now
        longest = 0.0
        for key, (resume, snapshot) in list(self._pauses.items()):
            acc, vkey, mod, dec = key
            if acc != account_id or vkey != village_key or mod != module:
                continue
            if decision is not None and dec != decision:
                continue
            if now >= resume or resources_changed(snapshot, resources):
                del self._pauses[key]
                continue
            longest = max(longest, resume - now)
        return longest

    def is_paused(self, *args, **kwargs) -> bool:
        return self.remaining(*args, **kwargs) > 0.0

    def clear(
        self,
        account_id: str,
        village_key: str,
        module: str,
        decision: str | None = None,
    ) -> None:
        """Sblocca subito la pausa (es. EXECUTED sulla stessa decisione)."""
        for key in list(self._pauses):
            acc, vkey, mod, dec = key
            if acc != account_id or vkey != village_key or mod != module:
                continue
            if decision is not None and dec != decision:
                continue
            del self._pauses[key]

    def _prune(self, now: float) -> None:
        for key, (resume, _) in list(self._pauses.items()):
            if now >= resume:
                del self._pauses[key]


# --- Jitter sulle soglie decisionali (anti firma comportamentale) ---------
#
# Le soglie matematiche fisse (storage 90%, cura eroe 50%) sono una firma
# rilevabile: ogni ciclo campiona valori leggermente diversi, conservati in
# ``settings.cycle_jitter`` e letti dalle regole del brain/eroe.
def sample_cycle_thresholds() -> dict:
    """Soglie campionate per il ciclo corrente (una tantum per ciclo)."""
    return {
        "storage": random.uniform(0.82, 0.93),
        "hero_heal": random.uniform(0.45, 0.62),
    }


# --- Resource-ETA Sleep ----------------------------------------------------
#
# Quando un upgrade fallisce per deficit di risorse, riprovare al ciclo dopo
# e' polling a vuoto: la pausa del modulo viene invece calcolata sul tempo
# reale necessario a produrre il deficit (con jitter e cap 45-60 min).
def resource_eta_seconds(
    deficit: dict, production_per_hour: dict
) -> float | None:
    """Secondi per colmare ``deficit`` data la produzione oraria per risorsa.

    ``deficit``/``production_per_hour`` usano chiavi wood/clay/iron/crop.
    Ritorna None se una risorsa in deficit ha produzione <= 0 (ETA non
    stimabile), 0 se non c'e' deficit.
    """
    eta = 0.0
    for res, need in (deficit or {}).items():
        need_f = max(0.0, float(need or 0.0))
        if need_f <= 0:
            continue
        prod = float((production_per_hour or {}).get(res) or 0.0)
        if prod <= 0:
            return None
        eta = max(eta, need_f / (prod / 3600.0))
    return eta


def eta_pause_bounds(eta_seconds: float) -> tuple[float, float]:
    """(min,max) della pausa modulo dal Resource-ETA.

    Pausa = eta * uniform(0.95, 1.12), cap randomizzato a 45-60 minuti,
    jitter asimmetrico fino a +120s / -120s. Pensata per
    ``ModulePauseRegistry.register(min_seconds=lo, max_seconds=hi)``.
    """
    eta = max(0.0, float(eta_seconds))
    if eta <= 0:
        return 0.0, 0.0
    cap = random.uniform(45.0 * 60.0, 60.0 * 60.0)
    base = min(eta * random.uniform(0.95, 1.12), cap)
    lo = max(30.0, base - random.uniform(0.0, 120.0))
    hi = min(cap, base + random.uniform(30.0, 120.0))
    return lo, max(lo + 30.0, hi)


def human_cycle_interval(min_seconds: float, max_seconds: float) -> float:
    """Intervallo tra due cicli con distribuzione NON uniforme.

    - base: triangolare (picco al centro del range) -> niente pattern
      a intervalli equidistanti ne' uniformi.
    - ~10%: "distrazione umana" prolungata (pausa lunga extra).
    """
    lo, hi = sorted((float(min_seconds), float(max_seconds)))
    base = random.triangular(lo, hi)
    if random.random() < 0.10:
        base += random.uniform(hi * 0.5, hi * 2.0)
    return base


def nightly_window(
    account_id: str,
    base_hour: float = 1.0,
    hours_min: float = 4.0,
    hours_max: float = 6.0,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Finestra di sonno di oggi: (inizio, fine) in ora locale.

    Inizio = base_hour +- 1.5h di jitter, durata = uniform(min, max).
    RNG seedato su (account, data): la finestra e' stabile entro il
    giorno (un riavvio non la sposta) ma varia di giorno in giorno.
    """
    now = now or datetime.now()
    rng = random.Random(f"{account_id}|{now.date().isoformat()}")
    start_hour = (base_hour + rng.uniform(-1.5, 1.5)) % 24.0
    duration_h = rng.uniform(min(hours_min, hours_max), max(hours_min, hours_max))
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start += timedelta(hours=start_hour)
    end = start + timedelta(hours=duration_h)
    return start, end


def night_sleep_remaining(
    account_id: str,
    base_hour: float = 1.0,
    hours_min: float = 4.0,
    hours_max: float = 6.0,
    now: datetime | None = None,
) -> float:
    """Secondi fino alla fine della finestra di sonno; 0 se fuori."""
    now = now or datetime.now()
    start, end = nightly_window(
        account_id, base_hour, hours_min, hours_max, now=now
    )
    if start <= now < end:
        return (end - now).total_seconds()
    return 0.0
