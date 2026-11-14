"""Persistenza centrale: SQLite su ``state/travian.db`` (modalita' WAL).

Consolida lo stato runtime dell'agente, prima sparso in JSON/JSONL
sotto ``state/``, in un unico database relazionale leggibile da agente
e dashboard (processi separati, connessione per operazione).

Tabelle:

- ``kv_state`` — documenti JSON dei manager a stato (scout, hero,
  farming, defense, expansion, replenish, war_targets, build_plan,
  expansion_config). Chiave = nome del file legacy (es.
  ``scout_Europa.json``): la migrazione importa il contenuto 1:1.
- ``farm_targets`` — una riga per target di saccheggio per account
  (colonne interrogabili direttamente dalla dashboard).
- ``cropper_candidates`` — una riga per tile cropper per account.
- ``village_snapshots`` — storico decisionale per-villaggio
  (ex ``state/history/history_<acc>.jsonl``): JSON completo in ``data``
  piu' colonne indicizzate per la query "ultimo snapshot".

Migrazione: al primo accesso i file JSON esistenti vengono importati
una sola volta e spostati in ``state/legacy/`` (flag ``_migration_v1``
in ``kv_state``). I costruttori dei manager con ``state_path``/``path``
esplicito continuano a usare il filesystem (test e override manuali);
il path default usa il DB.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .config import BASE_DIR

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"
DB_PATH = STATE_DIR / "travian.db"
LEGACY_DIR = STATE_DIR / "legacy"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS farm_targets (
    account_id TEXT NOT NULL,
    id TEXT NOT NULL,
    x INTEGER, y INTEGER,
    name TEXT, is_oasis INTEGER DEFAULT 0,
    population INTEGER DEFAULT 0, estimated_loot INTEGER DEFAULT 0,
    last_loot_ratio REAL DEFAULT 1.0, losses_recorded INTEGER DEFAULT 0,
    distance REAL DEFAULT 0.0, nearest_village TEXT DEFAULT '',
    status TEXT DEFAULT 'pending', manual INTEGER DEFAULT 0,
    updated_ts REAL DEFAULT 0,
    ord INTEGER DEFAULT 0,
    target_type TEXT DEFAULT '', last_raid_ts REAL DEFAULT 0,
    last_outcome TEXT DEFAULT '', fail_reason TEXT DEFAULT '',
    PRIMARY KEY (account_id, id)
);
CREATE TABLE IF NOT EXISTS cropper_candidates (
    account_id TEXT NOT NULL,
    id TEXT NOT NULL,
    x INTEGER, y INTEGER, type TEXT DEFAULT '',
    bonus_crop_percent INTEGER DEFAULT 0,
    distance_hours REAL DEFAULT 0.0, nearest_village TEXT DEFAULT '',
    oases TEXT DEFAULT '[]',
    status TEXT DEFAULT 'pending', manual INTEGER DEFAULT 0,
    updated_ts REAL DEFAULT 0,
    ord INTEGER DEFAULT 0,
    PRIMARY KEY (account_id, id)
);
CREATE TABLE IF NOT EXISTS village_snapshots (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id TEXT NOT NULL,
    village_name TEXT,
    ts REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vs_acc ON village_snapshots(account_id, village_name, ts);
"""

_MIGRATION_FLAG = "_migration_v1"
_initialized = False


