"""Orchestratore per gestire account multipli in parallelo con asyncio ed error recovery."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from pathlib import Path
from typing import Literal

from . import selectors
from .actions import ActionExecutor
from .ai_scout_brain import AIScoutBrain, assess_account_power
from .brain import LLMBrain, RuleBasedBrain
from .browser import TravianBrowser
from .config import BASE_DIR, Settings, load_accounts
from .expansion import ExpansionManager, feeder_surplus, find_market
from .farming import FarmingManager, find_rally_point
from .hero import HeroManager
from .models import Action, ActionType, Field, GameState, Resources, Village
from .sensors import GameObserver

logger = logging.getLogger(__name__)

HISTORY_DIR = BASE_DIR / "state" / "history"
_last_video_bonus_claim: dict[str, float] = {}
# Manager dei moduli autonomi, un'istanza per account (persistenza su disco)
_farming_managers: dict[str, FarmingManager] = {}
_expansion_managers: dict[str, ExpansionManager] = {}
_scout_brains: dict[str, AIScoutBrain] = {}
_hero_managers: dict[str, HeroManager] = {}


def _append_history(account_id: str, state: GameState, action: Action) -> None:
    """Salva lo storico decisionale per-villaggio in JSONL (state/history/)."""
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        safe_id = "".join(c if c.isalnum() or c in "._-" else "_" for c in account_id)
        entry = {
            "ts": state.timestamp,
            "village_id": state.village_id,
            "village_name": state.village_name,
            "resources": {
                "wood": state.resources.wood,
                "clay": state.resources.clay,
                "iron": state.resources.iron,
                "crop": state.resources.crop,
                "warehouse_cap": state.resources.warehouse_capacity,
                "granary_cap": state.resources.granary_capacity,
            },
            "build_queue": len(state.build_queue),
            "incoming_attacks": state.incoming_attacks,
            "action": action.type.value,
            "reason": action.reason,
        }
        path: Path = HISTORY_DIR / f"history_{safe_id}.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("Scrittura storico fallita", exc_info=True)

INITIAL_BACKOFF = 10.0
MAX_BACKOFF = 300.0
BACKOFF_FACTOR = 2.0
JITTER_MIN = 0.9
JITTER_MAX = 1.1


def _calculate_backoff(attempt: int, base: float = INITIAL_BACKOFF, maximum: float = MAX_BACKOFF) -> float:
    """Calcola il ritardo di backoff esponenziale con jitter per evitare picchi simultanei."""
    delay = min(base * (BACKOFF_FACTOR ** max(0, attempt - 1)), maximum)
    return delay * random.uniform(JITTER_MIN, JITTER_MAX)


async def monitor_all_villages(
    browser: TravianBrowser,
    observer: GameObserver,
    brain: RuleBasedBrain | LLMBrain,
    executor: ActionExecutor,
    settings: Settings,
    prefix: str,
    last_states: dict[str, Resources] | None = None,
    farming_mgr: FarmingManager | None = None,
    expansion_mgr: ExpansionManager | None = None,
    scout_brain: AIScoutBrain | None = None,
    hero_mgr: HeroManager | None = None,
) -> None:
    """Ciclo di controllo multi-villaggio per l'account corrente.

    Flusso per ogni villaggio estratto dalla sidebar (parametro newdid):
    1. navigazione all'URL del villaggio
    2. lettura dello stato specifico (risorse, campi, coda, attacchi)
    3. decisione del brain e esecuzione dell'azione con contesto validato
    4. moduli autonomi: Farming (farm list), Espansione e AI Scout Brain (raid dinamico)
    5. pausa umana tra un villaggio e l'altro
    """
    # Assicuriamoci di essere sul dorf1 prima di leggere la sidebar (così i box dei villaggi sono presenti)
    if selectors.PATH_DORF1 not in (browser.page.url or ""):
        await browser.goto(selectors.PATH_DORF1)

    # 1. Estrae la lista dei villaggi dell'account
    villages = await observer.get_all_villages()

    if not villages:
        # Fallback di sicurezza: prova a rileggere o usa il villaggio corrente
        logger.warning("%s Lista villaggi vuota dalla sidebar. Tentativo di fallback sul villaggio attivo.", prefix)
        current_name = await observer.extract_village_name() or "corrente"
        villages = [Village(name=current_name)]
    else:
        logger.info(
            "%s Trovati %d villaggi nell'account: %s",
            prefix,
            len(villages),
            ", ".join(v.name for v in villages),
        )

    states_by_village: dict[str, GameState] = {}
    troops_by_village: dict[str, dict[str, int]] = {}

    # 1.1 Controllo e attivazione automatica del bonus +15% per 8 ore su tutte le risorse (video ad)
    if settings.auto_video_bonus:
        now = time.time()
        last_claim = _last_video_bonus_claim.get(settings.account_id, 0.0)
        has_notification = await observer.has_video_bonus_available()
        # Riscatta se sono trascorse ~7.8 ore (28000s) o se l'InfoBox segnala la disponibilità
        if (now - last_claim >= 28000.0) or has_notification:
            logger.info(
                "%s Verifica e riscatto automatico bonus produzione +15%% (8 ore) via video...",
                prefix,
            )
            claim_action = Action(
                type=ActionType.CLAIM_VIDEO_BONUS,
                reason="Attivazione automatica bonus +15% produzione risorse per 8 ore (video ad)",
            )
            claim_result = await executor.execute(claim_action)
            # Registra il timer solo se il bonus e' stato davvero accreditato
            # o era gia' attivo: un fallimento viene ritentato al ciclo dopo.
            claimed = claim_result.success and (
                "attivati" in claim_result.message
                or "già attivi" in claim_result.message
            )
            if claimed:
                _last_video_bonus_claim[settings.account_id] = now
                logger.info("%s Bonus video +15%% registrato per le prossime 8 ore", prefix)
            elif claim_result.success:
                logger.debug(
                    "%s Bonus video: %s (nessuna attivazione confermata)",
                    prefix,
                    claim_result.message,
                )
            else:
                logger.warning(
                    "%s Riscatto bonus video fallito: %s",
                    prefix,
                    claim_result.message,
                )

    # Edifici interni per villaggio: serve ai moduli farming/espansione
    interior_map: dict[str, list[Field]] = {}

    # 2. Itera su ciascun villaggio trovato
    for v_idx, village in enumerate(villages):
        village.role = settings.village_role_for(village.name, village.village_id)
        vname = village.name or f"villaggio {v_idx + 1}"
        vprefix = f"{prefix}[{vname}:{village.role}]"
        try:
            # Naviga al villaggio specifico (l'URL contiene già il newdid)
            if village.url:
                if village.url.startswith("http"):
                    await browser.page.goto(
                        village.url, wait_until="domcontentloaded"
                    )
                else:
                    await browser.goto(village.url)
            elif v_idx > 0:
                logger.warning("%s Villaggio '%s' privo di URL newdid, impossibile navigare.", prefix, vname)
                continue  # senza URL non si può cambiare villaggio

            # Assicura di essere sul dorf1 per leggere i campi e la coda correttamente
            if selectors.PATH_DORF1 not in (browser.page.url or ""):
                await browser.goto(selectors.PATH_DORF1)

            # 3. Stato specifico di QUESTO villaggio
            state = await observer.read_state()
            state.fields = await observer.read_fields()
            state.build_queue = await observer.read_build_queue()

            # Edifici interni del dorf2 (slot 19-40): fusi in state.fields
            interior = await observer.read_interior_fields()
            if interior:
                state.fields = [f for f in state.fields if f.is_resource_field] + interior
                interior_map[village.name or str(village.village_id)] = interior
                logger.debug(
                    "%s Edifici interni rilevati: %d", vprefix, len(interior)
                )

            if village.name:
                state.village_name = village.name
            if not state.village_id and village.village_id:
                state.village_id = village.village_id

            # Snapshot degli altri villaggi: serve al brain per scegliere
            # dove inviare le risorse in eccesso (emergenza storage pieno)
            if last_states is not None:
                state.peer_villages = {
                    name: res
                    for name, res in last_states.items()
                    if name != state.village_name
                }

            logger.info(
                "%s Risorse: L:%d A:%d F:%d G:%d | Coda:%d | Attacchi:%d",
                vprefix,
                state.resources.wood,
                state.resources.clay,
                state.resources.iron,
                state.resources.crop,
                len(state.build_queue),
                state.incoming_attacks,
            )

            # 4. Il brain decide e l'esecutore agisce (con validazione newdid)
            if last_states is not None and state.village_name:
                last_states[state.village_name] = state.resources
            states_by_village[vname] = state
            troops_by_village[vname] = state.troops_home

            action = brain.decide(state)
            target_vid = state.village_id or village.village_id
            result = await executor.execute(action, village_id=target_vid)
            _append_history(settings.account_id, state, action)
            if not result.success:
                logger.warning(
                    "%s Azione non eseguita: %s", vprefix, result.message
                )

            # 4.1 Modulo farming: raid periodico dalla farm list, se scaduto
            if farming_mgr is not None:
                rally = find_rally_point(state.interior_fields())
                try:
                    await farming_mgr.run_for_village(
                        browser=browser,
                        observer=observer,
                        executor=executor,
                        settings=settings,
                        village=village,
                        rally_slot=rally.slot_id if rally else 0,
                        prefix=vprefix,
                    )
                except Exception as f_exc:
                    logger.warning("%s Errore modulo farming: %s", vprefix, f_exc)

            # 4.1.b Modulo AI Scout Brain: raid dinamico a coordinate libere basato su radar mappa e report
            if scout_brain is not None and settings.scout_brain.enabled:
                rally = find_rally_point(state.interior_fields())
                if rally is not None:
                    try:
                        troops = await observer.read_troops_home(rally.slot_id, village.village_id)
                        if troops:
                            state.troops_home = troops
                            troops_by_village[vname] = troops

                        scanned = await observer.scan_map_area(
                            village.x or 0, village.y or 0, radius=settings.scout_brain.scan_radius
                        )
                        if scanned:
                            scout_brain.update_targets_from_map(scanned, village.x or 0, village.y or 0)

                        reports = await observer.read_combat_reports(limit=10)
                        if reports:
                            scout_brain.process_combat_reports(reports, blacklist_hours=settings.scout_brain.blacklist_hours)

                        plan = await scout_brain.decide_raid_plan(village, state.troops_home, settings)
                        if plan:
                            scout_act = Action(
                                type=ActionType.SEND_CUSTOM_RAID,
                                params={"rally_slot": rally.slot_id, "targets": plan},
                                reason=f"AI Scout Brain: invio {len(plan)} raid dinamici mirati",
                            )
                            scout_res = await executor.execute(scout_act, village_id=target_vid)
                            logger.info("%s AI Scout: %s", vprefix, scout_res.message)
                    except Exception as s_exc:
                        logger.warning("%s Errore modulo AI Scout Brain: %s", vprefix, s_exc)

            # 4.2 Modulo espansione: i villaggi non-fondatore con mercato
            # spediscono il surplus al fondatore mentre servono coloni
            if (
                expansion_mgr is not None
                and expansion_mgr.feed_target
                and village.name != expansion_mgr.feed_target
            ):
                market = find_market(state.interior_fields())
                vcfg_feed = settings.village_config_for(village.name, target_vid)
                market_slot = (
                    int(vcfg_feed.market_slot or 0)
                    if vcfg_feed is not None and vcfg_feed.market_slot
                    else (market.slot_id if market is not None else 0)
                )
                surplus = feeder_surplus(state)
                if market_slot and surplus:
                    founder_v = next(
                        (v for v in villages if v.name == expansion_mgr.feed_target),
                        None,
                    )
                    feed_params: dict = {
                        "market_slot": market_slot,
                        "target_village": expansion_mgr.feed_target,
                        "target": expansion_mgr.feed_target,
                        "resources": surplus,
                    }
                    if founder_v is not None and founder_v.x is not None and founder_v.y is not None:
                        feed_params["target_x"] = founder_v.x
                        feed_params["target_y"] = founder_v.y

                    feed_action = Action(
                        type=ActionType.SEND_RESOURCES,
                        params=feed_params,
                        reason=(
                            f"Espansione: feeder -> '{expansion_mgr.feed_target}' "
                            f"invia surplus {surplus} per i coloni"
                        ),
                    )
                    feed_res = await executor.execute(
                        feed_action, village_id=target_vid
                    )
                    logger.info(
                        "%s Espansione feeder: %s -> '%s' (%s)",
                        vprefix, surplus, expansion_mgr.feed_target,
                        feed_res.message,
                    )

            # 5. Pausa umana tra un villaggio e l'altro
            if v_idx < len(villages) - 1:
                await asyncio.sleep(random.uniform(2.5, 5.0))
        except Exception as v_exc:
            logger.warning(
                "%s Errore nel villaggio '%s': %s. Passo al successivo.",
                prefix,
                vname,
                v_exc,
            )
            continue

    # 3. Step espansione account-level: coloni/CP/target dopo il giro
    if expansion_mgr is not None:
        try:
            await expansion_mgr.step(
                browser=browser,
                observer=observer,
                executor=executor,
                settings=settings,
                villages=villages,
                interior_map=interior_map,
                prefix=prefix,
            )
        except Exception as e_exc:
            logger.warning("%s Errore modulo espansione: %s", prefix, e_exc)

    # 3.1 Modulo eroe: cura, avventure, attributi (account-level, con cooldown)
    if hero_mgr is not None and settings.hero.enabled:
        try:
            await hero_mgr.run(browser=browser, settings=settings, prefix=prefix)
        except Exception as h_exc:
            logger.warning("%s Errore modulo eroe: %s", prefix, h_exc)

    # 4. Global Power Assessment: valutazione complessiva impero multi-villaggio
    if scout_brain is not None:
        try:
            power = assess_account_power(settings, villages, states_by_village, troops_by_village)
            scout_brain.last_assessment = power
            scout_brain._save()
            logger.info(
                "%s Global Power Assessment: Potenza Militare=%d, Net Crop=%d/h, Saturazione=%.1f%%",
                prefix, power.military_power, power.net_crop_production, power.storage_saturation_pct,
            )
        except Exception as p_exc:
            logger.debug("%s Errore Global Power Assessment: %s", prefix, p_exc)

    logger.debug("%s Tutti i %d villaggi processati.", prefix, len(villages))


async def run_single_account_loop(
    settings: Settings,
    brain_type: Literal["rules", "llm"] = "rules",
    once: bool = False,
    dump: bool = False,
) -> None:
    """Loop di monitoraggio resiliente per un singolo account isolato."""
    prefix = f"[{settings.account_id}]"
    brain = RuleBasedBrain(settings) if brain_type == "rules" else LLMBrain(settings)

    logger.info("%s Avvio agente per %s", prefix, settings.server_url)

    consecutive_errors = 0
    browser = TravianBrowser(settings)
    observer: GameObserver | None = None
    executor: ActionExecutor | None = None
    # Snapshot risorse per villaggio: persiste tra i cicli, usato dal brain
    # per decidere dove inviare l'eccedenza in caso di storage pieno
    last_states: dict[str, Resources] = {}

    try:
        while True:
            try:
                # 0. Hot-reload della configurazione da accounts.json senza riavviare il bot
                try:
                    for fresh_acc in load_accounts():
                        if fresh_acc.account_id == settings.account_id:
                            settings.villages = fresh_acc.villages
                            settings.village_roles = fresh_acc.village_roles
                            settings.strategy_profile = fresh_acc.strategy_profile
                            settings.allow_parallel_build = fresh_acc.allow_parallel_build
                            settings.loop_interval_min = fresh_acc.loop_interval_min
                            settings.loop_interval_max = fresh_acc.loop_interval_max
                            settings.farming = fresh_acc.farming
                            settings.expansion = fresh_acc.expansion
                            settings.scout_brain = fresh_acc.scout_brain
                            settings.hero = fresh_acc.hero
                            break
                except Exception as hr_err:
                    logger.debug("%s Errore hot-reload config: %s", prefix, hr_err)

                # 0.1 Controllo interruttore di pausa d'emergenza (creato dalla dashboard GUI)
                pause_file = BASE_DIR / "state" / f"pause_{settings.account_id}.flag"
                if pause_file.exists():
                    logger.info("%s Agente in PAUSA (trovato %s). Attesa 10s...", prefix, pause_file.name)
                    await asyncio.sleep(10.0)
                    continue

                # Inizializza o ripristina il browser se non attivo
                if not browser.is_alive():
                    logger.info("%s Inizializzazione / ripristino contesto browser...", prefix)
                    await browser.recover()
                    assert browser.page is not None
                    observer = GameObserver(browser.page)
                    executor = ActionExecutor(browser, settings)

                assert browser.page is not None
                if observer is None:
                    observer = GameObserver(browser.page)
                else:
                    observer.page = browser.page

                if executor is None:
                    executor = ActionExecutor(browser, settings)
                else:
                    executor.browser = browser

                if dump:
                    await browser.ensure_logged_in()
                    await browser.goto(selectors.PATH_DORF1)
                    path = await browser.dump_debug("dorf1")
                    logger.info("%s Dump salvato in %s", prefix, path)
                    if once:
                        return

                await browser.ensure_logged_in()
                await browser.goto(selectors.PATH_DORF1)

                # Esegue il ciclo completo su tutti i villaggi dell'account
                if settings.account_id not in _farming_managers:
                    _farming_managers[settings.account_id] = FarmingManager(
                        settings.account_id
                    )
                if settings.account_id not in _expansion_managers:
                    _expansion_managers[settings.account_id] = ExpansionManager(
                        settings.account_id
                    )
                if settings.account_id not in _scout_brains:
                    _scout_brains[settings.account_id] = AIScoutBrain(
                        settings.account_id
                    )
                if settings.account_id not in _hero_managers:
                    _hero_managers[settings.account_id] = HeroManager(
                        settings.account_id
                    )
                await monitor_all_villages(
                    browser=browser,
                    observer=observer,
                    brain=brain,
                    executor=executor,
                    settings=settings,
                    prefix=prefix,
                    last_states=last_states,
                    farming_mgr=_farming_managers[settings.account_id],
                    expansion_mgr=_expansion_managers[settings.account_id],
                    scout_brain=_scout_brains[settings.account_id],
                    hero_mgr=_hero_managers[settings.account_id],
                )

                # Reset del conteggio errori e ripristino al completamento con successo
                if consecutive_errors > 0:
                    logger.info(
                        "%s Ciclo ripristinato con successo dopo %d errore/i.",
                        prefix,
                        consecutive_errors,
                    )
                    consecutive_errors = 0

                if once:
                    return

                interval = random.uniform(
                    settings.loop_interval_min, settings.loop_interval_max
                )
                logger.info("%s Prossimo ciclo tra %.0fs", prefix, interval)
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                logger.info("%s Ciclo interrotto (cancellazione ricevuta).", prefix)
                break
            except Exception as exc:
                consecutive_errors += 1
                backoff_wait = _calculate_backoff(consecutive_errors)
                logger.warning(
                    "%s [Errore #%d] %s: %s. Tentativo di ripristino e retry tra %.1fs (backoff esponenziale)...",
                    prefix,
                    consecutive_errors,
                    type(exc).__name__,
                    exc,
                    backoff_wait,
                )

                # Tentativo di recupero del contesto o ricaricamento pagina
                try:
                    if not browser.is_alive():
                        await browser.recover()
                        if browser.page is not None and observer is not None:
                            observer.page = browser.page
                    else:
                        await browser.reload()
                except Exception as rec_exc:
                    logger.debug(
                        "%s Errore durante il recupero immediato: %s",
                        prefix,
                        rec_exc,
                    )

                if once:
                    logger.error("%s Uscita in modalità --once dopo errore.", prefix)
                    return

                try:
                    await asyncio.sleep(backoff_wait)
                except asyncio.CancelledError:
                    logger.info("%s Ciclo interrotto durante l'attesa di backoff.", prefix)
                    break
    finally:
        try:
            await browser.stop()
        except Exception:
            logger.debug("%s Errore durante la chiusura del browser", prefix, exc_info=True)
        logger.info("%s Agente terminato.", prefix)


async def _supervised_account_loop(
    settings: Settings,
    brain_type: Literal["rules", "llm"],
    once: bool,
    dump: bool,
) -> None:
    """Supervisore per isolare ogni account in modo che un crash non intacchi gli altri."""
    prefix = f"[{settings.account_id}]"
    try:
        await run_single_account_loop(
            settings, brain_type=brain_type, once=once, dump=dump
        )
    except asyncio.CancelledError:
        logger.info("%s Task account interrotto.", prefix)
    except Exception:
        logger.exception(
            "%s Eccezione critica non gestita nell'account (isolata dagli altri).",
            prefix,
        )


async def run_all_accounts(
    accounts: list[Settings],
    brain_type: Literal["rules", "llm"] = "rules",
    once: bool = False,
    dump: bool = False,
) -> None:
    """Esegue tutti gli account contemporaneamente in modo concorrente tramite asyncio.gather isolato."""
    if not accounts:
        logger.warning("Nessun account configurato da eseguire.")
        return

    logger.info("Avvio parallelo per %d account...", len(accounts))
    tasks = [
        asyncio.create_task(
            _supervised_account_loop(
                acc, brain_type=brain_type, once=once, dump=dump
            ),
            name=f"task_{acc.account_id}",
        )
        for acc in accounts
    ]
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except asyncio.CancelledError:
        logger.info("Arresto parallelo richiesto, interruzione task in corso...")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("Tutti i task account sono terminati.")