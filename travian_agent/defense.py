"""Modulo Difesa: reazione intelligente agli attacchi in arrivo.

Tre fasi sequenziali per villaggio, guidate dai movimenti della Piazza
d'Armi (Punto di Raduno, gid 16):

1. Threat Assessment: classifica i movimenti in arrivo in fake/basso
   rischio (scout, pochi uomini, rinforzi) vs minaccia reale. Solo le
   minacce reali con timer sotto `panic_threshold_seconds` attivano il
   protocollo d'emergenza.

2. Resource Drain: converte il surplus liquido in code di spesa
   (addestramento massivo + upgrade piu' costoso), azzerando il bottino
   depredabile all'impatto. Opzionalmente spedisce il residuo via mercato.

3. Troop Dodging: evacua le truppe di valore in rinforzo verso una
   destinazione sicura (villaggio proprio piu' vicino o target
   configurato), schedulando il rientro automatico dopo l'impatto.

Persistenza: le fughe e i rientri pendenti vivono in
state/defense_<account>.json e sopravvivono ai riavvii del bot.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from . import db, selectors
from .config import BASE_DIR, Settings
from .models import (
    Action,
    ActionType,
    GID_BARRACKS,
    GID_STABLE,
    GID_WORKSHOP,
    IncomingAttack,
)

if TYPE_CHECKING:
    from .actions import ActionExecutor
    from .browser import TravianBrowser
    from .models import Field, GameState, Village
    from .sensors import GameObserver

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Edifici addestramento usati dal resource drain
_TRAINER_GIDS = {GID_BARRACKS, GID_STABLE, GID_WORKSHOP}

# Margine di sicurezza: mai spedire risorse/difese sotto questo stock
_DRAIN_KEEP = 200


@dataclass
class PendingDodge:
    """Una fuga programmata: esegui il rinforzo quando now >= dodge_at_ts."""

    home_village_id: int
    home_name: str
    attack_arrival_ts: float
    dodge_at_ts: float
    troops: dict[str, int] = field(default_factory=dict)
    dest_village_id: int = 0   # villaggio ospite
    dest_x: int | None = None
    dest_y: int | None = None
    sent: bool = False


@dataclass
class PendingRecall:
    """Rientro programmato: rispedisci a casa le truppe evacuate."""

    host_village_id: int       # villaggio che ospita le truppe
    home_village_id: int
    home_name: str
    home_x: int | None
    home_y: int | None
    recall_at_ts: float
    troops: dict[str, int] = field(default_factory=dict)


@dataclass
class DefenseManager:
    """Gestore della reazione anti-attacco per un account."""

    account_id: str
    state_path: Path | None = None
    dodges: dict[str, PendingDodge] = field(default_factory=dict)   # key: village_id
    recalls: list[PendingRecall] = field(default_factory=list)
    # Lock temporaneo (spec 4.1): village_id -> ts di fine emergenza.
    # Mentre attivo, replenishment.py viene congelato dall'orchestrator per
    # evitare conflitti sulla spesa delle risorse appena svuotate.
    emergency_until: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._kv_key: str | None = None
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self._kv_key = f"defense_{safe}.json"
            self.state_path = STATE_DIR / self._kv_key
        self._load()

    # ------------------------------------------------------------------
    # Persistenza
    # ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            data: dict | None = None
            if self._kv_key is not None:
                text = db.kv_read(self._kv_key)
                data = json.loads(text) if text else None
            elif self.state_path and self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if data:
                self.dodges = {
                    str(k): PendingDodge(**v)
                    for k, v in (data.get("dodges") or {}).items()
                    if isinstance(v, dict)
                }
                self.recalls = [
                    PendingRecall(**v)
                    for v in (data.get("recalls") or [])
                    if isinstance(v, dict)
                ]
                self.emergency_until = {
                    str(k): float(v)
                    for k, v in (data.get("emergency_until") or {}).items()
                }
        except Exception:
            self.dodges = {}
            self.recalls = []
            self.emergency_until = {}

    def _save(self) -> None:
        try:
            payload = json.dumps(
                {
                    "dodges": {k: vars(v) for k, v in self.dodges.items()},
                    "recalls": [vars(r) for r in self.recalls],
                    "emergency_until": self.emergency_until,
                },
                indent=2,
            )
            if self._kv_key is not None:
                db.kv_write(self._kv_key, payload)
                return
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(payload, encoding="utf-8")
        except Exception:
            logger.debug("Salvataggio stato difesa fallito", exc_info=True)

    # ------------------------------------------------------------------
    # Lock temporaneo: congela il replenishment durante l'emergenza
    # ------------------------------------------------------------------
    def is_emergency_locked(
        self, village_id: int | str, now: float | None = None
    ) -> bool:
        """True se il villaggio e' in lock d'emergenza difesa.

        Il lock copre la finestra tra l'allarme sotto soglia di panico e
        l'impatto + recall_delay: in quel periodo il modulo di rimpiazzo
        non deve spendere le risorse appena svuotate dal drain.
        """
        now = time.time() if now is None else now
        until = self.emergency_until.get(str(village_id), 0.0)
        return now < until

    def _lock_village(self, village_id: int | str, until_ts: float) -> None:
        self.emergency_until[str(village_id)] = until_ts
        self._save()

    # ------------------------------------------------------------------
    # Entry point: chiamato dall'orchestrator per ogni villaggio del ciclo
    # ------------------------------------------------------------------
    async def handle_village(
        self,
        *,
        browser: "TravianBrowser",
        observer: "GameObserver",
        executor: "ActionExecutor",
        settings: Settings,
        village: "Village",
        state: "GameState",
        villages: list["Village"],
        rally_slot: int,
        market_slot: int,
        prefix: str,
        now: float | None = None,
    ) -> None:
        cfg = settings.defense
        if not cfg.enabled:
            return
        vcfg = settings.village_config_for(village.name, village.village_id)
        if vcfg is not None and not vcfg.defense_enabled:
            return
        now = now if now is not None else time.time()

        # A) Rientri pendenti: se questo villaggio ospita truppe evacuate
        #    e il timer e' scaduto, le rispedisce a casa.
        await self._process_recalls(
            executor=executor,
            settings=settings,
            village=village,
            rally_slot=rally_slot,
            prefix=prefix,
            now=now,
        )

        # B) Fuga programmata scaduta (registrata in un ciclo precedente)
        pending = self.dodges.get(str(village.village_id))
        if pending and not pending.sent and now >= pending.dodge_at_ts:
            await self._execute_dodge(
                executor=executor,
                settings=settings,
                village=village,
                pending=pending,
                rally_slot=rally_slot,
                prefix=prefix,
            )

        # C) Threat assessment sui movimenti in arrivo
        if not rally_slot:
            return
        attacks = await observer.get_incoming_attacks(
            rally_slot, village.village_id
        )
        threats = [
            a for a in attacks
            if a.is_threat and not a.is_likely_fake(cfg.fake_troop_threshold)
        ]
        if not threats:
            return

        nearest = min(threats, key=lambda a: a.arrival_ts)
        secs = max(0, int(nearest.arrival_ts - now))
        logger.warning(
            "%s ATTACCO in arrivo da '%s': impatto tra %ds (%s)",
            prefix, nearest.attacker or "ignoto", secs, nearest.label,
        )

        # D) Emergenza: sotto la soglia di panico -> drain + dodge
        if secs > cfg.panic_threshold_seconds:
            logger.info(
                "%s Attacco a %ds (soglia %ds): monitorato, nessuna azione.",
                prefix, secs, cfg.panic_threshold_seconds,
            )
            # Registra la fuga programmata se ancora assente
            if not pending:
                await self._schedule_dodge(
                    observer=observer,
                    settings=settings,
                    village=village,
                    villages=villages,
                    attack=nearest,
                    rally_slot=rally_slot,
                    prefix=prefix,
                    now=now,
                )
            return

        logger.warning(
            "%s ATTACCO REALE IMMINENTE (%ds): avvio contromisure.",
            prefix, secs,
        )

        # Lock temporaneo: replenishment congelato fino a impatto + recall
        self._lock_village(
            village.village_id,
            nearest.arrival_ts + cfg.recall_delay_seconds,
        )

        # Fase 2: resource drain
        await self._drain_resources(
            browser=browser,
            observer=observer,
            executor=executor,
            settings=settings,
            village=village,
            state=state,
            market_slot=market_slot,
            rally_slot=rally_slot,
            prefix=prefix,
        )

        # Fase 3: troop dodging (se non gia' inviata)
        pending = self.dodges.get(str(village.village_id))
        if not pending:
            pending = await self._schedule_dodge(
                observer=observer,
                settings=settings,
                village=village,
                villages=villages,
                attack=nearest,
                rally_slot=rally_slot,
                prefix=prefix,
                now=now,
            )
        if pending and not pending.sent:
            await self._execute_dodge(
                executor=executor,
                settings=settings,
                village=village,
                pending=pending,
                rally_slot=rally_slot,
                prefix=prefix,
            )

    # ------------------------------------------------------------------
    # Fase 2: Resource Drain
    # ------------------------------------------------------------------
    async def _drain_resources(
        self,
        *,
        browser: "TravianBrowser",
        observer: "GameObserver",
        executor: "ActionExecutor",
        settings: Settings,
        village: "Village",
        state: "GameState",
        market_slot: int,
        rally_slot: int,
        prefix: str,
    ) -> None:
        res = state.resources
        total = res.wood + res.clay + res.iron + res.crop
        if total <= _DRAIN_KEEP * 4:
            logger.info("%s Drain: stock basso (%d), niente da svuotare.", prefix, total)
            return
        logger.warning("%s Resource Drain: stock %d da convertire in spese.", prefix, total)

        # 1) Addestramento massivo nei trainer del villaggio
        trainer_slots = [
            f.slot_id for f in state.interior_fields() if f.gid in _TRAINER_GIDS
        ]
        for slot in trainer_slots:
            if await self._emergency_train(
                executor, observer, settings, village, slot, prefix
            ):
                break

        # 2) Upgrade piu' costoso accodabile
        await self._emergency_upgrade(browser, village, prefix)

        # 3) Surplus residuo via mercato (opzionale, se configurato)
        cfg = settings.defense
        if cfg.drain_via_market and market_slot and cfg.drain_target_village:
            await self._drain_via_market(
                executor=executor,
                observer=observer,
                settings=settings,
                village=village,
                market_slot=market_slot,
                target_name=cfg.drain_target_village,
                prefix=prefix,
            )

    async def _emergency_train(
        self,
        executor: "ActionExecutor",
        observer: "GameObserver",
        settings: Settings,
        village: "Village",
        trainer_slot: int,
        prefix: str,
    ) -> bool:
        """Compila l'input del lotto massimo sostenibile e avvia il training."""
        page = executor.browser.page
        if page is None:
            return False
        try:
            url = f"{selectors.PATH_BUILD}?id={trainer_slot}"
            if village.village_id:
                url += f"&newdid={village.village_id}"
            await executor.browser.goto(url)

            data = await executor._extract_training_data(page)
            if not data:
                return False
            chosen = executor._pick_training_batch(
                data, priority_units=None, res_margin=0.98, crop_margin=0.9
            )
            if chosen is None:
                return False
            input_name, quantity, unit_label = chosen
            await page.locator(f"input[name='{input_name}']").first.fill(
                str(quantity), timeout=2000
            )
            if await executor._click_first(page, selectors.TRAIN_SUBMIT, timeout_ms=2000):
                logger.warning(
                    "%s Drain: addestramento %d x '%s' avviato (slot %s).",
                    prefix, quantity, unit_label or input_name, trainer_slot,
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Drain training slot %s fallito: %s", prefix, trainer_slot, exc)
        return False

    async def _emergency_upgrade(
        self, browser: "TravianBrowser", village: "Village", prefix: str
    ) -> bool:
        """Accoda l'upgrade piu' costoso disponibile nel dorf2."""
        page = browser.page
        if page is None:
            return False
        try:
            url = selectors.PATH_DORF2
            if village.village_id:
                url += f"?newdid={village.village_id}"
            await browser.goto(url)
            clicked = await page.evaluate(
                """
                () => {
                    const btnSel = 'button.upgrade, button.green, .upgradeButtonsContainer button, ' +
                                   'button[value*="migliora" i], button:has-text("Migliora"), ' +
                                   'button:has-text("Upgrade")';
                    let best = null, bestCost = 0;
                    document.querySelectorAll(btnSel).forEach(btn => {
                        if (btn.disabled) return;
                        const box = btn.closest(
                            '.buildingWrapper, .upgradeWrapper, .build_desc, ' +
                            '.contractLink, .contents, .item, tr, div'
                        ) || btn.parentElement;
                        const nums = ((box.innerText || '').match(/\\d+/g) || [])
                            .map(n => parseInt(n, 10))
                            .filter(n => n > 0 && n < 10000000);
                        const cost = nums.reduce((a, b) => a + b, 0);
                        if (cost > bestCost) { bestCost = cost; best = btn; }
                    });
                    if (best && bestCost > 0) { best.click(); return bestCost; }
                    return 0;
                }
                """
            )
            if clicked:
                logger.warning(
                    "%s Drain: upgrade piu' costoso accodato (~%s risorse).",
                    prefix, clicked,
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Drain upgrade fallito: %s", prefix, exc)
        return False

    async def _drain_via_market(
        self,
        *,
        executor: "ActionExecutor",
        observer: "GameObserver",
        settings: Settings,
        village: "Village",
        market_slot: int,
        target_name: str,
        prefix: str,
    ) -> None:
        """Spedisce il surplus residuo al villaggio sicuro via mercato."""
        try:
            res = (await observer.read_state()).resources
            surplus = {
                "wood": max(0, res.wood - _DRAIN_KEEP),
                "clay": max(0, res.clay - _DRAIN_KEEP),
                "iron": max(0, res.iron - _DRAIN_KEEP),
                "crop": max(0, res.crop - _DRAIN_KEEP),
            }
            if sum(surplus.values()) <= 0:
                return
            t_cfg = settings.village_config_for(target_name)
            params: dict = {
                "market_slot": market_slot,
                "target_village": target_name,
                "resources": surplus,
            }
            if t_cfg and t_cfg.x is not None and t_cfg.y is not None:
                params["target_x"], params["target_y"] = t_cfg.x, t_cfg.y
            result = await executor.execute(
                Action(
                    type=ActionType.SEND_RESOURCES,
                    params=params,
                    reason=f"Difesa: drain surplus verso '{target_name}'",
                ),
                village_id=village.village_id,
            )
            logger.warning(
                "%s Drain mercato -> '%s': %s", prefix, target_name, result.message
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Drain mercato fallito: %s", prefix, exc)

    # ------------------------------------------------------------------
    # Fase 3: Troop Dodging + Smart Recall
    # ------------------------------------------------------------------
    def _resolve_dodge_destination(
        self,
        settings: Settings,
        village: "Village",
        villages: list["Village"],
    ) -> tuple[int | None, int | None, int]:
        """Destinazione fuga: config dodge_target ('x|y' o nome) oppure il
        villaggio proprio piu' vicino. Ritorna (x, y, host_village_id)."""
        tgt = (settings.defense.dodge_target or "").strip()
        if tgt:
            # formato "x|y"
            if "|" in tgt:
                try:
                    sx, sy = tgt.split("|", 1)
                    return int(sx), int(sy), 0
                except ValueError:
                    pass
            # nome villaggio proprio
            for v in villages or []:
                if v.name == tgt and v.x is not None and v.y is not None:
                    return v.x, v.y, v.village_id
            t_cfg = settings.village_config_for(tgt)
            if t_cfg and t_cfg.x is not None and t_cfg.y is not None:
                return t_cfg.x, t_cfg.y, 0

        # Default: villaggio proprio piu' vicino con coordinate note
        if village.x is not None and village.y is not None:
            best = None
            best_d = None
            for v in villages or []:
                if v.village_id == village.village_id:
                    continue
                if v.x is None or v.y is None:
                    continue
                d = (v.x - village.x) ** 2 + (v.y - village.y) ** 2
                if best_d is None or d < best_d:
                    best_d = d
                    best = v
            if best is not None:
                return best.x, best.y, best.village_id
        return None, None, 0

    async def _schedule_dodge(
        self,
        *,
        observer: "GameObserver",
        settings: Settings,
        village: "Village",
        villages: list["Village"],
        attack: IncomingAttack,
        rally_slot: int,
        prefix: str,
        now: float,
    ) -> PendingDodge | None:
        """Registra la fuga programmata (truppe lette ora, esecuzione poi)."""
        cfg = settings.defense
        troops = await observer.read_troops_home(rally_slot, village.village_id)
        dodge_troops = {k: int(v) for k, v in troops.items() if int(v or 0) > 0}
        if sum(dodge_troops.values()) < cfg.min_dodge_troops:
            logger.info(
                "%s Dodge: solo %d truppe a casa (< %d): nessuna fuga.",
                prefix, sum(dodge_troops.values()), cfg.min_dodge_troops,
            )
            return None

        x, y, host_id = self._resolve_dodge_destination(settings, village, villages)
        if x is None or y is None:
            logger.warning(
                "%s Dodge: nessuna destinazione sicura con coordinate note.",
                prefix,
            )
            return None

        dodge_at = attack.arrival_ts - cfg.dodge_margin_seconds
        pending = PendingDodge(
            home_village_id=village.village_id,
            home_name=village.name,
            attack_arrival_ts=attack.arrival_ts,
            dodge_at_ts=dodge_at,
            troops=dodge_troops,
            dest_village_id=host_id,
            dest_x=x,
            dest_y=y,
        )
        self.dodges[str(village.village_id)] = pending
        self._save()
        logger.info(
            "%s Dodge programmato: %d truppe -> (%d|%d) tra %ds.",
            prefix, sum(dodge_troops.values()), x, y, max(0, int(dodge_at - now)),
        )
        return pending

    async def _execute_dodge(
        self,
        *,
        executor: "ActionExecutor",
        settings: Settings,
        village: "Village",
        pending: PendingDodge,
        rally_slot: int,
        prefix: str,
    ) -> None:
        """Invia le truppe in rinforzo alla destinazione sicura."""
        action = Action(
            type=ActionType.SEND_CUSTOM_RAID,
            params={
                "rally_slot": rally_slot,
                "move_type": "2",  # rinforzo
                "targets": [
                    {"x": pending.dest_x, "y": pending.dest_y, "troops": pending.troops}
                ],
            },
            reason=(
                f"Difesa: evacuazione {sum(pending.troops.values())} truppe "
                f"verso ({pending.dest_x}|{pending.dest_y})"
            ),
        )
        result = await executor.execute(action, village_id=village.village_id)
        if result.success:
            pending.sent = True
            cfg = settings.defense
            self.recalls.append(
                PendingRecall(
                    host_village_id=pending.dest_village_id,
                    home_village_id=pending.home_village_id,
                    home_name=pending.home_name,
                    home_x=village.x,
                    home_y=village.y,
                    recall_at_ts=pending.attack_arrival_ts + cfg.recall_delay_seconds,
                    troops=pending.troops,
                )
            )
            self._save()
            logger.warning(
                "%s Dodge eseguito: %s. Rientro programmato dopo l'impatto.",
                prefix, result.message,
            )
        else:
            logger.warning(
                "%s Dodge fallito: %s (riprovo al prossimo ciclo).",
                prefix, result.message,
            )

    async def _process_recalls(
        self,
        *,
        executor: "ActionExecutor",
        settings: Settings,
        village: "Village",
        rally_slot: int,
        prefix: str,
        now: float,
    ) -> None:
        """Se questo villaggio ospita truppe evacuate scadute, le rimanda a casa."""
        if not self.recalls or not rally_slot:
            return
        remaining: list[PendingRecall] = []
        for rec in self.recalls:
            # Il rientro parte dal villaggio che ospita le truppe
            if rec.host_village_id and rec.host_village_id != village.village_id:
                remaining.append(rec)
                continue
            if now < rec.recall_at_ts:
                remaining.append(rec)
                continue
            if rec.home_x is None or rec.home_y is None or not rec.troops:
                continue  # dati insufficienti: scarta

            action = Action(
                type=ActionType.SEND_CUSTOM_RAID,
                params={
                    "rally_slot": rally_slot,
                    "move_type": "2",  # rinforzo verso casa = rientro
                    "targets": [
                        {"x": rec.home_x, "y": rec.home_y, "troops": rec.troops}
                    ],
                },
                reason=(
                    f"Difesa: rientro {sum(rec.troops.values())} truppe "
                    f"verso '{rec.home_name}' ({rec.home_x}|{rec.home_y})"
                ),
            )
            result = await executor.execute(action, village_id=village.village_id)
            if result.success:
                logger.warning(
                    "%s Smart Recall eseguito: %s", prefix, result.message
                )
            else:
                # Riprova al prossimo passaggio su questo villaggio
                remaining.append(rec)
                logger.warning(
                    "%s Smart Recall fallito: %s", prefix, result.message
                )
        if len(remaining) != len(self.recalls):
            self.recalls = remaining
            self._save()
