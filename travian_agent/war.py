"""Reclaim & Chiefing Engine — gestione villaggi sottratti e riconquista.

Responsabilita':

1. **STOLEN_VILLAGE** — le coordinate di un villaggio configurato che
   scompaiono dalla sidebar vengono registrate in
   ``state/war_targets_<acc>.json`` (coordinate, timestamp, ruolo
   originario, info nemico se leggibili dal tile).
2. **Chiefing Train** — pianificazione delle wave di riconquista: la
   wave 0 (hammer + catapulte) sfonda mura/guarnigione, le wave
   successive (nobile + scorta) atterrano a intervalli stretti dopo
   l'impatto. Per ogni wave si calcola il tempo di viaggio dalla
   distanza toroidale e dalla velocita' dell'unita' piu' lenta, e se ne
   ricava il ``depart_ts`` = ``impact_ts - travel_s``: i villaggi piu'
   vicini/veloci partono dopo, quelli lenti/lontani prima, cosi' gli
   impatti restano consecutivi.
3. **Dispatch** — l'orchestratore spedisce le wave il cui ``depart_ts``
   e' scaduto, nel contesto del villaggio di origine (form Punto di
   Raduno, tipo movimento Attacco).

La riconquista vera e propria viene confermata a ciclo successivo:
quando le coordinate tornano in sidebar il target passa a 'reclaimed'
e l'orchestratore ri-onboarda il villaggio (bootstrap post-riconquista).
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = BASE_DIR / "state"

STATUS_STOLEN = "stolen"
STATUS_RECLAIMING = "reclaiming"
STATUS_RECLAIMED = "reclaimed"

WAVE_PLANNED = "planned"
WAVE_SENT = "sent"
WAVE_FAILED = "failed"

# Mappa T4.6 toroidale: coordinate -400..400
MAP_SIZE = 801.0
# Spaziatura di impatto tra wave consecutive del treno (spec: 1-3s)
IMPACT_GAP_SECONDS = 3.0
# Margine di sicurezza tra 'ora' e la partenza della prima wave utile:
# evita depart_ts nel passato quando il treno viene pianificato "per ora".
DEPART_BUFFER_SECONDS = 45.0
# Quante wave al massimo vengono spedite per singola visita al villaggio
MAX_WAVES_PER_DISPATCH = 4

# Velocita' di marcia (campi/ora, server x1) per unita' — mirror dei
# valori `speed` di ai_scout_brain.TROOP_STATS (t1..t30). Tenuto locale
# per non importare il modulo AI (dipendenze pesanti).
UNIT_SPEED: dict[str, float] = {
    # Romani
    "t1": 6.0, "t2": 5.0, "t3": 7.0, "t4": 16.0, "t5": 14.0,
    "t6": 10.0, "t7": 4.0, "t8": 3.0, "t9": 4.0, "t10": 5.0,
    # Teutoni
    "t11": 7.0, "t12": 7.0, "t13": 6.0, "t14": 9.0, "t15": 10.0,
    "t16": 9.0, "t17": 4.0, "t18": 3.0, "t19": 4.0, "t20": 5.0,
    # Galli
    "t21": 7.0, "t22": 6.0, "t23": 17.0, "t24": 19.0, "t25": 16.0,
    "t26": 13.0, "t27": 4.0, "t28": 3.0, "t29": 5.0, "t30": 5.0,
}

# Unita' "capo" per tribu' (Senatore / Comandante / Capotribu')
CHIEF_UNITS = {"romans": "t9", "teutons": "t19", "gauls": "t29"}

# Riduzione lealta' per colpo di nobile, in punti percentuali (spec:
# 20-25% a colpo -> 4-5 colpi puliti da 100% a 0%). Usata solo per
# stimare quante wave 'chief' servono nel treno.
LOYALTY_DROP_PER_CHIEF = {"romans": 22.5, "teutons": 22.5, "gauls": 25.0}


def chief_waves_needed(tribe: str, loyalty: float = 100.0) -> int:
    """Numero di colpi nobile stimati per azzerare la lealta' residua."""
    drop = LOYALTY_DROP_PER_CHIEF.get(str(tribe).lower(), 22.5)
    if loyalty <= 0:
        return 0
    return max(1, math.ceil(loyalty / drop))


