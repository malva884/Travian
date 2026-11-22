"""Radar Movement Store — vettori di movimento live per il RADAR SETTORE.

Fonte unica dei movimenti mostrati sul radar del Command Center:
mercanti in viaggio, avventure eroe, raid/attacchi in uscita e attacchi
in arrivo. Ogni villaggio espone una lista standardizzata:

    {"id": "mov_12345", "category": "merchant|hero_adventure|
      outgoing_raid|outgoing_attack|incoming_attack",
     "direction": "outbound|inbound|return",
     "from_x": 78, "from_y": -7, "to_x": 97, "to_y": -22,
     "label": "Pizzo (97|-22)", "started_at": 1795218000,
     "arrives_at": 1795219800,
     "details": {"resources": {...}, "troops_summary": "15 Imperiani"}}

Registrazione zero-overhead: i movimenti vengono rilevati dalle pagine
gia' visitate dal ciclo (Punto di Raduno, Mercato, avventure eroe) o
registrati direttamente dalle azioni di invio — nessuna navigazione
extra. La persistenza usa il KV SQLite di ``db.py`` (chiave
``radar_<acc>.json``) con fallback su ``state/radar_<acc>.json``, cosi' i
vettori sopravvivono ai riavvii del worker fino a ``arrives_at``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from .config import BASE_DIR
from . import db

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Categorie canoniche (devono combaciare col renderer frontend).
CAT_MERCHANT = "merchant"
CAT_HERO_ADVENTURE = "hero_adventure"
CAT_OUTGOING_RAID = "outgoing_raid"
CAT_OUTGOING_ATTACK = "outgoing_attack"
CAT_INCOMING_ATTACK = "incoming_attack"
CAT_REINFORCEMENT = "reinforcement"
VALID_CATEGORIES = frozenset({
    CAT_MERCHANT,
    CAT_HERO_ADVENTURE,
    CAT_OUTGOING_RAID,
    CAT_OUTGOING_ATTACK,
    CAT_INCOMING_ATTACK,
    CAT_REINFORCEMENT,
})

VALID_DIRECTIONS = frozenset({"outbound", "inbound", "return"})

# Le righe della panoramica Punto di Raduno variano il countdown
# (arrives_at = now + seconds deriva di pochi secondi tra un ciclo e
# l'altro): l'id e' costruito su un bucket di 60s per deduplicare i
# sync passivi senza perdere l'aggiornamento dell'ETA.
_ETA_BUCKET_SECONDS = 60.0

# Tolleranza dedup fuzzy: un movimento osservato dal sync passivo e'
# lo stesso di uno registrato dall'azione bot se condividono categoria,
# direzione e arrives_at entro questa finestra.
_DEDUP_ETA_TOLERANCE = 15.0


def make_move_id(
    village_key: str, category: str, counterpart: str, arrives_at: float
) -> str:
    """Id deterministico di un movimento (dedup sync + action)."""
    bucket = int(round(float(arrives_at or 0.0) / _ETA_BUCKET_SECONDS))
    parts = [
        str(village_key or ""),
        str(category or ""),
        str(counterpart or "")[:60].replace(" ", "_"),
        str(bucket),
    ]
    return "mov_" + "_".join(p for p in parts if p)


def _int_or_none(val) -> int | None:
    try:
        if val is None or val == "":
            return None
        return int(val)
    except (TypeError, ValueError):
        return None


def normalize_movement(
    village_key: str,
    data: dict,
    *,
    default_started: float | None = None,
) -> dict | None:
    """Valida e normalizza un dict movimento grezzo -> schema radar.

    Ritorna None per input non movimento (manca arrives_at o category
    sconosciuta). ``default_started`` copre i movimenti osservati in
    corsa (non registrati alla partenza): il renderer li proietta dal
    bordo radar con progresso 0.
    """
    if not isinstance(data, dict):
        return None
    category = str(data.get("category") or "")
    if category not in VALID_CATEGORIES:
        return None
    arrives_at = float(data.get("arrives_at") or 0.0)
    if arrives_at <= 0:
        return None
    direction = str(data.get("direction") or "")
    if direction not in VALID_DIRECTIONS:
        direction = (
            "outbound"
            if category in (CAT_MERCHANT, CAT_OUTGOING_RAID,
                            CAT_OUTGOING_ATTACK, CAT_HERO_ADVENTURE)
            else "inbound"
        )
    counterpart = str(
        data.get("counterpart")
        or data.get("label")
        or ""
    )[:80]
    move = {
        "id": str(
            data.get("id")
            or make_move_id(village_key, category, counterpart, arrives_at)
        ),
        "category": category,
        "direction": direction,
        "from_x": _int_or_none(data.get("from_x")),
        "from_y": _int_or_none(data.get("from_y")),
        "to_x": _int_or_none(data.get("to_x")),
        "to_y": _int_or_none(data.get("to_y")),
        "label": str(data.get("label") or counterpart)[:90],
        "counterpart": counterpart,
        "started_at": float(
            data.get("started_at") or default_started or 0.0
        ),
        "arrives_at": arrives_at,
        "details": {
            "resources": dict(
                (data.get("details") or {}).get("resources") or {}
            ),
            "troops_summary": str(
                (data.get("details") or {}).get("troops_summary") or ""
            )[:80],
        },
    }
    return move


class RadarStore:
    """Persistenza dei vettori movimento per account.

    Struttura: ``{village_key: {move_id: movement}}`` dove village_key
    e' il nome del villaggio (stessa chiave del payload telemetria).
    Thread-safe e multi-processo: ogni accesso pubblico rilegge lo
    storage (KV SQLite con fallback file), cosi' agente e dashboard
    condividono gli stessi vettori senza lost-update.
    """

    def __init__(self, account_id: str, path: Path | None = None) -> None:
        self.account_id = account_id
        self._lock = threading.RLock()
        self._db_key: str | None = None
        if path is None:
            safe = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in account_id
            )
            self._db_key = safe
            path = STATE_DIR / f"radar_{safe}.json"
        self.path = path
        self._data: dict[str, dict[str, dict]] = {}
        self._load()

    # --------------------------------------------------------------
    # Persistenza
    # --------------------------------------------------------------
    def _load(self) -> None:
        raw: str | None = None
        if self._db_key is not None:
            try:
                raw = db.kv_read(f"radar_{self._db_key}.json")
            except Exception:  # noqa: BLE001
                raw = None
        if raw is None:
            try:
                raw = self.path.read_text(encoding="utf-8")
            except (OSError, ValueError):
                raw = None
        data: dict[str, dict[str, dict]] = {}
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        if isinstance(parsed, dict):
            for vkey, moves in parsed.items():
                if not isinstance(moves, dict):
                    continue
                bucket: dict[str, dict] = {}
                for mid, mv in moves.items():
                    norm = normalize_movement(
                        str(vkey), mv, default_started=0.0
                    )
                    if norm is not None:
                        bucket[str(mid)] = norm
                if bucket:
                    data[str(vkey)] = bucket
        self._data = data

    def _save(self) -> None:
        try:
            payload = json.dumps(self._data, ensure_ascii=False, indent=1)
            if self._db_key is not None:
                db.kv_write(f"radar_{self._db_key}.json", payload)
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(self.path)
        except Exception:  # noqa: BLE001
            logger.debug("Salvataggio radar store fallito", exc_info=True)

    # --------------------------------------------------------------
    # API pubblica
    # --------------------------------------------------------------
    def register(self, village_key: str, movement: dict) -> dict | None:
        """Registra/aggiorna un movimento. Ritorna il dict normalizzato."""
        norm = normalize_movement(str(village_key), movement)
        if norm is None:
            return None
        with self._lock:
            self._load()
            bucket = self._data.setdefault(str(village_key), {})
            prev = bucket.get(norm["id"])
            # Un sync passivo non deve sovrascrivere started_at gia'
            # noto con un default meno preciso.
            if prev and not norm.get("started_at"):
                norm["started_at"] = prev.get("started_at") or 0.0
            bucket[norm["id"]] = norm
            self._prune_bucket(bucket)
            self._save()
        return norm

    def register_many(
        self, village_key: str, movements: list[dict]
    ) -> int:
        """Registra una lista di movimenti in un colpo solo."""
        count = 0
        with self._lock:
            for mv in movements or []:
                norm = normalize_movement(str(village_key), mv)
                if norm is None:
                    continue
                self._load()
                bucket = self._data.setdefault(str(village_key), {})
                prev = bucket.get(norm["id"])
                if prev and not norm.get("started_at"):
                    norm["started_at"] = prev.get("started_at") or 0.0
                bucket[norm["id"]] = norm
                count += 1
            if count:
                self._prune_bucket(self._data[str(village_key)])
                self._save()
        return count

    def sync_categories(
        self,
        village_key: str,
        movements: list[dict],
        categories: frozenset[str],
    ) -> int:
        """Sincronizza una categoria: le chiavi non piu' osservate e
        appartenenti a ``categories`` vengono rimosse (il movimento e'
        arrivato/completato). Solo per i sync passivi delle pagine
        rilette ad ogni ciclo (rally overview, mercato)."""
        count = 0
        vkey = str(village_key)
        with self._lock:
            self._load()
            bucket = self._data.setdefault(vkey, {})
            seen: set[str] = set()
            for mv in movements or []:
                norm = normalize_movement(vkey, mv)
                if norm is None:
                    continue
                # Dedup fuzzy vs movimenti registrati dalle azioni bot:
                # stesso vettore (categoria+direzione+ETA ~uguale) ma id
                # diverso per controparte label differente -> riuso
                # l'id esistente invece di creare un duplicato.
                if norm["id"] not in bucket:
                    arr = float(norm.get("arrives_at") or 0.0)
                    for mid, prev_mv in bucket.items():
                        if (
                            prev_mv.get("category") == norm["category"]
                            and prev_mv.get("direction")
                            == norm["direction"]
                            and abs(
                                float(prev_mv.get("arrives_at") or 0.0)
                                - arr
                            )
                            <= _DEDUP_ETA_TOLERANCE
                            and mid not in seen
                        ):
                            norm["id"] = mid
                            break
                prev = bucket.get(norm["id"])
                if prev and not norm.get("started_at"):
                    norm["started_at"] = prev.get("started_at") or 0.0
                bucket[norm["id"]] = norm
                seen.add(norm["id"])
                count += 1
            stale = [
                mid
                for mid, mv in bucket.items()
                if mv.get("category") in categories and mid not in seen
            ]
            for mid in stale:
                del bucket[mid]
            self._prune_bucket(bucket)
            if count or stale:
                self._save()
        return count

    def movements_for(
        self, village_key: str, *, now: float | None = None
    ) -> list[dict]:
        """Vettori attivi del villaggio, ordinati per arrivo."""
        now = float(now if now is not None else time.time())
        with self._lock:
            self._load()
            bucket = self._data.get(str(village_key)) or {}
            out = [
                mv
                for mv in bucket.values()
                if float(mv.get("arrives_at") or 0.0) > now
            ]
        out.sort(key=lambda m: float(m.get("arrives_at") or 0.0))
        return [dict(m) for m in out]

    def prune(self, *, now: float | None = None) -> int:
        """Rimuove i movimenti scaduti (arrives_at <= now)."""
        now = float(now if now is not None else time.time())
        removed = 0
        with self._lock:
            self._load()
            for bucket in self._data.values():
                before = len(bucket)
                self._prune_bucket(bucket, now=now)
                removed += before - len(bucket)
            self._data = {
                k: b for k, b in self._data.items() if b
            }
            if removed:
                self._save()
        return removed

    @staticmethod
    def _prune_bucket(
        bucket: dict[str, dict], now: float | None = None
    ) -> None:
        now = float(now if now is not None else time.time())
        stale = [
            mid
            for mid, mv in bucket.items()
            if float(mv.get("arrives_at") or 0.0) <= now
        ]
        for mid in stale:
            del bucket[mid]


_stores: dict[str, RadarStore] = {}


def radar_store(account_id: str) -> RadarStore:
    """Istanza condivisa dello store radar per l'account."""
    key = str(account_id or "default")
    if key not in _stores:
        _stores[key] = RadarStore(key)
    return _stores[key]


def merge_village_movements(
    store: RadarStore | None,
    village_key: str,
    extra: list[dict] | None,
    *,
    now: float | None = None,
) -> list[dict]:
    """Vettori persistiti + movimenti live non persistiti (dedup id)."""
    seen: dict[str, dict] = {}
    if store is not None:
        for mv in store.movements_for(village_key, now=now):
            seen[str(mv.get("id") or "")] = dict(mv)
    for mv in extra or []:
        norm = normalize_movement(village_key, mv)
        if norm is not None:
            seen.setdefault(norm["id"], norm)
    out = list(seen.values())
    out.sort(key=lambda m: float(m.get("arrives_at") or 0.0))
    return out