@contextmanager
def _db():
    """Connessione breve per operazione: sicura tra thread e processi."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        _ensure_ready(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Colonne farm_targets aggiunte dopo lo schema iniziale: ALTER idempotente
# per i DB creati prima dell'introduzione dei campi esito-raid.
_FARM_LATE_COLS = (
    ("target_type", "TEXT DEFAULT ''"),
    ("last_raid_ts", "REAL DEFAULT 0"),
    ("last_outcome", "TEXT DEFAULT ''"),
    ("fail_reason", "TEXT DEFAULT ''"),
)


def _ensure_farm_columns(conn: sqlite3.Connection) -> None:
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(farm_targets)")
    }
    for name, ddl in _FARM_LATE_COLS:
        if name not in cols:
            conn.execute(
                f"ALTER TABLE farm_targets ADD COLUMN {name} {ddl}"
            )


def _ensure_ready(conn: sqlite3.Connection) -> None:
    """Schema + WAL + migrazione legacy, una sola volta per processo."""
    global _initialized
    if _initialized:
        return
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_SCHEMA)
    _ensure_farm_columns(conn)
    _migrate_legacy(conn)
    conn.commit()
    _initialized = True


# ----------------------------------------------------------------------
# KV generico: documenti JSON dei manager a stato (chiave = nome file)
# ----------------------------------------------------------------------

def kv_read(key: str) -> str | None:
    """Contenuto JSON testuale associato alla chiave, o None."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT data FROM kv_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row[0]) if row else None
    except Exception:
        logger.debug("kv_read(%s) fallito", key, exc_info=True)
        return None


def kv_write(key: str, data: str) -> None:
    """Salva un documento JSON (testo) sotto la chiave data."""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO kv_state (key, data, updated_ts) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET data=excluded.data,"
                " updated_ts=excluded.updated_ts",
                (key, data, time.time()),
            )
    except Exception:
        logger.debug("kv_write(%s) fallito", key, exc_info=True)


# ----------------------------------------------------------------------
# Farm targets: tabella interrogabile, ordine lista preservato in `ord`
# ----------------------------------------------------------------------