def _unit_key(raw: str) -> str:
    """Normalizza chiavi truppa 'u9'/'T9' -> 't9' (come fa il form JS)."""
    return str(raw).strip().lower().replace("u", "t")


def map_distance(x1: int, y1: int, x2: int, y2: int) -> float:
    """Distanza euclidea su mappa toroidale -400..400 (campi)."""
    dx = abs(int(x1) - int(x2)) % MAP_SIZE
    dx = min(dx, MAP_SIZE - dx)
    dy = abs(int(y1) - int(y2)) % MAP_SIZE
    dy = min(dy, MAP_SIZE - dy)
    return math.hypot(dx, dy)


def wave_speed(troops: dict[str, int]) -> float:
    """Velocita' della wave = unita' piu' lenta tra quelle presenti."""
    speeds = [
        UNIT_SPEED[_unit_key(k)]
        for k, qty in (troops or {}).items()
        if int(qty or 0) > 0 and _unit_key(k) in UNIT_SPEED
    ]
    # Fallback prudente: velocita' catapulta (l'unita' piu' lenta)
    return min(speeds) if speeds else 3.0


def travel_seconds(
    troops: dict[str, int], sx: int, sy: int, tx: int, ty: int
) -> float:
    """Tempo di viaggio origine->target in secondi (server x1)."""
    return map_distance(sx, sy, tx, ty) / wave_speed(troops) * 3600.0


def plan_chiefing_train(
    tx: int,
    ty: int,
    waves_spec: list[dict],
    impact_ts: float | None = None,
    gap_s: float = IMPACT_GAP_SECONDS,
    now: float | None = None,
) -> list[dict]:
    """Costruisce il treno di riconquista con partenze sincronizzate.

    ``waves_spec`` e' la lista ORDINATA delle wave: indice 0 = hammer
    (truppe + catapulte), indici 1..N = wave 'chief' (nobile + scorta).
    Ogni voce: {source_id, source_name, x, y, troops, kind}.

    Se ``impact_ts`` e' None, l'ancora di impatto e'
    ``now + max(travel) + DEPART_BUFFER_SECONDS``: tutti i depart_ts
    risultano nel futuro, compresa la wave piu' lenta.

    Ritorna i dict wave persistibili con travel_s / depart_ts /
    impact_ts / status calcolati.
    """
    now = now if now is not None else time.time()
    out: list[dict] = []
    for idx, spec in enumerate(waves_spec):
        troops = {str(k): int(v) for k, v in (spec.get("troops") or {}).items()}
        travel = travel_seconds(
            troops, int(spec.get("x", 0)), int(spec.get("y", 0)), tx, ty
        )
        out.append(
            {
                "wave_id": idx,
                "kind": str(spec.get("kind") or ("hammer" if idx == 0 else "chief")),
                "source_id": str(spec.get("source_id") or ""),
                "source_name": str(spec.get("source_name") or ""),
                "x": int(spec.get("x", 0)),
                "y": int(spec.get("y", 0)),
                "troops": troops,
                "move_type": str(spec.get("move_type") or "3"),  # attacco
                "travel_s": round(travel, 1),
                "depart_ts": 0.0,
                "impact_ts": 0.0,
                "status": WAVE_PLANNED,
            }
        )
    if not out:
        return []
    if impact_ts is None:
        impact_ts = now + max(w["travel_s"] for w in out) + DEPART_BUFFER_SECONDS
    for idx, w in enumerate(out):
        w["impact_ts"] = impact_ts + idx * gap_s
        w["depart_ts"] = w["impact_ts"] - w["travel_s"]
    return out


