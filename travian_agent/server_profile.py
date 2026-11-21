"""Adaptive Server Profile: auto-calibrazione per-server persistita su disco.

Ogni server Travian (legacy vs moderni, skin diverse) ha markup e
routing differenti. Invece di patchare il codice a ogni drift, l'agente
mantiene un profilo JSON per host in ``state/server_profile_<host>.json``
con le calibrazioni apprese a runtime:

- ``routing_mode``: 'legacy' (build.php?id=N) / 'modern' (/slot?id=N,
  /building?id=N, /build/N) rilevato dagli href di dorf1/dorf2;
- ``empty_slot_gids``: gid che indicano uno slot libero. Server
  differenti marcano l'area vuota con -1, null o gid=0: gid=0 viene
  promosso qui dal self-heal di ``actions._upgrade_field`` quando il
  DOM mostra l'UI di 'nuova costruzione';
- ``preferred_selectors``: selettore vincente (delle liste fallback di
  ``selectors.py``) promosso a primo tentativo per questa host.

Il profilo sopravvive ai riavvii del container (state/ e' il volume
persistente) ed e' auto-consistente: file corrotto -> profilo pulito.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import BASE_DIR

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

PROFILE_VERSION = 1

# Token path che identificano il routing 'modern' dei server recenti
# (Travian Legends nuova UI): usati dal probe JS di calibrazione.
_MODERN_URL_TOKENS = ("/building", "/slot", "/build/")
_LEGACY_URL_TOKENS = ("build.php",)


def _slugify_host(host: str) -> str:
    """'ts30.x3.europe.travian.com' -> 'ts30.x3.europe.travian.com'."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in host)


def host_from_url(url: str | None) -> str:
    """Hostname pulito da un URL qualsiasi ('' se non estraibile)."""
    u = str(url or "").strip()
    if not u:
        return ""
    if "://" not in u:
        u = "https://" + u
    try:
        return (urlparse(u).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def profile_path(host: str) -> Path:
    return STATE_DIR / f"server_profile_{_slugify_host(host)}.json"


@dataclass
class ServerProfile:
    """Calibrazioni per-server; serializzato in state/server_profile_*.json."""

    host: str = ""
    routing_mode: str = "unknown"  # unknown | legacy | modern
    building_url_tokens: list[str] = field(
        default_factory=lambda: ["build.php", "/building", "/build/", "/slot"]
    )
    empty_slot_gids: list = field(default_factory=lambda: [None, -1])
    preferred_selectors: dict[str, str] = field(default_factory=dict)
    last_calibrated_at: str | None = None
    version: int = PROFILE_VERSION
    # Contatore delle calibrazioni applicate in vita del profilo:
    # indicizzato nei meta delle fixture catturate per correlazione.
    calibrations: int = 0

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    @classmethod
    def load(cls, host: str) -> "ServerProfile":
        """Profilo dell'host; default pulito se assente/illeggibile."""
        path = profile_path(host)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(host=host)
        if not isinstance(data, dict):
            return cls(host=host)
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known}
        try:
            prof = cls(**clean)
        except TypeError:
            return cls(host=host)
        prof.host = str(prof.host or host)
        if not isinstance(prof.empty_slot_gids, list):
            prof.empty_slot_gids = [None, -1]
        if not isinstance(prof.preferred_selectors, dict):
            prof.preferred_selectors = {}
        return prof

    def save(self) -> None:
        """Scrittura atomica del profilo (best-effort, mai bloccante)."""
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            path = profile_path(self.host)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            logger.debug(
                "Salvataggio profilo server %s fallito", self.host,
                exc_info=True,
            )

    def _touch(self) -> None:
        self.last_calibrated_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        self.calibrations += 1
        self.save()

    # ------------------------------------------------------------------
    # Calibrazioni
    # ------------------------------------------------------------------
    def calibrate_empty_gid(self, gid: int) -> bool:
        """Registra ``gid`` come marker di slot libero. True se nuovo."""
        gid = int(gid)
        if gid in self.empty_slot_gids:
            return False
        self.empty_slot_gids.append(gid)
        self._touch()
        return True

    def is_empty_gid(self, gid) -> bool:
        """True se ``gid`` marca uno slot libero su questo server."""
        if gid is None:
            return None in self.empty_slot_gids
        try:
            return int(gid) in [g for g in self.empty_slot_gids if g is not None]
        except (TypeError, ValueError):
            return False

    def set_routing_mode(self, mode: str) -> bool:
        """Imposta 'legacy'/'modern'; True se il valore e' cambiato."""
        mode = str(mode or "").strip().lower()
        if mode not in ("legacy", "modern") or mode == self.routing_mode:
            return False
        self.routing_mode = mode
        self._touch()
        return True

    def promote_selector(self, key: str, selector: str) -> bool:
        """Salva il selettore vincente di una lista di fallback."""
        key = str(key or "").strip()
        selector = str(selector or "").strip()
        if not key or not selector:
            return False
        if self.preferred_selectors.get(key) == selector:
            return False
        self.preferred_selectors[key] = selector
        self._touch()
        return True

    def ordered_selectors(
        self, key: str, selectors: list[str]
    ) -> list[str]:
        """Lista di fallback riordinata: il selettore promosso prima.

        Il preferred e' sempre seguito dalla lista completa: se il
        markup e' cambiato di nuovo, i fallback continuano a coprire.
        """
        items = [str(s) for s in (selectors or []) if s]
        pref = self.preferred_selectors.get(str(key or ""))
        if pref and pref in items:
            return [pref] + [s for s in items if s != pref]
        return items