def farm_load(acc_key: str) -> list[dict]:
    """Righe del catalogo farm dell'account nello schema del JSON legacy."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT id, x, y, name, is_oasis, population, estimated_loot,"
                " last_loot_ratio, losses_recorded, distance, nearest_village,"
                " status, manual, updated_ts, target_type, last_raid_ts,"
                " last_outcome, fail_reason"
                " FROM farm_targets WHERE account_id = ? ORDER BY ord",
                (acc_key,),
            ).fetchall()
        return [
            {
                "id": r[0], "x": r[1], "y": r[2], "name": r[3] or "",
                "is_oasis": bool(r[4]), "population": r[5] or 0,
                "estimated_loot": r[6] or 0,
                "last_loot_ratio": r[7] if r[7] is not None else 1.0,
                "losses_recorded": r[8] or 0, "distance": r[9] or 0.0,
                "nearest_village": r[10] or "", "status": r[11] or "pending",
                "manual": bool(r[12]), "updated_ts": r[13] or 0.0,
                "target_type": r[14] or "", "last_raid_ts": r[15] or 0.0,
                "last_outcome": r[16] or "", "fail_reason": r[17] or "",
            }
            for r in rows
        ]
    except Exception:
        logger.debug("farm_load(%s) fallito", acc_key, exc_info=True)
        return []


def farm_save(acc_key: str, rows: list[dict]) -> None:
    """Riscrive il catalogo farm dell'account (semantica last-writer-wins,
    come il file JSON)."""
    try:
        with _db() as conn:
            conn.execute(
                "DELETE FROM farm_targets WHERE account_id = ?", (acc_key,)
            )
            conn.executemany(
                "INSERT INTO farm_targets (account_id, id, x, y, name,"
                " is_oasis, population, estimated_loot, last_loot_ratio,"
                " losses_recorded, distance, nearest_village, status,"
                " manual, updated_ts, ord, target_type, last_raid_ts,"
                " last_outcome, fail_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        acc_key, str(r.get("id") or ""),
                        int(r.get("x") or 0), int(r.get("y") or 0),
                        str(r.get("name") or ""),
                        1 if r.get("is_oasis") else 0,
                        int(r.get("population") or 0),
                        int(r.get("estimated_loot") or 0),
                        float(r.get("last_loot_ratio") or 1.0),
                        int(r.get("losses_recorded") or 0),
                        float(r.get("distance") or 0.0),
                        str(r.get("nearest_village") or ""),
                        str(r.get("status") or "pending"),
                        1 if r.get("manual") else 0,
                        float(r.get("updated_ts") or 0.0),
                        i,
                        str(r.get("target_type") or ""),
                        float(r.get("last_raid_ts") or 0.0),
                        str(r.get("last_outcome") or ""),
                        str(r.get("fail_reason") or ""),
                    )
                    for i, r in enumerate(rows)
                ],
            )
    except Exception:
        logger.debug("farm_save(%s) fallito", acc_key, exc_info=True)


# Campi modificabili con UPDATE mirato (whitelist anti SQL-injection:
# i nomi colonna non arrivano mai dall'esterno).
_FARM_PATCH_COLS = {
    "status", "manual", "updated_ts", "last_raid_ts", "last_outcome",
    "fail_reason", "last_loot_ratio", "losses_recorded", "name",
    "population", "estimated_loot", "distance", "nearest_village",
    "target_type", "is_oasis",
}


def farm_patch(
    acc_key: str,
    fields: dict,
    *,
    target_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
) -> bool:
    """UPDATE mirato per-riga su farm_targets (nessun rewrite del catalogo).

    Identifica la riga per `target_id` oppure per coordinate `(x, y)`.
    Ritorna True se almeno una riga e' stata aggiornata. Pensato per le
    mutazioni frequenti (approve/reject, esito raid): elimina la race
    last-writer-wins del rewrite globale tra agente e dashboard.
    """
    sets: list[str] = []
    vals: list = []
    for col, val in fields.items():
        if col in _FARM_PATCH_COLS:
            sets.append(f"{col} = ?")
            vals.append(val)
    if not sets:
        return False
    where = "account_id = ?"
    params = vals + [acc_key]
    if target_id is not None:
        where += " AND id = ?"
        params.append(str(target_id))
    elif x is not None and y is not None:
        where += " AND x = ? AND y = ?"
        params += [int(x), int(y)]
    else:
        return False
    try:
        with _db() as conn:
            cur = conn.execute(
                f"UPDATE farm_targets SET {', '.join(sets)} WHERE {where}",
                params,
            )
        return cur.rowcount > 0
    except Exception:
        logger.debug("farm_patch(%s) fallito", acc_key, exc_info=True)
        return False


# ----------------------------------------------------------------------
# Cropper candidates: tabella condivisa per tutti gli account
# ----------------------------------------------------------------------

def cropper_load() -> dict[str, list[dict]]:
    """Tutti i candidati come dict account_id -> lista (schema legacy)."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT account_id, id, x, y, type, bonus_crop_percent,"
                " distance_hours, nearest_village, oases, status, manual,"
                " updated_ts FROM cropper_candidates ORDER BY account_id, ord"
            ).fetchall()
        out: dict[str, list[dict]] = {}
        for r in rows:
            try:
                oases = json.loads(r[8] or "[]")
            except (TypeError, ValueError):
                oases = []
            out.setdefault(r[0], []).append(
                {
                    "id": r[1], "x": r[2], "y": r[3], "type": r[4] or "",
                    "bonus_crop_percent": r[5] or 0,
                    "distance_hours": r[6] or 0.0,
                    "nearest_village": r[7] or "",
                    "oases": oases if isinstance(oases, list) else [],
                    "status": r[9] or "pending", "manual": bool(r[10]),
                    "updated_ts": r[11] or 0.0,
                }
            )
        return out
    except Exception:
        logger.debug("cropper_load() fallito", exc_info=True)
        return {}


def cropper_save(data: dict[str, list[dict]]) -> None:
    """Riscrive tutta la watchlist cropper (semantica file unico)."""
    try:
        with _db() as conn:
            conn.execute("DELETE FROM cropper_candidates")
            params: list[tuple] = []
            for acc_id, rows in (data or {}).items():
                for i, r in enumerate(rows):
                    params.append(
                        (
                            str(acc_id), str(r.get("id") or ""),
                            int(r.get("x") or 0), int(r.get("y") or 0),
                            str(r.get("type") or ""),
                            int(r.get("bonus_crop_percent") or 0),
                            float(r.get("distance_hours") or 0.0),
                            str(r.get("nearest_village") or ""),
                            json.dumps(r.get("oases") or [], ensure_ascii=False),
                            str(r.get("status") or "pending"),
                            1 if r.get("manual") else 0,
                            float(r.get("updated_ts") or 0.0),
                            i,
                        )
                    )
            conn.executemany(
                "INSERT INTO cropper_candidates (account_id, id, x, y, type,"
                " bonus_crop_percent, distance_hours, nearest_village, oases,"
                " status, manual, updated_ts, ord)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                params,
            )
    except Exception:
        logger.debug("cropper_save() fallito", exc_info=True)


