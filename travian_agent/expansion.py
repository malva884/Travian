"""Modulo Espansione: fondazione autonoma di nuovi villaggi.

Flusso (eseguito una volta per ciclo account, dopo il giro dei villaggi):

1. Individua il villaggio fondatore: `expansion.founder_village` se
   configurato, altrimenti il primo villaggio con Residenza/Palazzo
   (gid 25/26) negli edifici interni rilevati durante il ciclo.
2. Legge i Punti Cultura sulla pagina della residenza
   (read_culture_points): se non bastano, esce.
3. Conta i coloni pronti (sensore truppe sulle icone u10/u20/u30).
   - < 3 coloni -> emette TRAIN_TROOPS sullo slot residenza con
     priority_units coloni/settler e ATTIVA la richiesta feeder:
     durante il ciclo successivo ogni villaggio non-fondatore con
     mercato invia il surplus al fondatore (SEND_RESOURCES).
   - >= 3 coloni -> scansiona i target_slots via read_map_tile e, al
     primo tile libero con >= crop_min campi grano, emette
     FOUND_VILLAGE (navigazione mappa + click 'Fonda').
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .config import BASE_DIR, ExpansionTarget, Settings
from .models import (
    Action,
    ActionType,
    Field,
    GameState,
    GID_MARKETPLACE,
    GID_PALACE,
    GID_RESIDENCE,
)

if TYPE_CHECKING:
    from .actions import ActionExecutor
    from .browser import TravianBrowser
    from .sensors import GameObserver
    from .models import Village

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Coloni richiesti per la fondazione
SETTLERS_REQUIRED = 3
# Icone unit dei coloni per tribu': u10 (romani), u20 (teutoni), u30 (galli)
SETTLER_ICONS = ("u10", "u20", "u30")
# Cap conservativo per singola spedizione mercato (mercante ~500-1000)
FEED_CAP_PER_RESOURCE = 2000
# Quota di riempimento che il feeder mantiene per se' (sopra -> spedisce)
FEEDER_KEEP_RATIO = 0.55
# Cooldown tra tentativi di fondazione falliti (evita spam di navigazione)
FOUND_RETRY_SECONDS = 900.0


def find_residence(interior: list[Field], min_level: int = 1) -> Field | None:
    """Residenza/Palazzo >= min_level negli edifici interni."""
    best: Field | None = None
    for f in interior or []:
        if f.gid in (GID_RESIDENCE, GID_PALACE) and f.level >= min_level:
            if best is None or f.level > best.level:
                best = f
    return best


def find_market(interior: list[Field]) -> Field | None:
    for f in interior or []:
        if f.gid == GID_MARKETPLACE and f.level >= 1:
            return f
    return None


def feeder_surplus(state: GameState, keep_ratio: float = FEEDER_KEEP_RATIO) -> dict[str, int]:
    """Surplus spedibile da un feeder: quantita' sopra keep_ratio di capienza.

    Ritorna {wood, clay, iron, crop} con solo voci positive, cappate a
    FEED_CAP_PER_RESOURCE per spedizione.
    """
    res = state.resources
    out: dict[str, int] = {}
    for name in ("wood", "clay", "iron"):
        cap = res.warehouse_capacity
        if cap <= 0:
            continue
        excess = int(getattr(res, name) - cap * keep_ratio)
        if excess > 0:
            out[name] = min(excess, FEED_CAP_PER_RESOURCE)
    cap = res.granary_capacity
    if cap > 0:
        excess = int(res.crop - cap * keep_ratio)
        if excess > 0:
            out["crop"] = min(excess, FEED_CAP_PER_RESOURCE)
    return out


def count_settlers(troops: dict[str, int]) -> int:
    """Conta i coloni a casa dalle icone unit u10/u20/u30."""
    return sum(int(troops.get(k, 0) or 0) for k in SETTLER_ICONS)


@dataclass
class ExpansionManager:
    """Planner di fondazione per un account; stato su state/expansion_<id>.json."""

    account_id: str
    state_path: Path | None = None
    founded_tiles: list[dict] = field(default_factory=list)
    feed_target: str = ""          # nome del villaggio che deve ricevere risorse
    next_found_attempt: float = 0.0  # cooldown anti-spam dopo fallimenti

    def __post_init__(self) -> None:
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self.state_path = STATE_DIR / f"expansion_{safe}.json"
        self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            if self.state_path and self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                self.founded_tiles = list(data.get("founded_tiles") or [])
                self.feed_target = str(data.get("feed_target") or "")
                self.next_found_attempt = float(data.get("next_found_attempt") or 0.0)
        except Exception:
            pass

    def _save(self) -> None:
        try:
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(
                    {
                        "founded_tiles": self.founded_tiles,
                        "feed_target": self.feed_target,
                        "next_found_attempt": self.next_found_attempt,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.debug("Salvataggio stato espansione fallito", exc_info=True)

    # ------------------------------------------------------------------
    # Selezione founder e target
    # ------------------------------------------------------------------
    def _pick_founder(
        self,
        settings: Settings,
        villages: list["Village"],
        interior_map: dict[str, list[Field]],
    ) -> tuple["Village | None", Field | None]:
        """Villaggio fondatore e suo edificio Residenza/Palazzo."""
        cfg_name = (settings.expansion.founder_village or "").strip()
        vkey = lambda v: v.name or str(v.village_id)  # noqa: E731
        if cfg_name:
            for v in villages:
                if v.name == cfg_name or str(v.village_id) == cfg_name:
                    res = find_residence(interior_map.get(vkey(v), []))
                    return v, res
            logger.warning(
                "Espansione: founder_village '%s' non trovato tra i villaggi.",
                cfg_name,
            )
            return None, None
        # Auto: primo villaggio con residenza/palazzo >= 10 (fallback >= 1)
        for min_level in (10, 1):
            for v in villages:
                res = find_residence(
                    interior_map.get(vkey(v), []), min_level=min_level
                )
                if res is not None:
                    return v, res
        return None, None

    async def _pick_target(
        self, observer: "GameObserver", settings: Settings, prefix: str
    ) -> ExpansionTarget | None:
        """Primo tile configurato libero con campi grano >= crop_min."""
        done = {(t.get("x"), t.get("y")) for t in self.founded_tiles}
        for target in settings.expansion.target_slots:
            if (target.x, target.y) in done:
                continue
            info = await observer.read_map_tile(target.x, target.y)
            if info.get("occupied"):
                logger.info(
                    "%s Espansione: tile (%d, %d) occupato, salto. %s",
                    prefix, target.x, target.y, target.note,
                )
                continue
            crop = int(info.get("crop_fields") or 0)
            if crop and crop < settings.expansion.crop_min:
                logger.info(
                    "%s Espansione: tile (%d, %d) solo %dc, richiesti >= %dc.",
                    prefix, target.x, target.y, crop, settings.expansion.crop_min,
                )
                continue
            if not (info.get("free") or info.get("settle_link") or crop):
                # Tile non riconosciuto come libero: salto per sicurezza
                logger.debug(
                    "%s Espansione: tile (%d, %d) non verificato come libero.",
                    prefix, target.x, target.y,
                )
                continue
            return target
        return None

    # ------------------------------------------------------------------
    # Step principale (una volta per ciclo account)
    # ------------------------------------------------------------------
    async def step(
        self,
        *,
        browser: "TravianBrowser",
        observer: "GameObserver",
        executor: "ActionExecutor",
        settings: Settings,
        villages: list["Village"],
        interior_map: dict[str, list[Field]],
        prefix: str,
        now: float | None = None,
    ) -> None:
        """Valuta lo stato espansione e agisce: coloni -> feed/raid -> fonda."""
        cfg = settings.expansion
        if not cfg.auto_expand:
            return
        if not cfg.target_slots:
            logger.debug("%s Espansione attiva ma nessun target_slots configurato.", prefix)
            return
        now = now if now is not None else time.time()
        if now < self.next_found_attempt:
            return

        founder, residence = self._pick_founder(settings, villages, interior_map)
        if founder is None or residence is None:
            logger.info(
                "%s Espansione: nessun villaggio con Residenza/Palazzo trovato.",
                prefix,
            )
            return

        # 1. Punti cultura
        cp_have, cp_need = await observer.read_culture_points(
            residence.slot_id, founder.village_id
        )
        if cp_need > 0 and cp_have < cp_need + cfg.min_culture_margin:
            logger.info(
                "%s Espansione: CP insufficienti a '%s' (%d/%d). Attendo.",
                prefix, founder.name, cp_have, cp_need,
            )
            self.feed_target = ""
            return

        # 2. Coloni disponibili?
        troops = await observer.read_troops_home(residence.slot_id, founder.village_id)
        settlers = count_settlers(troops)
        if settlers < SETTLERS_REQUIRED:
            # Addestra coloni + attiva rete feeder verso il fondatore
            self.feed_target = founder.name
            self._save()
            train = Action(
                type=ActionType.TRAIN_TROOPS,
                params={
                    "trainer_slot": residence.slot_id,
                    "trainer_gid": residence.gid,
                    "priority_units": [
                        "colono", "coloni", "settler", "settlers", "siedler",
                    ],
                },
                reason=(
                    f"Espansione: addestramento coloni in "
                    f"'{residence.name or 'Residenza'}' ({settlers}/{SETTLERS_REQUIRED})"
                ),
            )
            result = await executor.execute(train, village_id=founder.village_id)
            logger.info(
                "%s Espansione: coloni %d/%d a '%s' -> %s",
                prefix, settlers, SETTLERS_REQUIRED, founder.name, result.message,
            )
            return

        # 3. Scansione dei tile target
        target = await self._pick_target(observer, settings, prefix)
        if target is None:
            logger.info(
                "%s Espansione: nessun tile libero valido tra i target configurati.",
                prefix,
            )
            self.next_found_attempt = now + FOUND_RETRY_SECONDS
            self._save()
            return

        # 4. Fondazione
        action = Action(
            type=ActionType.FOUND_VILLAGE,
            params={"x": target.x, "y": target.y},
            reason=(
                f"Espansione: fondazione nuovo villaggio a "
                f"({target.x}|{target.y}) {target.note}"
            ),
        )
        result = await executor.execute(action, village_id=founder.village_id)
        if result.success:
            self.founded_tiles.append({"x": target.x, "y": target.y, "ts": now})
            self.feed_target = ""
            logger.info(
                "%s Espansione: villaggio fondato a (%d|%d)!",
                prefix, target.x, target.y,
            )
        else:
            self.next_found_attempt = now + FOUND_RETRY_SECONDS
            logger.warning(
                "%s Espansione: fondazione a (%d|%d) fallita: %s",
                prefix, target.x, target.y, result.message,
            )
        self._save()