# Cache in-process: un profilo per host, caricato alla prima richiesta.
_profiles: dict[str, ServerProfile] = {}


def profile_for_host(host: str, *, refresh: bool = False) -> ServerProfile:
    """Profilo dell'host (cache in-process, ricarica da disco se refresh)."""
    host = (host or "").lower()
    if not host:
        host = "unknown"
    if refresh or host not in _profiles:
        _profiles[host] = ServerProfile.load(host)
    return _profiles[host]


def profile_for_url(url: str | None, *, refresh: bool = False) -> ServerProfile:
    """Profilo derivato dall'hostname di un URL/server_url."""
    return profile_for_host(host_from_url(url), refresh=refresh)


# JS condiviso: ispeziona gli href degli slot/campi di dorf1/dorf2 e
# classifica il routing degli edifici. Scansione globale dei link a
# build.php|/slot|/building|/build/ per coprire markup diversi.
_JS_ROUTING_PROBE = """
    () => {
        let modern = 0, legacy = 0;
        for (const a of document.querySelectorAll('a[href], area[href]')) {
            const h = (a.getAttribute('href') || '').toLowerCase();
            if (!h) continue;
            if (/\\/(building|slot)\\?|build\\.php\\?|\\/build\\/\\d/.test(h)) {
                if (h.includes('build.php')) legacy++;
                else if (/\\/(building|slot)\\?|\\/build\\/\\d/.test(h)) modern++;
            }
        }
        if (modern > 0) return 'modern';
        if (legacy > 0) return 'legacy';
        return null;
    }
"""


async def calibrate_routing(
    page, profile: ServerProfile | None = None
) -> str:
    """Rileva routing_mode dagli href della pagina corrente (dorf1/dorf2).

    No-op se la pagina non ha link edificio o il profilo e' gia'
    calibrato (il valore persiste: il probe gira solo mentre
    ``routing_mode == 'unknown'``). Ritorna il modo corrente.
    """
    if profile is None:
        profile = profile_for_url(getattr(page, "url", ""))
    if profile.routing_mode != "unknown":
        return profile.routing_mode
    try:
        mode = await page.evaluate(_JS_ROUTING_PROBE)
    except Exception:  # noqa: BLE001
        return profile.routing_mode
    if mode in ("legacy", "modern"):
        profile.set_routing_mode(mode)
        logger.info(
            "[SELF-HEAL] routing_mode=%s calibrato per %s",
            mode,
            profile.host,
        )
    return profile.routing_mode