@dataclass
class WarStore:
    """Persistenza dei target di guerra su state/war_targets_<acc>.json.

    Ogni target: {x, y, name, village_id, status, lost_ts, enemy_info,
    old_role, reclaim_source, reclaim_planned, waves, updated_ts}.
    """

    account_id: str
    state_path: Path | None = None
    targets: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.state_path is None:
            safe = "".join(
                c if c.isalnum() or c in "._-" else "_" for c in self.account_id
            )
            self.state_path = STATE_DIR / f"war_targets_{safe}.json"
        self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.state_path and self.state_path.exists():
                data = json.loads(
                    self.state_path.read_text(encoding="utf-8-sig")
                )
                raw = data.get("targets") if isinstance(data, dict) else data
                if isinstance(raw, list):
                    self.targets = [
                        t for t in raw
                        if isinstance(t, dict)
                        and t.get("x") is not None
                        and t.get("y") is not None
                    ]
        except Exception:
            logger.debug("Lettura war targets fallita", exc_info=True)

    def _save(self) -> None:
        try:
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {"targets": self.targets}, indent=2, ensure_ascii=False
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.debug("Salvataggio war targets fallito", exc_info=True)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------
    def target_at(self, x: int, y: int) -> dict | None:
        for t in self.targets:
            if t.get("x") == x and t.get("y") == y:
                return t
        return None

    def list_targets(self, status: str | None = None) -> list[dict]:
        if status is None:
            return list(self.targets)
        return [t for t in self.targets if t.get("status") == status]

    def due_waves(
        self, source_id: str, now: float | None = None
    ) -> list[tuple[dict, dict]]:
        """Coppie (target, wave) pianificate e scadute per un villaggio.

        Filtra su wave 'planned' con depart_ts <= now la cui sorgente e'
        ``source_id`` (newdid del villaggio che l'orchestratore sta
        visitando). Ordinate per impact_ts: la wave hammer parte sempre
        prima delle chief.
        """
        now = now if now is not None else time.time()
        out: list[tuple[dict, dict]] = []
        for t in self.targets:
            if t.get("status") != STATUS_RECLAIMING:
                continue
            for w in t.get("waves") or []:
                if (
                    w.get("status") == WAVE_PLANNED
                    and str(w.get("source_id") or "") == str(source_id)
                    and float(w.get("depart_ts") or 0.0) <= now
                ):
                    out.append((t, w))
        out.sort(key=lambda tw: float(tw[1].get("impact_ts") or 0.0))
        return out

    # ------------------------------------------------------------------
    # Mutazioni
    # ------------------------------------------------------------------
    def add_stolen(
        self,
        x: int,
        y: int,
        name: str = "",
        village_id: str = "",
        old_role: str = "",
        enemy_info: str = "",
    ) -> dict | None:
        """Registra un STOLEN_VILLAGE; None se le coordinate sono note."""
        if self.target_at(x, y) is not None:
            return None
        t = {
            "x": int(x),
            "y": int(y),
            "name": str(name or ""),
            "village_id": str(village_id or ""),
            "status": STATUS_STOLEN,
            "lost_ts": time.time(),
            "enemy_info": str(enemy_info or ""),
            "old_role": str(old_role or ""),
            "reclaim_source": "",
            "reclaim_planned": False,
            "waves": [],
            "updated_ts": time.time(),
        }
        self.targets.append(t)
        self._save()
        return t

    def mark_status(self, x: int, y: int, status: str) -> bool:
        t = self.target_at(x, y)
        if t is None:
            return False
        t["status"] = status
        t["updated_ts"] = time.time()
        self._save()
        return True

    def plan_waves(
        self, x: int, y: int, waves: list[dict], source_id: str = ""
    ) -> bool:
        """Attiva il RECLAIM_VILLAGE: aggancia il treno pianificato al target."""
        t = self.target_at(x, y)
        if t is None or not waves:
            return False
        t["waves"] = waves
        t["reclaim_planned"] = True
        t["reclaim_source"] = str(source_id or "")
        t["status"] = STATUS_RECLAIMING
        t["updated_ts"] = time.time()
        self._save()
        return True
