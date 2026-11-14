"""Custom Farm Target Manager — alternativa alle farm list del Gold Club.

Flusso human-in-the-loop (analogo a ``cropper.py``):

    [ Scansione mappa (AI Scout / Farming) ] -> [ store: pending ]
        -> [ Dashboard: approve / reject ]
        -> [ farming.py invia raid mirati solo sui target `approved` ]
        -> [ report: perdite o difensori -> `blacklisted` ]

Persistenza: ``state/farm_targets_<account_id>.json`` (un file per
account, al contrario del file unico condiviso di cropper_candidates).

    [{"id": "f_minus12_45", "x": -12, "y": 45, "name": "Villaggio di X",
      "is_oasis": false, "population": 34, "estimated_loot": 2040,
      "last_loot_ratio": 1.0, "losses_recorded": 0,
      "distance": 3.6, "nearest_village": "Hipponion",
      "status": "pending", "manual": false, "updated_ts": 1731600000.0}]

Stati ammessi: pending (scoperto), approved (autorizzato), rejected
(scartato dall'utente), blacklisted (difeso: auto da report o manuale).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import BASE_DIR
from . import db

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Stati ammessi dal workflow human-in-the-loop
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_BLACKLISTED = "blacklisted"
VALID_STATUSES = (
    STATUS_PENDING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_BLACKLISTED,
)

# Stima bottino: popolazione * 60 risorse (come l'euristica AI Scout)
LOOT_PER_POP = 60
# Bottino minimo stimato per le oasi (senza popolazione rilevabile)
OASIS_BASE_LOOT = 120


def make_target_id(x: int, y: int) -> str:
    """Id stabile: 'f_minus12_45' per il tile a (-12|45)."""

    def enc(v: int) -> str:
        return ("minus" if v < 0 else "") + str(abs(v))

    return f"f_{enc(x)}_{enc(y)}"


@dataclass
class FarmTarget:
    """Un bersaglio di saccheggio custom (villaggio o oasi)."""

    id: str
    x: int
    y: int
    name: str = ""
    is_oasis: bool = False
    population: int = 0
    estimated_loot: int = 0
    last_loot_ratio: float = 1.0
    losses_recorded: int = 0
    distance: float = 0.0          # campi dal villaggio proprio piu' vicino
    nearest_village: str = ""      # villaggio mittente (auto o manuale)
    target_type: str = ""          # oasis | inactive_village | farm
    status: str = STATUS_PENDING
    manual: bool = False           # inserito a mano dalla dashboard
    last_raid_ts: float = 0.0      # ts ultimo invio raid verso il target
    last_outcome: str = ""         # clean | losses | empty (dai report)
    fail_reason: str = ""          # motivo ultimo mancato invio
    updated_ts: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: dict) -> "FarmTarget | None":
        try:
            x = int(data.get("x", 0))
            y = int(data.get("y", 0))
            return cls(
                id=str(data.get("id") or make_target_id(x, y)),
                x=x,
                y=y,
                name=str(data.get("name") or ""),
                is_oasis=bool(data.get("is_oasis")),
                population=int(data.get("population") or 0),
                estimated_loot=int(data.get("estimated_loot") or 0),
                last_loot_ratio=float(data.get("last_loot_ratio") or 1.0),
                losses_recorded=int(data.get("losses_recorded") or 0),
                distance=float(data.get("distance") or 0.0),
                nearest_village=str(data.get("nearest_village") or ""),
                target_type=str(data.get("target_type") or ""),
                status=data.get("status")
                if data.get("status") in VALID_STATUSES
                else STATUS_PENDING,
                manual=bool(data.get("manual")),
                last_raid_ts=float(data.get("last_raid_ts") or 0.0),
                last_outcome=str(data.get("last_outcome") or ""),
                fail_reason=str(data.get("fail_reason") or ""),
                updated_ts=float(data.get("updated_ts") or time.time()),
            )
        except Exception:
            return None


class FarmTargetStore:
    """Persistenza dei target farm su state/farm_targets_<acc>.json.

    Una istanza per account: il file e' condiviso tra agente e dashboard
    (lettura/scrittura non atomica ma best-effort, come gli altri store).
    """

    def __init__(self, account_id: str, path: Path | None = None) -> None:
        self.account_id = account_id
        # path esplicito -> file JSON (test/override); default -> tabella DB
        self._db_key: str | None = None
        if path is None:
            safe = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in account_id
            )
            self._db_key = safe
            path = STATE_DIR / f"farm_targets_{safe}.json"
        self.path = path
        self._data: list[dict] = []
        self._load()

    # --------------------------------------------------------------
    # Persistenza
    # --------------------------------------------------------------
    def _load(self) -> None:
        if self._db_key is not None:
            self._data = db.farm_load(self._db_key)
            return
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if isinstance(raw, list):
                    self._data = [r for r in raw if isinstance(r, dict)]
                elif isinstance(raw, dict):
                    # Tolleranza: formato {"targets": [...]}
                    self._data = [
                        r for r in raw.get("targets", [])
                        if isinstance(r, dict)
                    ]
        except Exception:
            logger.debug("Lettura farm_targets fallita", exc_info=True)
            self._data = {}

    def _save(self) -> None:
        if self._db_key is not None:
            db.farm_save(self._db_key, self._data)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Salvataggio farm_targets fallito", exc_info=True)

    # --------------------------------------------------------------
    # API di lettura
    # --------------------------------------------------------------
    def list_targets(self, status: str | None = None) -> list[FarmTarget]:
        """Target dell'account (filtro opzionale per stato), per distanza."""
        out: list[FarmTarget] = []
        for raw in self._data:
            t = FarmTarget.from_dict(raw)
            if t is not None and (status is None or t.status == status):
                out.append(t)
        out.sort(key=lambda t: (t.distance, t.x, t.y))
        return out

    def find(self, target_id: str) -> FarmTarget | None:
        for raw in self._data:
            if raw.get("id") == target_id:
                return FarmTarget.from_dict(raw)
        return None

    def status_at(self, x: int, y: int) -> str | None:
        """Stato del record associato alle coordinate, o None se assente."""
        for raw in self._data:
            if int(raw.get("x", 0)) == x and int(raw.get("y", 0)) == y:
                return str(raw.get("status") or STATUS_PENDING)
        return None

    def get_approved_targets(
        self, near_x: int | None = None, near_y: int | None = None
    ) -> list[FarmTarget]:
        """Solo i target approved: unici bersagli leciti per il farming.

        Se vengono passate coordinate, la lista e' riordinata per distanza
        reale dal punto dato (villaggio attivo), non solo da quella
        memorizzata al momento della scansione.
        """
        approved = self.list_targets(STATUS_APPROVED)
        if near_x is not None and near_y is not None:
            approved.sort(
                key=lambda t: math.hypot(t.x - near_x, t.y - near_y)
            )
        return approved

    def pending_count(self) -> int:
        return sum(
            1 for r in self._data
            if r.get("status") == STATUS_PENDING
        )

    # --------------------------------------------------------------
    # API di scrittura
    # --------------------------------------------------------------
    def upsert(self, target: FarmTarget) -> None:
        """Inserisce o aggiorna un target; non retrocede uno stato deciso."""
        for i, raw in enumerate(self._data):
            if raw.get("id") == target.id or (
                int(raw.get("x", 0)) == target.x
                and int(raw.get("y", 0)) == target.y
            ):
                old = str(raw.get("status") or STATUS_PENDING)
                keep = asdict(target)
                # Non sovrascrivere stati decisi (approved/rejected/blacklisted)
                if old != STATUS_PENDING:
                    keep["status"] = old
                    keep["manual"] = raw.get("manual", keep["manual"])
                keep["last_loot_ratio"] = raw.get(
                    "last_loot_ratio", keep["last_loot_ratio"]
                )
                keep["losses_recorded"] = raw.get(
                    "losses_recorded", keep["losses_recorded"]
                )
                keep["updated_ts"] = time.time()
                self._data[i] = keep
                self._save()
                return
        self._data.append(asdict(target))
        self._save()

    def set_status(self, target_id: str, status: str) -> bool:
        if status not in VALID_STATUSES:
            return False
        for raw in self._data:
            if raw.get("id") == target_id:
                raw["status"] = status
                raw["updated_ts"] = time.time()
                self._save()
                return True
        return False

    def set_status_at(self, x: int, y: int, status: str) -> bool:
        """Imposta lo stato del record alle coordinate (se esiste)."""
        if status not in VALID_STATUSES:
            return False
        for raw in self._data:
            if int(raw.get("x", 0)) == x and int(raw.get("y", 0)) == y:
                raw["status"] = status
                raw["updated_ts"] = time.time()
                self._save()
                return True
        return False

    def approve(self, target_id: str) -> bool:
        return self.set_status(target_id, STATUS_APPROVED)

    def reject(self, target_id: str) -> bool:
        return self.set_status(target_id, STATUS_REJECTED)

    def blacklist(self, target_id: str) -> bool:
        return self.set_status(target_id, STATUS_BLACKLISTED)

    def add_manual_target(
        self,
        x: int,
        y: int,
        name: str = "",
        is_oasis: bool = False,
        population: int = 0,
        status: str = STATUS_PENDING,
        nearest_village: str = "",
    ) -> FarmTarget | None:
        """Inserimento manuale di un target dalla dashboard.

        Ritorna None se le coordinate sono gia' catalogate (qualunque
        stato): niente duplicati. Il record entra in testa alla lista
        con flag manual; `status` default 'pending', la dashboard puo'
        passare 'approved' per attivare subito i raid mirati.
        """
        if self.status_at(x, y) is not None:
            return None
        if status not in VALID_STATUSES:
            status = STATUS_PENDING
        tgt = FarmTarget(
            id=make_target_id(x, y),
            x=x,
            y=y,
            name=str(name or f"Target ({x}|{y})"),
            is_oasis=bool(is_oasis),
            population=int(population or 0),
            estimated_loot=_estimate_loot(population, is_oasis),
            nearest_village=str(nearest_village or ""),
            target_type="oasis" if is_oasis else "farm",
            status=status,
            manual=True,
        )
        self._data.insert(0, asdict(tgt))
        self._save()
        return tgt

    def record_raid_attempt(
        self, x: int, y: int, sent: bool, reason: str = ""
    ) -> None:
        """Registra l'esito del tentativo di invio verso (x|y).

        last_raid_ts si aggiorna solo su invio riuscito; fail_reason
        conserva il motivo del mancato invio per la dashboard.
        """
        changed = False
        for raw in self._data:
            if int(raw.get("x", 0)) != x or int(raw.get("y", 0)) != y:
                continue
            raw["fail_reason"] = "" if sent else str(reason or "")
            if sent:
                raw["last_raid_ts"] = time.time()
            raw["updated_ts"] = time.time()
            changed = True
            break
        if changed:
            self._save()

    # --------------------------------------------------------------
    # Discovery (Step 2 spec): tile scansionati -> pending
    # --------------------------------------------------------------
    def register_scanned(
        self,
        scanned_tiles: list[dict],
        origin_x: int,
        origin_y: int,
        nearest_village: str = "",
        skip_names: set[str] | None = None,
    ) -> int:
        """Registra i tile razziabili trovati dalla scansione mappa.

        Sono candidati le oasi e i villaggi occupati (population > 0);
        i tile liberi non sono razziabili e vengono ignorati. Tile gia'
        noti (qualunque stato) aggiornano solo popolazione/nome.
        Ritorna il numero di NUOVI target inseriti come pending.
        """
        skip = {n.strip().lower() for n in (skip_names or set()) if n}
        new_count = 0
        for tile in scanned_tiles:
            try:
                x, y = int(tile["x"]), int(tile["y"])
            except (KeyError, TypeError, ValueError):
                continue
            is_oasis = bool(tile.get("is_oasis"))
            name = str(
                tile.get("village_name") or tile.get("name") or ""
            )
            population = int(tile.get("population") or 0)
            if not is_oasis and population <= 0:
                continue  # tile libero o non razziabile
            if name and name.strip().lower() in skip:
                continue  # villaggio proprio

            dist = math.hypot(x - origin_x, y - origin_y)
            existing_id = None
            for raw in self._data:
                if int(raw.get("x", 0)) == x and int(raw.get("y", 0)) == y:
                    existing_id = raw.get("id")
                    break

            tgt = FarmTarget(
                id=existing_id or make_target_id(x, y),
                x=x,
                y=y,
                name=name or ("Oasi" if is_oasis else f"Tile ({x}|{y})"),
                is_oasis=is_oasis,
                population=population,
                estimated_loot=_estimate_loot(population, is_oasis),
                distance=round(dist, 1),
                nearest_village=nearest_village,
                target_type="oasis" if is_oasis else "farm",
                status=STATUS_PENDING,
            )
            before = len(self._data)
            self.upsert(tgt)
            if existing_id is None and len(self._data) == before + 1:
                new_count += 1
        if new_count:
            logger.info(
                "FarmTargetStore[%s]: %d nuovi target pending dalla "
                "scansione mappa.",
                self.account_id, new_count,
            )
        return new_count

    # --------------------------------------------------------------
    # Esiti raid (Step 3 spec): report -> loot ratio / auto-blacklist
    # --------------------------------------------------------------
    def process_combat_reports(self, reports: list[dict]) -> int:
        """Aggiorna gli esiti dei raid dai report di combattimento.

        - Report su target noto: aggiorna last_loot_ratio e perdite.
        - Perdite proprie o difensori nemici -> status 'blacklisted'
          (anti-suicidio; sblocco solo manuale da dashboard).
        - Esito pulito su target blacklisted NON riabilita: lo sblocco
          e' una decisione umana (pulsante 'Ripristina').
        Ritorna il numero di target aggiornati.
        """
        now = time.time()
        updated = 0
        for r in reports:
            x, y = r.get("x"), r.get("y")
            if x is None or y is None:
                continue
            for raw in self._data:
                if int(raw.get("x", 0)) != int(x) or int(raw.get("y", 0)) != int(y):
                    continue
                losses = int(r.get("losses", 0) or 0)
                defenders = int(r.get("defenders", 0) or 0)
                loot = float(r.get("loot_ratio", 1.0))
                raw["last_loot_ratio"] = loot
                raw["losses_recorded"] = int(
                    raw.get("losses_recorded", 0) or 0
                ) + losses
                raw["last_raid_ts"] = now
                raw["last_outcome"] = (
                    "losses" if (losses > 0 or defenders > 0)
                    else "clean" if loot > 0
                    else "empty"
                )
                raw["updated_ts"] = now
                updated += 1
                if losses > 0 or defenders > 0:
                    if raw.get("status") != STATUS_BLACKLISTED:
                        raw["status"] = STATUS_BLACKLISTED
                        logger.warning(
                            "FarmTarget (%s|%s) BLACKLISTED: perdite=%d, "
                            "difensori=%d.",
                            x, y, losses, defenders,
                        )
                break  # una sola riga per coordinate
        if updated:
            self._save()
        return updated


def _estimate_loot(population: int, is_oasis: bool) -> int:
    """Stima euristica del bottino potenziale del tile."""
    if is_oasis:
        return max(OASIS_BASE_LOOT, int(population or 0) * LOOT_PER_POP)
    return int(population or 0) * LOOT_PER_POP


# Cache degli store per account (dashboard + agente condividono l'istanza)
_farm_stores: dict[str, FarmTargetStore] = {}


def farm_target_store(account_id: str) -> FarmTargetStore:
    """FarmTargetStore condiviso per account (dashboard + agente)."""
    store = _farm_stores.get(account_id)
    if store is None:
        store = FarmTargetStore(account_id)
        _farm_stores[account_id] = store
    return store
