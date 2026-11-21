"""Automatic Anomaly Fixture Capture.

Agganciato ai codici di fallimento strutturali di ``actions.py`` e
``sensors.py`` (INVALID_SLOT_OCCUPIED, BTN_NOT_FOUND, QUEUE_UNCONFIRMED,
MARKET_FORM_MISSING, FIELDS_EMPTY, ...). Per ogni anomalia salva in
``tests/fixtures/captured/`` due file:

- ``<ts>_<error_code>_<host>.html``: ``page.content()`` sanitizzato
  (token di sessione, cookie inline, email; coordinate solo se
  ``TRAVIAN_CAPTURE_SCRUB_COORDS=1``);
- ``<ts>_<error_code>_<host>.meta.json``: URL, error_code, village_id,
  slot_id, expected_gid, detected_gid, versione profilo e le
  ``expected`` usate dal replay harness ``tests/test_captured_fixtures.py``.

Rate-limit deterministico: max 1 dump per ``<error_code>:<slot_or_gid>``
per host ogni 6 ore (indice persistito in
``state/capture_dedup.json`` — sopravvive ai riavvii del container).
Disattivabile con ``TRAVIAN_CAPTURE_FIXTURES=0``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import BASE_DIR
from .server_profile import ServerProfile, host_from_url, profile_for_host

logger = logging.getLogger(__name__)

FIXTURES_DIR = BASE_DIR / "tests" / "fixtures" / "captured"
_DEDUP_PATH = BASE_DIR / "state" / "capture_dedup.json"

# Max 1 dump per <error_code>:<key> per host in questa finestra.
DEDUP_WINDOW_SECONDS = 6 * 3600.0

_SAFE_TAG = re.compile(r"[^A-Za-z0-9._-]+")
# Token di sessione / credenziali: query params, JSON e cookie inline.
# Richiede '=' dopo il nome: senza boundary falsi positivi su parole
# comuni ('conSIDering', 'AUTHor', 'oTOKENe').
_RE_SESSION_QS = re.compile(
    r"(?i)\b(sessid|session_id|sid|token|auth|csrf|ajaxtoken|"
    r"access_token|password|passwd|pwd)\s*=\s*[\"']?[^&\"'<>\s,;]{4,}"
)
_RE_COOKIE = re.compile(
    r"(?i)((?:cookie|document\.cookie)[^\"'<>\n]{0,120})"
)
_RE_JSON_TOKEN = re.compile(
    r"(?i)(\"(?:sessid|session_id|token|csrf|password|passwd|pwd)\"\s*:"
    r"\s*\")[^\"]*(\")"
)
_RE_EMAIL = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
)
# Coordinate sensibili "(x|y)" — solo su opt-in: i test di replay su
# fixture di mercato potrebbero dipendere dalle coordinate nel DOM.
_RE_COORDS = re.compile(r"\(\s*-?\d{1,4}\s*\|\s*-?\d{1,4}\s*\)")


def _scrub_enabled() -> bool:
    return os.getenv("TRAVIAN_CAPTURE_SCRUB_COORDS", "0") == "1"


def capture_enabled() -> bool:
    """Kill-switch globale: ``TRAVIAN_CAPTURE_FIXTURES=0`` disattiva."""
    return os.getenv("TRAVIAN_CAPTURE_FIXTURES", "1") != "0"


def sanitize_html(html: str, *, scrub_coords: bool | None = None) -> str:
    """Rimuove token di sessione, cookie, email (e coordinate su opt-in)."""
    out = str(html or "")
    out = _RE_SESSION_QS.sub(r"\1=<REDACTED>", out)
    out = _RE_JSON_TOKEN.sub(r"\1<REDACTED>\2", out)
    out = _RE_COOKIE.sub("[COOKIE_REDACTED]", out)
    out = _RE_EMAIL.sub("[EMAIL_REDACTED]", out)
    if _scrub_enabled() if scrub_coords is None else scrub_coords:
        out = _RE_COORDS.sub("(0|0)", out)
    return out


# ---------------------------------------------------------------------------
# Deduplicazione persistente
# ---------------------------------------------------------------------------

def _load_dedup() -> dict:
    try:
        data = json.loads(_DEDUP_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_dedup(data: dict) -> None:
    try:
        _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DEDUP_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(_DEDUP_PATH)
    except OSError:
        logger.debug("Salvataggio dedup capture fallito", exc_info=True)


def _dedup_hit(host: str, error_code: str, key: str, now: float) -> bool:
    """True se esiste gia' un dump recente per host+code+key.

    Registra ``now`` quando ritorna False (nuova cattura consentita).
    """
    dkey = f"{error_code}:{key}"
    data = _load_dedup()
    entries = data.setdefault(host, {})
    last = float(entries.get(dkey) or 0.0)
    if last and now - last < DEDUP_WINDOW_SECONDS:
        return True
    entries[dkey] = now
    _save_dedup(data)
    return False


# ---------------------------------------------------------------------------
# Guard mirrorati in Python puro (replay harness senza browser)
# ---------------------------------------------------------------------------

_RE_GID_CLASS = re.compile(r"\bgid(\d+)\b|\bg(\d+)\b")
_RE_OPEN_TAG = re.compile(r"<[a-zA-Z][a-zA-Z0-9]*(?:\s[^>]*)?/?>")
_RE_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def _tag_attrs(tag_html: str) -> dict:
    """Attributi di un open-tag come dict (chiavi lowercase)."""
    return {
        m.group(1).lower(): m.group(2)
        for m in _RE_ATTR.finditer(tag_html)
    }


def detect_gid_in_html(html: str) -> int | None:
    """Controparte statica di ``actions._detect_page_gid``.

    Replica l'ordine di ispezione dello snippet JS (#build, #contract,
    .contractLink, #content .building/.buildingImage) cercando le classi
    ``gidN``/``gN`` nel markup. Usato al capture-time per ``detected_gid``
    e dal replay harness per verificare che il parser trovi lo stesso gid.
    """
    text = str(html or "")
    if not text:
        return None
    tags = _RE_OPEN_TAG.findall(text)

    def _gid(cls_val: str) -> int | None:
        g = _RE_GID_CLASS.search(cls_val or "")
        return int(g.group(1) or g.group(2)) if g else None

    # 1-2) elementi con id="build" / id="contract"
    for t in tags:
        attrs = _tag_attrs(t)
        if attrs.get("id") in ("build", "contract"):
            gid = _gid(attrs.get("class", ""))
            if gid is not None:
                return gid
    # 3) .contractLink
    for t in tags:
        attrs = _tag_attrs(t)
        cls = attrs.get("class", "")
        if "contractLink" in cls:
            gid = _gid(cls)
            if gid is not None:
                return gid
    # 4) qualsiasi elemento con classe .building/.buildingImage
    for t in tags:
        attrs = _tag_attrs(t)
        cls = attrs.get("class", "")
        if re.search(r"\bbuilding(Image)?\b", cls):
            gid = _gid(cls)
            if gid is not None:
                return gid
    return None


# ---------------------------------------------------------------------------
# Scrittura fixture
# ---------------------------------------------------------------------------

def write_capture(
    html: str,
    *,
    error_code: str,
    url: str,
    village_id: int | str = 0,
    slot_id: int | None = None,
    expected_gid: int | None = None,
    detected_gid: int | None = None,
    host: str | None = None,
    profile: ServerProfile | None = None,
    extra: dict | None = None,
    now: float | None = None,
) -> Path | None:
    """Salva la coppia .html + .meta.json; None se deduplicata/disattiva.

    ``error_code`` e ``key`` (slot o gid) compongono la chiave di
    deduplicazione ``<error_code>:<key>`` — stessa convenzione dei log.
    """
    if not capture_enabled():
        return None
    host = (host or host_from_url(url) or "unknown").lower()
    code = _SAFE_TAG.sub("_", str(error_code or "UNKNOWN")).strip("_") or "UNKNOWN"
    key = str(slot_id) if slot_id is not None else (
        str(expected_gid) if expected_gid is not None else "global"
    )
    now = time.time() if now is None else float(now)
    if _dedup_hit(host, code, key, now):
        logger.debug(
            "Capture %s:%s su %s deduplicata (finestra 6h)", code, key, host
        )
        return None

    if profile is None:
        profile = profile_for_host(host)
    if detected_gid is None:
        detected_gid = detect_gid_in_html(html)

    stamp = datetime.fromtimestamp(now, tz=timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    safe_host = _SAFE_TAG.sub("_", host)
    base = f"{stamp}_{code}_{safe_host}"

    # ``expected``: contratto consumato dal replay harness. Gli assert
    # sono calcolati a capture-time con gli stessi guard di actions.py
    # (mirrorati in Python), cosi' il test fallisce solo se i parser
    # cambiano comportamento sullo stesso HTML.
    expected: dict = {"dom_gid": detected_gid}
    try:
        from .actions import _is_building_url, _slot_url_check  # no cicli

        expected["url_is_building"] = _is_building_url(url)
        if slot_id is not None:
            expected["slot_url_match"] = bool(
                _slot_url_check(int(slot_id)).search(url or "")
            )
    except Exception:  # noqa: BLE001
        pass

    meta = {
        "captured_at": datetime.fromtimestamp(
            now, tz=timezone.utc
        ).isoformat(timespec="seconds"),
        "host": host,
        "url": str(url or ""),
        "error_code": code,
        "village_id": int(village_id or 0) if str(village_id or "").lstrip("-").isdigit() else 0,
        "slot_id": slot_id,
        "expected_gid": expected_gid,
        "detected_gid": detected_gid,
        "dedup_key": f"{code}:{key}",
        "profile_version": int(getattr(profile, "version", 0) or 0),
        "profile_calibrations": int(
            getattr(profile, "calibrations", 0) or 0
        ),
        "routing_mode": str(getattr(profile, "routing_mode", "unknown")),
        "empty_slot_gids": list(getattr(profile, "empty_slot_gids", []) or []),
        "expected": expected,
    }
    if extra:
        meta["extra"] = dict(extra)

    try:
        FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
        (FIXTURES_DIR / f"{base}.html").write_text(
            sanitize_html(html), encoding="utf-8"
        )
        (FIXTURES_DIR / f"{base}.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        logger.debug("Scrittura fixture %s fallita", base, exc_info=True)
        return None

    logger.warning(
        "[CAPTURE] Fixture anomalia %s su %s -> tests/fixtures/captured/%s.*",
        code,
        host,
        base,
    )
    return FIXTURES_DIR / f"{base}.meta.json"


async def capture_anomaly(
    page,
    error_code: str,
    *,
    village_id: int | str = 0,
    slot_id: int | None = None,
    expected_gid: int | None = None,
    detected_gid: int | None = None,
    profile: ServerProfile | None = None,
    extra: dict | None = None,
) -> Path | None:
    """Cattura page.content() corrente come fixture di regressione.

    Mai bloccante: qualunque errore di lettura/scrittura viene solo
    loggato — la cattura non deve alterare il flusso dell'agente.
    """
    try:
        html = await page.content()
    except Exception:  # noqa: BLE001
        return None
    try:
        url = str(page.url or "")
    except Exception:  # noqa: BLE001
        url = ""
    return write_capture(
        html,
        error_code=error_code,
        url=url,
        village_id=village_id,
        slot_id=slot_id,
        expected_gid=expected_gid,
        detected_gid=detected_gid,
        profile=profile,
        extra=extra,
    )