# ----------------------------------------------------------------------
# Storico decisionale per-villaggio (ex history_*.jsonl)
# ----------------------------------------------------------------------

def history_insert(account_id: str, entry: dict) -> None:
    """Accoda uno snapshot decisionale del villaggio."""
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO village_snapshots (account_id, village_name, ts, data)"
                " VALUES (?,?,?,?)",
                (
                    account_id,
                    str(entry.get("village_name") or ""),
                    float(entry.get("ts") or time.time()),
                    json.dumps(entry, ensure_ascii=False),
                ),
            )
    except Exception:
        logger.debug("history_insert(%s) fallito", account_id, exc_info=True)


def history_latest(account_id: str) -> dict[str, dict]:
    """Ultimo snapshot registrato per ciascun villaggio dell'account."""
    try:
        with _db() as conn:
            rows = conn.execute(
                "SELECT vs.data FROM village_snapshots vs"
                " JOIN (SELECT village_name, MAX(ts) AS mts"
                "       FROM village_snapshots WHERE account_id = ?"
                "       GROUP BY village_name) m"
                " ON vs.village_name = m.village_name AND vs.ts = m.mts"
                " WHERE vs.account_id = ?",
                (account_id, account_id),
            ).fetchall()
        out: dict[str, dict] = {}
        for (raw,) in rows:
            try:
                data = json.loads(raw)
            except (TypeError, ValueError):
                continue
            vname = data.get("village_name") or f"id_{data.get('village_id', '?')}"
            out[vname] = data
        return out
    except Exception:
        logger.debug("history_latest(%s) fallito", account_id, exc_info=True)
        return {}


# ----------------------------------------------------------------------
# Migrazione import-once dei JSON legacy -> state/legacy/
# ----------------------------------------------------------------------

def _archive(path: Path) -> None:
    """Sposta un file importato in state/legacy/ (niente perdita dati)."""
    try:
        LEGACY_DIR.mkdir(parents=True, exist_ok=True)
        dest = LEGACY_DIR / path.name
        if dest.exists():
            dest = LEGACY_DIR / f"{path.stem}_{int(time.time())}{path.suffix}"
        path.replace(dest)
    except OSError:
        logger.debug("Archiviazione %s fallita", path.name, exc_info=True)


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """Importa i JSON di stato esistenti nel DB (una sola volta)."""
    done = conn.execute(
        "SELECT data FROM kv_state WHERE key = ?", (_MIGRATION_FLAG,)
    ).fetchone()
    if done:
        return
    imported: list[str] = []

    # 1. Documenti dei manager a stato -> kv_state (chiave = nome file)
    kv_patterns = (
        "scout_*.json", "hero_*.json", "farming_*.json", "defense_*.json",
        "expansion_*.json", "replenish_*.json", "war_targets_*.json",
        "build_plan_*.json", "expansion_config.json",
    )
    for pattern in kv_patterns:
        for f in STATE_DIR.glob(pattern):
            try:
                text = f.read_text(encoding="utf-8-sig")
                conn.execute(
                    "INSERT INTO kv_state (key, data, updated_ts)"
                    " VALUES (?,?,?)"
                    " ON CONFLICT(key) DO UPDATE SET data=excluded.data,"
                    " updated_ts=excluded.updated_ts",
                    (f.name, text, f.stat().st_mtime),
                )
                _archive(f)
                imported.append(f.name)
            except OSError:
                logger.debug("Import kv %s fallito", f.name, exc_info=True)

    # 2. Cataloghi farm -> farm_targets (chiave = suffisso del filename)
    for f in STATE_DIR.glob("farm_targets_*.json"):
        try:
            raw = json.loads(f.read_text(encoding="utf-8-sig"))
            rows = raw if isinstance(raw, list) else raw.get("targets", [])
            acc_key = f.stem.removeprefix("farm_targets_")
            conn.execute("DELETE FROM farm_targets WHERE account_id = ?", (acc_key,))
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                conn.execute(
                    "INSERT INTO farm_targets (account_id, id, x, y, name,"
                    " is_oasis, population, estimated_loot, last_loot_ratio,"
                    " losses_recorded, distance, nearest_village, status,"
                    " manual, updated_ts, ord)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        acc_key, str(r.get("id") or ""),
                        int(r.get("x") or 0), int(r.get("y") or 0),
                        str(r.get("name") or ""),
                        1 if r.get("is_oasis") else 0,
                        int(r.get("population") or 0),
                        int(r.get("estimated_loot") or 0),
                        float(r.get("last_loot_ratio") or 1.0),
                        int(r.get("losses_recorded") or 0),
                        float(r.get("distance") or 0.0),
                        str(r.get("nearest_village") or ""),
                        str(r.get("status") or "pending"),
                        1 if r.get("manual") else 0,
                        float(r.get("updated_ts") or 0.0), i,
                    ),
                )
            _archive(f)
            imported.append(f.name)
        except (OSError, ValueError):
            logger.debug("Import farm %s fallito", f.name, exc_info=True)

    # 3. Watchlist cropper -> cropper_candidates
    cand_file = STATE_DIR / "cropper_candidates.json"
    if cand_file.exists():
        try:
            raw = json.loads(cand_file.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                for acc_id, rows in raw.items():
                    if not isinstance(rows, list):
                        continue
                    conn.execute(
                        "DELETE FROM cropper_candidates WHERE account_id = ?",
                        (str(acc_id),),
                    )
                    for i, r in enumerate(rows):
                        if not isinstance(r, dict):
                            continue
                        conn.execute(
                            "INSERT INTO cropper_candidates (account_id, id,"
                            " x, y, type, bonus_crop_percent, distance_hours,"
                            " nearest_village, oases, status, manual,"
                            " updated_ts, ord)"
                            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                str(acc_id), str(r.get("id") or ""),
                                int(r.get("x") or 0), int(r.get("y") or 0),
                                str(r.get("type") or ""),
                                int(r.get("bonus_crop_percent") or 0),
                                float(r.get("distance_hours") or 0.0),
                                str(r.get("nearest_village") or ""),
                                json.dumps(r.get("oases") or [], ensure_ascii=False),
                                str(r.get("status") or "pending"),
                                1 if r.get("manual") else 0,
                                float(r.get("updated_ts") or 0.0), i,
                            ),
                        )
            _archive(cand_file)
            imported.append(cand_file.name)
        except (OSError, ValueError):
            logger.debug("Import cropper fallito", exc_info=True)

    # 4. Storico villaggi -> village_snapshots
    hist_dir = STATE_DIR / "history"
    if hist_dir.exists():
        for f in hist_dir.glob("history_*.jsonl"):
            safe_acc = f.stem.removeprefix("history_")
            try:
                for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    # Chiave = safe_id dal filename, stessa convenzione di
                    # orchestrator._append_history -> history_insert(safe_id)
                    conn.execute(
                        "INSERT INTO village_snapshots"
                        " (account_id, village_name, ts, data) VALUES (?,?,?,?)",
                        (
                            safe_acc,
                            str(entry.get("village_name") or ""),
                            float(entry.get("ts") or 0.0),
                            line,
                        ),
                    )
                _archive(f)
                imported.append(f.name)
            except OSError:
                logger.debug("Import history %s fallito", f.name, exc_info=True)

    conn.execute(
        "INSERT OR REPLACE INTO kv_state (key, data, updated_ts) VALUES (?,?,?)",
        (_MIGRATION_FLAG, json.dumps(imported), time.time()),
    )
    if imported:
        logger.info(
            "Migrazione DB: importati %d file JSON legacy -> %s",
            len(imported), DB_PATH.name,
        )


def init_db() -> None:
    """Crea schema e lancia la migrazione se manca (idempotente)."""
    try:
        with _db():
            pass
    except Exception:
        logger.debug("init_db fallito", exc_info=True)
