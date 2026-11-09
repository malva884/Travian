"""Esecutore: traduce un'Action in click/navigazioni sulla pagina."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from . import selectors
from .browser import TravianBrowser, sleep_human
from .config import Settings
from .models import Action, ActionResult, ActionType

logger = logging.getLogger(__name__)

_NEWDID = re.compile(r"newdid=(\d+)")

# Snippet JS condiviso: individua gli input quantita' del form di invio
# mercato. Le UI con slider (T4.5+) possono usare nomi diversi da r1-r4:
# si prendono i campi testo/numerici dentro il form che contiene il campo
# destinazione, escludendo nome villaggio, coordinate X/Y e ricerca.
_JS_MARKET_QTY_INPUTS = """
    const findQtyInputs = () => {
        const isQty = (i) => {
            const t = (i.type || 'text').toLowerCase();
            if (t !== 'text' && t !== 'number' && t !== '') return false;
            const key = ((i.name || '') + ' ' + (i.id || '')).toLowerCase().trim();
            if (/^(x|y)$/.test(key)) return false;
            if (/dname|village|target|player|search|name|message|note/.test(key)) return false;
            return true;
        };
        const scope =
            document.querySelector(
                'form:has(input[name*="dname" i]), form:has(input[name*="village" i]), ' +
                'form:has(input[id*="village" i]), form:has(input[name="x"]), ' +
                '#sendResources, .sendResources, #marketplaceSend, .marketplaceSend'
            ) || document;
        return Array.from(scope.querySelectorAll('input')).filter(isQty);
    };
"""


# Mappa di sinonimi / alias per le unita' delle varie tribu' Travian
UNIT_ALIASES: dict[str, set[str]] = {
    # Romani
    "t1": {"legionario", "legionari", "legionnaire", "legionnaires", "t1"},
    "t2": {"pretoriano", "pretoriani", "praetorian", "praetorians", "t2"},
    "t3": {"imperiano", "imperiani", "imperian", "imperians", "t3"},
    "t4": {"emissario", "emissari", "emissario a cavallo", "equites legati", "scout", "scouts", "t4"},
    "t5": {"equites imperatoris", "imperatoris", "ei", "cavaliere del generale", "cavalieri del generale", "cavalieri", "cavaliere", "cavalleria", "t5"},
    "t6": {"equites caesaris", "caesaris", "cesare", "ec", "cavaliere di cesare", "cavalieri di cesare", "cavalieri", "cavaliere", "cavalleria", "t6"},
    "t7": {"ariete", "arieti", "ram", "rams", "t7"},
    "t8": {"catapulta", "catapulte", "catapulta da fuoco", "catapult", "t8"},
    "t9": {"senatore", "senatori", "senator", "t9"},
    "t10": {"colono", "coloni", "settler", "settlers", "t10"},

    # Teutoni
    "t11": {"combattente", "combattenti", "mazziere", "mazzieri", "clubswinger", "t11"},
    "t12": {"alabardiere", "alabardieri", "spearman", "spearmen", "t12"},
    "t13": {"guerriero con ascia", "ascia", "asce", "asciere", "ascieri", "axeman", "t13"},
    "t14": {"esploratore", "esploratori", "scout", "scouts", "spia", "t14"},
    "t15": {"paladino", "paladini", "cavaliere", "cavalieri", "paladin", "t15"},
    "t16": {"cavaliere teutonico", "cavalieri teutonici", "cavalieri", "cavaliere", "teutonic knight", "t16"},
    "t17": {"ariete", "arieti", "ram", "rams", "t17"},
    "t18": {"catapulta", "catapulte", "catapult", "t18"},
    "t19": {"comandante", "comandanti", "chief", "t19"},
    "t20": {"colono", "coloni", "settler", "settlers", "t20"},

    # Galli
    "t21": {"falange", "falangi", "phalanx", "t21"},
    "t22": {"spadaccino", "spadaccini", "spade", "swordsman", "swordsmen", "t22"},
    "t23": {"esploratore", "esploratori", "scout", "scouts", "pathfinder", "t23"},
    "t24": {"fulmine di teutates", "fulmini", "tt", "teutates thunder", "cavaliere", "cavalieri", "cavalleria", "t24"},
    "t25": {"cavaliere druido", "druidi", "druidrider", "cavaliere", "cavalieri", "cavalleria", "t25"},
    "t26": {"haeduan", "eduano", "eduani", "cavaliere", "cavalieri", "cavalleria", "t26"},
    "t27": {"ariete", "arieti", "ram", "rams", "t27"},
    "t28": {"trabucco", "trabucchi", "trebuchet", "t28"},
    "t29": {"capotribu", "capotribù", "chieftain", "t29"},
    "t30": {"colono", "coloni", "settler", "settlers", "t30"},
}

# Costi statici ufficiali Travian per le unita' standard (legno, argilla, ferro, grano)
STATIC_TROOP_COSTS: dict[str, dict[str, int]] = {
    # Romani
    "t1": {"wood": 120, "clay": 100, "iron": 150, "crop": 30},   # Legionario
    "t2": {"wood": 100, "clay": 130, "iron": 160, "crop": 70},   # Pretoriano
    "t3": {"wood": 150, "clay": 160, "iron": 210, "crop": 80},   # Imperiano
    "t4": {"wood": 140, "clay": 160, "iron": 20, "crop": 40},    # Emissario a cavallo
    "t5": {"wood": 550, "clay": 440, "iron": 320, "crop": 100},  # Equites Imperatoris
    "t6": {"wood": 550, "clay": 640, "iron": 800, "crop": 180},  # Equites Caesaris
    "t7": {"wood": 900, "clay": 360, "iron": 500, "crop": 70},   # Ariete
    "t8": {"wood": 950, "clay": 1350, "iron": 600, "crop": 90},  # Catapulta da fuoco
    "t9": {"wood": 30750, "clay": 27200, "iron": 45000, "crop": 37500},
    "t10": {"wood": 4600, "clay": 4200, "iron": 5800, "crop": 4400},

    # Teutoni
    "t11": {"wood": 95, "clay": 75, "iron": 40, "crop": 40},     # Combattente
    "t12": {"wood": 145, "clay": 70, "iron": 85, "crop": 40},    # Alabardiere
    "t13": {"wood": 130, "clay": 120, "iron": 170, "crop": 70},  # Guerriero con ascia
    "t14": {"wood": 160, "clay": 100, "iron": 50, "crop": 50},   # Esploratore
    "t15": {"wood": 370, "clay": 270, "iron": 290, "crop": 90},  # Paladino
    "t16": {"wood": 450, "clay": 515, "iron": 480, "crop": 80},  # Cavaliere Teutonico
    "t17": {"wood": 1000, "clay": 300, "iron": 350, "crop": 70}, # Ariete
    "t18": {"wood": 900, "clay": 1200, "iron": 600, "crop": 60}, # Catapulta
    "t19": {"wood": 35500, "clay": 26600, "iron": 25000, "crop": 27200},
    "t20": {"wood": 5800, "clay": 4400, "iron": 4600, "crop": 5200},

    # Galli
    "t21": {"wood": 100, "clay": 130, "iron": 55, "crop": 30},   # Falange
    "t22": {"wood": 140, "clay": 150, "iron": 185, "crop": 60},  # Spadaccino
    "t23": {"wood": 170, "clay": 150, "iron": 20, "crop": 40},   # Esploratore
    "t24": {"wood": 350, "clay": 450, "iron": 230, "crop": 60},  # Fulmine di Teutates
    "t25": {"wood": 360, "clay": 330, "iron": 280, "crop": 120}, # Cavaliere Druido
    "t26": {"wood": 500, "clay": 620, "iron": 675, "crop": 170}, # Haeduan
    "t27": {"wood": 950, "clay": 555, "iron": 330, "crop": 75},  # Ariete
    "t28": {"wood": 960, "clay": 1450, "iron": 630, "crop": 90}, # Trabucco
    "t29": {"wood": 30750, "clay": 45000, "iron": 31000, "crop": 37500},
    "t30": {"wood": 4400, "clay": 5600, "iron": 4600, "crop": 4200},
}

STATIC_TROOP_LABELS: dict[str, str] = {
    "t1": "Legionario", "t2": "Pretoriano", "t3": "Imperiano",
    "t4": "Emissario a cavallo", "t5": "Equites Imperatoris", "t6": "Equites Caesaris",
    "t7": "Ariete", "t8": "Catapulta da fuoco", "t9": "Senatore", "t10": "Colono",
    "t11": "Combattente", "t12": "Alabardiere", "t13": "Guerriero con ascia",
    "t14": "Esploratore", "t15": "Paladino", "t16": "Cavaliere Teutonico",
    "t17": "Ariete", "t18": "Catapulta", "t19": "Comandante", "t20": "Colono",
    "t21": "Falange", "t22": "Spadaccino", "t23": "Esploratore",
    "t24": "Fulmine di Teutates", "t25": "Cavaliere Druido", "t26": "Haeduan",
    "t27": "Ariete", "t28": "Trabucco", "t29": "Capotribù", "t30": "Colono",
}


class ActionExecutor:
    def __init__(self, browser: TravianBrowser, settings: Settings) -> None:
        self.browser = browser
        self.settings = settings

    async def execute(self, action: Action, village_id: int | str = 0) -> ActionResult:
        logger.info("Azione %s -> %s", action.type.value, action.reason or "-")

        # Normalizzazione sicura di village_id a intero
        vid_int = 0
        if village_id:
            try:
                vid_int = int(village_id)
            except ValueError:
                logger.warning("village_id non valido: %s", village_id)

        # Verifica e forza il contesto villaggio se richiesto dall'azione
        if vid_int > 0 and action.type not in (
            ActionType.WAIT,
            ActionType.NOOP,
            ActionType.ALERT,
            ActionType.LOGIN,
            ActionType.CLAIM_VIDEO_BONUS,
        ):
            await self._ensure_village(vid_int)

        match action.type:
            case ActionType.UPGRADE_FIELD:
                return await self._upgrade_field(action, village_id=vid_int)
            case ActionType.SEND_RESOURCES:
                return await self._send_resources(action, village_id=vid_int)
            case ActionType.SEND_RAID:
                return await self._send_raid(action, village_id=vid_int)
            case ActionType.SEND_CUSTOM_RAID:
                return await self._send_custom_raid(action, village_id=vid_int)
            case ActionType.FOUND_VILLAGE:
                return await self._found_village(action, village_id=vid_int)
            case ActionType.TRAIN_TROOPS:
                return await self._train_troops(action, village_id=vid_int)
            case ActionType.CLAIM_VIDEO_BONUS:
                return await self._claim_video_bonus(action)
            case ActionType.ALERT:
                logger.warning("ALERT: %s", action.reason)
                return ActionResult(True, "alert notificato")
            case ActionType.LOGIN:
                await self.browser.ensure_logged_in()
                return ActionResult(True, "login verificato")
            case ActionType.WAIT | ActionType.NOOP:
                return ActionResult(True, action.reason)
            case _:
                return ActionResult(False, f"azione non gestita: {action.type}")

    async def _ensure_village(self, village_id: int) -> None:
        """Garantisce che il browser sia sul villaggio target (newdid)."""
        page = self.browser.page
        assert page is not None

        url = page.url or ""
        current = _NEWDID.search(url)
        # 1. Se l'URL riporta esplicitamente il newdid corretto, siamo già allineati
        if current is not None and int(current.group(1)) == village_id:
            return

        # 2. Se l'URL non ha newdid esplicito (es. dorf2.php), controlla se il villaggio attivo nel DOM corrisponde
        if current is None:
            try:
                active_did = await page.evaluate("""
                    () => {
                        const activeEl = document.querySelector(
                            '.villageList .active, .villageList li.active, ' +
                            '#sidebarBoxVillagelist .active, .listEntry.active, ' +
                            '.villageList .selected, table#vlist tr.hl'
                        );
                        if (!activeEl) return null;
                        const didAttr = activeEl.getAttribute('data-did') || activeEl.querySelector('[data-did]')?.getAttribute('data-did');
                        if (didAttr) return parseInt(didAttr, 10);
                        const href = activeEl.querySelector('a')?.getAttribute('href') || activeEl.getAttribute('href') || '';
                        const match = href.match(/newdid=(\\d+)/);
                        return match ? parseInt(match[1], 10) : null;
                    }
                """)
                if active_did == village_id:
                    return
            except Exception:
                pass

        logger.info(
            "Contesto villaggio errato (atteso newdid=%s, URL: %s): ricalibro",
            village_id,
            url,
        )
        await self.browser.goto(f"{selectors.PATH_DORF1}?newdid={village_id}")

        # Verifica post-navigazione
        after = _NEWDID.search(page.url or "")
        if after is not None and int(after.group(1)) != village_id:
            raise RuntimeError(
                f"Impossibile attivare il villaggio newdid={village_id} "
                f"(server ha risposto con newdid={after.group(1)})"
            )

    async def _upgrade_field(self, action: Action, village_id: int = 0) -> ActionResult:
        """Apre build.php?id=<slot> e clicca 'migliora' mantenendo il newdid esplicito.

        Funziona sia per i campi risorsa (slot 1-18, dorf1) sia per gli
        edifici interni (slot 19-40, dorf2): build.php?id=N apre
        direttamente il contratto di upgrade dell'edificio esistente.
        Su un'area vuota la pagina mostra invece il catalogo edifici e
        UPGRADE_BUTTON non e' presente, quindi lo slot viene saltato.
        """
        candidates: list[int] = action.params.get("candidates") or []
        if not candidates:
            return ActionResult(False, "nessun candidato per l'upgrade")

        page = self.browser.page
        assert page is not None

        for slot_id in candidates:
            # Costruisce l'URL di build includendo il newdid per garantire l'isolamento del villaggio
            build_url = f"{selectors.PATH_BUILD}?id={slot_id}"
            if village_id > 0:
                build_url += f"&newdid={village_id}"

            await self.browser.goto(build_url)

            button = None
            for sel in selectors.UPGRADE_BUTTON:
                locator = page.locator(sel).first
                try:
                    if await locator.count() == 0:
                        continue
                    disabled = await locator.get_attribute("disabled")
                    css_class = (await locator.get_attribute("class")) or ""
                    if disabled is None and "disabled" not in css_class:
                        button = locator
                        break
                except Exception:  # noqa: BLE001
                    continue

            if button is None:
                logger.debug("Slot %s non migliorabile nel villaggio %s, provo il prossimo", slot_id, village_id)
                continue

            await sleep_human(
                self.settings.action_delay_min, self.settings.action_delay_max
            )
            await button.click()
            await sleep_human(1.0, 2.0)
            logger.info("Upgrade avviato sullo slot %s (Villaggio %s)", slot_id, village_id)
            return ActionResult(True, f"upgrade avviato sullo slot {slot_id}")

        return ActionResult(
            False,
            "nessun candidato migliorabile (risorse insufficienti o livello max)",
        )

    async def _claim_video_bonus(self, action: Action) -> ActionResult:
        """Attiva il bonus +15% per 8 ore guardando il video per le 4 risorse.

        1. Apre il negozio / InfoBox se non aperto.
        2. Seleziona la scheda 'Vantaggi'.
        3. Identifica e clicca i pulsanti viola 'Attiva' (con icona video) per
           ciascuna risorsa (Legno, Argilla, Ferro, Grano).
        4. Attende la fine effettiva della riproduzione del video prima di confermare.
        5. Chiude la finestra al termine di tutte le attivazioni.
        """
        page = self.browser.page
        assert page is not None

        # 1. Se il modale di pagamento/shop non è già visibile, aprilo
        dialog_visible = await page.locator("#paymentWizard, .dialog, #modal, .modal").count() > 0
        if not dialog_visible:
            opened = await self._click_first(
                page, selectors.INFO_BOX_SHOP_BUTTON, timeout_ms=2500
            )
            if not opened:
                opened = await self._click_first(
                    page, selectors.SHOP_GOLD_BUTTON, timeout_ms=2500
                )
            if not opened:
                return ActionResult(False, "impossibile aprire il negozio (pulsante non trovato)")
            await sleep_human(1.5, 2.5)

        # 2. Seleziona la scheda 'Vantaggi'
        await self._click_first(
            page, selectors.SHOP_TAB_ADVANTAGES, timeout_ms=3000
        )
        await sleep_human(1.0, 2.0)

        # 3. Trova e attiva i bonus video (+15% per 8 ore)
        activated_count = 0
        for _ in range(4):
            clicked = await page.evaluate("""
                () => {
                    const buttons = Array.from(document.querySelectorAll(
                        '.dialog button, #paymentWizard button, .featureProduction button, .productionBoost button, .feature button'
                    ));
                    for (const b of buttons) {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        const card = (b.closest('.feature, .box, .card, .productionBonus, div') ? b.closest('.feature, .box, .card, .productionBonus, div').innerText : '').toLowerCase();
                        const pTxt = (b.parentElement ? b.parentElement.innerText : '').toLowerCase();

                        // Cerca bottoni 'Attiva' associati a +15% / 8 ore, escludendo Gold / +25%
                        const isVideoBonus = (
                            (card.includes('+15%') || pTxt.includes('+15%') || txt.includes('15%')) &&
                            (card.includes('8') || pTxt.includes('8') || card.includes('ore') || card.includes('hours')) &&
                            !b.disabled &&
                            !txt.includes('gold') &&
                            !txt.includes('25%') &&
                            !txt.includes('attivo') &&
                            !txt.includes('active')
                        );
                        if (isVideoBonus) {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if not clicked:
                break

            activated_count += 1
            logger.info("Avviato video per bonus risorsa %d/4. Attesa termine riproduzione video...", activated_count)

            # Attende il completamento effettivo del video prima di passare al prossimo
            await self._wait_for_video_ad_completion(page, max_wait_seconds=45)
            logger.info("Bonus risorsa %d/4 completato e accreditato", activated_count)
            await sleep_human(1.5, 2.5)

        # 4. Chiude il dialog del negozio
        await self._click_first(page, selectors.SHOP_DIALOG_CLOSE, timeout_ms=2000)
        await sleep_human(0.5, 1.0)

        if activated_count > 0:
            logger.info("Attivati con successo %d bonus risorsa +15%% (8 ore)", activated_count)
            return ActionResult(
                True, f"attivati {activated_count} bonus produzione risorsa (+15% 8h)"
            )
        return ActionResult(
            True, "bonus +15% già attivi o nessun bonus video attivabile"
        )

    async def _wait_for_video_ad_completion(self, page, max_wait_seconds: int = 45) -> bool:
        """Attende la fine effettiva della riproduzione del video pubblicitario.

        Non chiude prematuramente il player: monitora tag <video>, countdown e
        pulsanti di completamento/riscossione per garantire che Travian registri
        il bonus +15% per 8 ore.
        """
        # Attesa iniziale per l'avvio del player pubblicitario
        await asyncio.sleep(2.0)

        start_time = time.time()
        while time.time() - start_time < max_wait_seconds:
            # 1. Ispezione stato video nel DOM principale
            status = await page.evaluate("""
                () => {
                    // Cerca tutti i tag video (principali e visibili)
                    const videos = Array.from(document.querySelectorAll('video'));
                    for (const v of videos) {
                        if (v.ended || (v.duration && v.currentTime >= v.duration - 0.5)) {
                            return { state: 'ended', current: v.currentTime, duration: v.duration };
                        }
                        if (!v.paused && v.duration > 0) {
                            return { state: 'playing', remaining: Math.ceil(v.duration - v.currentTime) };
                        }
                    }

                    // Cerca overlay / iframe pubblicitari noti
                    const adContainers = document.querySelectorAll(
                        '#adContainer, #videoPlayer, .videoDialog, #imaContainer, #videoOverlay, iframe[src*="ad"], iframe[src*="video"], iframe[src*="ima"]'
                    );
                    const isAdVisible = Array.from(adContainers).some(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 50 && r.height > 50;
                    });

                    // Cerca pulsanti di completamento post-video (es. 'Riscuoti', 'Claim', 'Continua', 'Chiudi')
                    const endButtons = Array.from(document.querySelectorAll(
                        'button.rewardButton, button.claimButton, .videoDialog button.close, #adContainer button.close, button:has-text("Riscuoti"), button:has-text("Claim")'
                    )).filter(b => !b.disabled && b.offsetParent !== null);

                    if (endButtons.length > 0) {
                        return { state: 'ready_to_claim', hasButton: true };
                    }

                    // Se non ci sono video in riproduzione né container pubblicitari visibili,
                    // l'ad è terminato e il popup si è chiuso autonomamente
                    if (!isAdVisible && videos.length === 0) {
                        return { state: 'closed' };
                    }

                    return { state: 'waiting' };
                }
            """)

            state = (status or {}).get("state")
            if state == "ended":
                logger.debug("Video pubblicitario terminato.")
                break
            elif state == "playing":
                rem = (status or {}).get("remaining", "?")
                logger.debug("Video in riproduzione... (~%ss rimanenti)", rem)
            elif state == "ready_to_claim":
                logger.debug("Pulsante di riscossione/chiusura post-video pronto.")
                break
            elif state == "closed":
                logger.debug("Overlay video chiuso/completato.")
                break

            # Cerca anche all'interno di eventuali iframe di terze parti (IMA SDK / Azerion)
            ad_ended_in_frame = False
            for frame in page.frames:
                if frame == page.main_frame:
                    continue
                try:
                    f_status = await frame.evaluate("""
                        () => {
                            const v = document.querySelector('video');
                            if (v && (v.ended || (v.duration && v.currentTime >= v.duration - 0.5))) {
                                return true;
                            }
                            const btn = document.querySelector('.close-button, .reward-button, #close, .btn-close');
                            if (btn && !btn.disabled) {
                                btn.click();
                                return true;
                            }
                            return false;
                        }
                    """)
                    if f_status:
                        ad_ended_in_frame = True
                        break
                except Exception:
                    continue

            if ad_ended_in_frame:
                logger.debug("Video completato nel frame pubblicitario.")
                break

            await asyncio.sleep(2.0)

        # Clicca eventuali pulsanti di chiusura del player terminato (NON del negozio)
        try:
            post_ad_close = [
                "button.rewardButton",
                "button.claimButton",
                "#adContainer button.close",
                ".videoDialog .close",
                "#videoPlayer button.close",
                "button.adClose",
            ]
            for sel in post_ad_close:
                btn = page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    await asyncio.sleep(1.0)
                    break
        except Exception:  # noqa: BLE001
            pass

        # Pausa di sicurezza per permettere a Travian di confermare l'attivazione via AJAX/websocket
        await asyncio.sleep(2.5)
        return True

    async def _send_resources(self, action: Action, village_id: int = 0) -> ActionResult:
        """Invia l'eccedenza ad un altro villaggio tramite il Mercato.

        Flusso T4.x: build.php?id=<mercato>&t=5 -> tab invio -> campi r1-r4 +
        nome villaggio -> submit -> pagina di riepilogo -> conferma.
        """
        params = action.params or {}
        market_slot = params.get("market_slot")
        target = params.get("target")
        resources: dict[str, int] = params.get("resources") or {}
        if not market_slot or not target or not resources:
            return ActionResult(False, "parametri di invio incompleti")

        page = self.browser.page
        assert page is not None

        # Naviga direttamente al tab invio risorse (t=5) del mercato
        build_url = f"{selectors.PATH_BUILD}?id={market_slot}&t=5"
        if village_id > 0:
            build_url += f"&newdid={village_id}"
        await self.browser.goto(build_url)

        # 1. Stato del mercato: mercanti liberi e presenza del form di invio
        market_status = await page.evaluate(_JS_MARKET_QTY_INPUTS + """
            () => {
                const bodyText = (document.body && document.body.innerText) || '';
                // 'Mercanti: 20/20' e' la prima riga del tab invio; prendiamo
                // il PRIMO match perche' in basso compare anche 'Mercanti: 0/20'
                // (mercanti richiesti dalla spedizione corrente).
                const m = bodyText.match(
                    /(?:mercanti|merchants?|haendler|händler|comerciantes?|marchands?)[^0-9]{0,20}([0-9]+)[^0-9]{0,5}[/][^0-9]{0,5}([0-9]+)/i
                );
                const merchants = m
                    ? { available: parseInt(m[1], 10), total: parseInt(m[2], 10) }
                    : null;

                const errText = (
                    document.querySelector('.error, .none, .warning, .noMerchants, .alert')?.innerText || ''
                ).toLowerCase();
                const noMerchants = (
                    (merchants !== null && merchants.total > 0 && merchants.available === 0) ||
                    errText.includes('nessun mercante') ||
                    errText.includes('no merchant') ||
                    errText.includes('no trader') ||
                    errText.includes('kein händler')
                );

                return {
                    merchants: merchants,
                    noMerchants: noMerchants,
                    inputsCount: findQtyInputs().length,
                    url: location.href,
                };
            }
        """)

        # Se i mercanti sono esauriti (es. 0/1 in viaggio), segnala senza errori bloccanti
        if market_status and market_status.get("noMerchants"):
            m_info = market_status.get("merchants") or {}
            avail = m_info.get("available", 0)
            total = m_info.get("total", 1)
            logger.info("Mercato slot %s: nessun mercante disponibile (%d/%d in viaggio)", market_slot, avail, total)
            return ActionResult(
                False,
                f"nessun mercante disponibile al mercato ({avail}/{total} in viaggio)",
            )

        # Se gli input non sono presenti, clicca il tab 'Invia risorse' e
        # attendi che il contenuto venga caricato (tab AJAX o reload)
        if not market_status or market_status.get("inputsCount", 0) == 0:
            if await self._click_first(page, selectors.MARKET_TAB_SEND, timeout_ms=2000):
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=3000)
                except Exception:
                    pass
                await sleep_human(0.8, 1.5)

        # 2. Compila le quantità di risorse
        filled = 0
        for rtype, sel_list in selectors.MARKET_SEND_INPUTS.items():
            amount = int(resources.get(rtype, 0) or 0)
            if amount <= 0:
                continue
            for sel in sel_list:
                field = page.locator(sel).first
                try:
                    if await field.count() == 0:
                        continue
                    if await field.is_disabled():
                        continue
                    await field.fill(str(amount), timeout=1000)
                    filled += 1
                    break
                except Exception:
                    continue

        if filled == 0:
            # Fallback JS: prova i nomi classici r1-r4 / m2[n]
            filled = await page.evaluate(f"""
                (res) => {{
                    let cnt = 0;
                    const map = {{ wood: 1, clay: 2, iron: 3, crop: 4 }};
                    for (const [k, v] of Object.entries(res)) {{
                        const num = parseInt(v, 10);
                        if (!num || num <= 0) continue;
                        const idx = map[k];
                        const inp = document.querySelector(
                            `#r${{idx}}, input[name="r${{idx}}"], input[name="m2[${{idx}}]"], #m2_${{idx}}`
                        );
                        if (inp && !inp.disabled) {{
                            inp.value = num;
                            inp.dispatchEvent(new Event('input', {{ bubbles: true }}));
                            inp.dispatchEvent(new Event('change', {{ bubbles: true }}));
                            cnt++;
                        }}
                    }}
                    return cnt;
                }}
            """, resources)

        if filled == 0:
            # Fallback strutturale: le UI con slider possono usare nomi
            # qualsiasi -> prendi i campi quantita' del form in ordine DOM
            # (legno/argilla/ferro/grano).
            qty_map = {
                "wood": int(resources.get("wood", 0) or 0),
                "clay": int(resources.get("clay", 0) or 0),
                "iron": int(resources.get("iron", 0) or 0),
                "crop": int(resources.get("crop", 0) or 0),
            }
            filled = await page.evaluate(_JS_MARKET_QTY_INPUTS + """
                (res) => {
                    const order = ['wood', 'clay', 'iron', 'crop'];
                    const cands = findQtyInputs().slice(0, 4);
                    let cnt = 0;
                    order.forEach((r, idx) => {
                        const amount = parseInt(res[r], 10);
                        const inp = cands[idx];
                        if (!inp || !amount || amount <= 0) return;
                        inp.readOnly = false;
                        inp.disabled = false;
                        inp.focus();
                        inp.value = String(amount);
                        for (const ev of ['input', 'change', 'keyup', 'blur']) {
                            inp.dispatchEvent(new Event(ev, { bubbles: true }));
                        }
                        cnt++;
                    });
                    return cnt;
                }
            """, qty_map)

        if filled == 0:
            # Dump diagnostico: url finale, input presenti e testo visibile
            debug = await page.evaluate("""
                () => ({
                    url: location.href,
                    title: document.title,
                    formCount: document.forms.length,
                    inputs: Array.from(document.querySelectorAll('input'))
                        .slice(0, 40)
                        .map(i => ({
                            name: i.name || '', id: i.id || '', type: i.type || '',
                            disabled: i.disabled, readonly: i.readOnly,
                            visible: i.offsetParent !== null,
                        })),
                    text: ((document.body && document.body.innerText) || '').slice(0, 400),
                })
            """)
            logger.warning(
                "Mercato slot %s: form di invio non compilabile. url=%s forms=%s inputs=%s testo=%r",
                market_slot,
                debug.get("url"),
                debug.get("formCount"),
                debug.get("inputs"),
                debug.get("text"),
            )
            return ActionResult(
                False,
                "form di invio mercato non trovato o campi non compilabili (verificare mercanti/tab)",
            )

        # 3. Compila il villaggio di destinazione. Si digita (non fill) per
        # attivare l'autocomplete del campo nome villaggio.
        target_filled = False
        for sel in selectors.MARKET_TARGET_INPUT:
            locator = page.locator(sel).first
            try:
                if await locator.count() == 0 or await locator.is_disabled():
                    continue
                await locator.fill("", timeout=500)
                await locator.press_sequentially(str(target), delay=40, timeout=1500)
                target_filled = True
                break
            except Exception:
                continue

        if not target_filled:
            target_filled = await page.evaluate("""
                (targetName) => {
                    const inp = document.querySelector(
                        '#enterVillageName, input[name="dname"], input[name="targetVillage"], ' +
                        'input#targetVillage, input[name="villageName"], input[name="targetVillageName"], ' +
                        'input[name*="illage" i], input[id*="illage" i], ' +
                        'input[placeholder*="illage" i], input[placeholder*="illagg" i]'
                    );
                    if (inp && !inp.disabled) {
                        inp.readOnly = false;
                        inp.focus();
                        inp.value = targetName;
                        for (const ev of ['keydown', 'input', 'keyup', 'change']) {
                            inp.dispatchEvent(new Event(ev, { bubbles: true }));
                        }
                        return true;
                    }
                    return false;
                }
            """, str(target))

        if not target_filled:
            return ActionResult(
                False,
                f"campo destinazione villaggio non trovato (target '{target}')",
            )

        # Se il form usa l'autocomplete, seleziona il suggerimento che
        # contiene il nome target; altrimenti il nome digitato basta.
        await sleep_human(0.4, 0.9)
        await self._pick_target_suggestion(page, str(target))

        # Coordinate opzionali passate nei params: alcuni server le richiedono
        tx, ty = params.get("target_x"), params.get("target_y")
        if tx is not None and ty is not None:
            await page.evaluate("""
                ([x, y]) => {
                    const ix = document.querySelector('input[name="x"], input#xCoord, input[id*="xCoord"]');
                    const iy = document.querySelector('input[name="y"], input#yCoord, input[id*="yCoord"]');
                    if (ix) { ix.value = String(x); ix.dispatchEvent(new Event('input', { bubbles: true })); }
                    if (iy) { iy.value = String(y); iy.dispatchEvent(new Event('input', { bubbles: true })); }
                }
            """, [tx, ty])

        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )

        # 4. Submit del form di invio
        if not await self._click_first(page, selectors.MARKET_SEND_BUTTON):
            return ActionResult(False, "pulsante di invio mercato non trovato")
        await sleep_human(1.0, 2.0)

        # 5. T4.x mostra una pagina di riepilogo: conferma invio
        await self._click_first(page, selectors.MARKET_CONFIRM_BUTTON)
        await sleep_human(0.5, 1.0)

        logger.info(
            "Inviate %s a '%s' dal mercato slot %s (villaggio %s)",
            resources,
            target,
            market_slot,
            village_id,
        )
        return ActionResult(True, f"risorse inviate a '{target}'")

    async def _send_raid(self, action: Action, village_id: int = 0) -> ActionResult:
        """Lancia un raid dalla farm list del Punto di Raduno (gid 16).

        Flusso T4.x: build.php?id=<slot>&tt=99 -> righe liste raid ->
        'seleziona tutti' -> pulsante di avvio. Se `farm_list` e' valorizzato
        nei params, seleziona la riga il cui nome contiene quel testo.
        """
        params = action.params or {}
        rally_slot = params.get("rally_slot")
        farm_list = str(params.get("farm_list") or "").strip().lower()
        if not rally_slot:
            return ActionResult(False, "slot punto di raduno mancante")

        page = self.browser.page
        assert page is not None

        url = f"{selectors.PATH_BUILD}?id={rally_slot}&tt=99"
        if village_id > 0:
            url += f"&newdid={village_id}"
        await self.browser.goto(url)

        # Se il tab liste non mostra checkbox, prova il click esplicito sul tab
        if await page.locator("input[type='checkbox']").count() == 0:
            await self._click_first(page, selectors.RALLY_FARM_TAB, timeout_ms=2000)
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=3000)
            except Exception:
                pass
            await sleep_human(0.5, 1.0)

        # Individua la riga della farm list e spunta i target
        picked = await page.evaluate(
            """
            (args) => {
                const [rowsSelectors, wanted] = args;
                const nodes = [];
                const seen = new Set();
                for (const sel of rowsSelectors) {
                    document.querySelectorAll(sel).forEach(n => {
                        if (!seen.has(n)) { seen.add(n); nodes.push(n); }
                    });
                }
                let row = null;
                if (wanted) {
                    row = nodes.find(r =>
                        (r.innerText || '').toLowerCase().includes(wanted)
                    ) || null;
                }
                if (!row) {
                    row = nodes.find(r =>
                        r.querySelectorAll('input[type="checkbox"]').length > 0
                    ) || null;
                }
                if (!row) return { ok: false, reason: 'no_list_row' };

                const boxes = Array.from(
                    row.querySelectorAll('input[type="checkbox"]')
                ).filter(b => !b.disabled);
                const markAll = row.querySelector(
                    'input[type="checkbox"].markAll, input[type="checkbox"][name*="all" i]'
                );
                if (markAll) {
                    if (!markAll.checked) markAll.click();
                } else {
                    boxes.forEach(b => { if (!b.checked) b.click(); });
                }
                const checked = boxes.filter(b => b.checked).length;
                return {
                    ok: checked > 0,
                    checked: checked,
                    total: boxes.length,
                    reason: checked > 0 ? '' : 'no_targets_checked',
                };
            }
            """,
            [selectors.FARM_LIST_ROWS, farm_list],
        )

        if not picked or not picked.get("ok"):
            debug = await page.evaluate(
                """
                () => ({
                    url: location.href,
                    rows: document.querySelectorAll(
                        '.listEntry, tr.farmList, .farmList li'
                    ).length,
                    checkboxes: document.querySelectorAll('input[type="checkbox"]').length,
                    text: ((document.body && document.body.innerText) || '').slice(0, 300),
                })
                """
            )
            logger.warning(
                "Farm list non selezionabile (slot %s): %s debug=%s",
                rally_slot, picked, debug,
            )
            return ActionResult(
                False,
                f"farm list non trovata o target non selezionabili "
                f"({(picked or {}).get('reason', 'n/d')})",
            )

        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )

        if not await self._click_first(
            page, selectors.FARM_LIST_SEND_BUTTON, timeout_ms=2500
        ):
            return ActionResult(False, "pulsante avvio raid non trovato")
        await sleep_human(1.0, 2.0)

        # Alcune versioni mostrano una pagina di conferma prima del lancio
        await self._click_first(page, selectors.MAP_SETTLE_CONFIRM, timeout_ms=1500)

        logger.info(
            "Raid avviato da farm list (%s/%s target) nel villaggio %s",
            picked.get("checked"), picked.get("total"), village_id,
        )
        return ActionResult(
            True,
            f"raid avviato ({picked.get('checked')}/{picked.get('total')} target)",
        )

    async def _send_custom_raid(self, action: Action, village_id: int = 0) -> ActionResult:
        """Invia raid dinamici a coordinate libere calcolati dall'AI Scout Brain.

        Flusso T4.x: build.php?id=<rally_slot>&tt=2 -> inserimento coordinate (X|Y) ->
        selezione opzione Saccheggio (c=4) -> compilazione input truppe -> conferma.
        """
        params = action.params or {}
        rally_slot = params.get("rally_slot")
        raw_targets = params.get("targets") or []
        if not raw_targets and "x" in params and "y" in params:
            raw_targets = [params]

        if not rally_slot:
            return ActionResult(False, "slot punto di raduno mancante")
        if not raw_targets:
            return ActionResult(False, "nessun target valido per il raid dinamico")

        page = self.browser.page
        assert page is not None

        sent_count = 0
        errors: list[str] = []

        for tgt in raw_targets:
            tx = tgt.get("x")
            ty = tgt.get("y")
            troops = dict(tgt.get("troops") or {})
            if tx is None or ty is None or not troops:
                continue

            try:
                # 1. Naviga al form di invio truppe del Punto di Raduno
                url = f"{selectors.PATH_BUILD}?id={rally_slot}&tt=2"
                if village_id > 0:
                    url += f"&newdid={village_id}"
                await self.browser.goto(url)
                await sleep_human(0.4, 0.8)

                # 2. Compila le coordinate del target, il tipo movimento 'Saccheggio' (4) e le truppe
                fill_res = await page.evaluate(
                    """
                    (payload) => {
                        const { x, y, troops } = payload;

                        // Coordinate X / Y
                        const xIn = document.querySelector('input[name="x"], input#xCoordInput, #xCoord');
                        const yIn = document.querySelector('input[name="y"], input#yCoordInput, #yCoord');
                        if (xIn) { xIn.value = String(x); xIn.dispatchEvent(new Event('input', {bubbles: true})); }
                        if (yIn) { yIn.value = String(y); yIn.dispatchEvent(new Event('input', {bubbles: true})); }

                        // Radiobutton tipo attacco: 4 = Saccheggio (Raid), 3 = Attacco normale
                        const raidRadio = document.querySelector(
                            'input[type="radio"][value="4"], input[name="c"][value="4"], label:has(input[value="4"]) input'
                        );
                        if (raidRadio) {
                            raidRadio.checked = true;
                            raidRadio.dispatchEvent(new Event('change', {bubbles: true}));
                        }

                        // Compilazione quantità truppe
                        let filledAny = false;
                        for (const [key, qty] of Object.entries(troops)) {
                            if (qty <= 0) continue;
                            const norm = key.toLowerCase().replace('u', 't');
                            const input = document.querySelector(
                                `input[name="${norm}"], input[name*="[${norm}]"], input[name="${key}"]`
                            );
                            if (input && !input.disabled) {
                                input.value = String(qty);
                                input.dispatchEvent(new Event('input', {bubbles: true}));
                                filledAny = true;
                            }
                        }

                        return { ok: !!(xIn && yIn && filledAny) };
                    }
                    """,
                    {"x": tx, "y": ty, "troops": troops},
                )

                if not fill_res or not fill_res.get("ok"):
                    errors.append(f"compilazione form fallita per target ({tx}|{ty})")
                    continue

                await sleep_human(
                    self.settings.action_delay_min, self.settings.action_delay_max
                )

                # 3. Primo submit: invio ordine truppe
                if not await self._click_first(
                    page,
                    [
                        'button#btn_ok',
                        'button[type="submit"]',
                        '#ok',
                        'button:has-text("Invia")',
                        'button:has-text("Send")',
                    ],
                    timeout_ms=3000,
                ):
                    errors.append(f"primo invio fallito per ({tx}|{ty})")
                    continue

                await sleep_human(0.8, 1.5)

                # 4. Secondo submit: conferma del raid
                if not await self._click_first(
                    page,
                    [
                        'button#btn_ok',
                        'button[name="s1"]',
                        'input[name="s1"]',
                        'button[type="submit"]',
                        'button:has-text("Conferma")',
                        'button:has-text("Confirm")',
                    ],
                    timeout_ms=3500,
                ):
                    errors.append(f"conferma raid fallita per ({tx}|{ty})")
                    continue

                sent_count += 1
                logger.info(
                    "AI Scout: raid dinamico inviato con successo a (%s|%s) con truppe %s",
                    tx, ty, troops,
                )
                await sleep_human(1.0, 2.5)

            except Exception as exc:
                errors.append(f"eccezione su ({tx}|{ty}): {exc}")

        if sent_count > 0:
            return ActionResult(
                True,
                f"inviati {sent_count}/{len(raw_targets)} raid dinamici AI ({', '.join(errors) if errors else 'tutti riusciti'})",
            )
        return ActionResult(False, f"nessun raid dinamico inviato: {'; '.join(errors)}")

    async def _found_village(self, action: Action, village_id: int = 0) -> ActionResult:
        """Fonda un nuovo villaggio sul tile libero (x, y) via karte.php.

        Richiede 3 coloni nel villaggio corrente e CP sufficienti
        (verificati a monte dall'ExpansionManager).
        """
        params = action.params or {}
        x, y = params.get("x"), params.get("y")
        if x is None or y is None:
            return ActionResult(False, "coordinate di fondazione mancanti")

        page = self.browser.page
        assert page is not None

        await self.browser.goto(f"{selectors.PATH_MAP}?x={x}&y={y}")
        await sleep_human(0.5, 1.0)

        # Click su 'Fondare un villaggio' nel pannello dettagli tile
        if not await self._click_first(
            page, selectors.MAP_SETTLE_BUTTON, timeout_ms=3000
        ):
            debug = await page.evaluate(
                """
                () => ({
                    url: location.href,
                    links: Array.from(document.querySelectorAll('a'))
                        .slice(0, 60)
                        .map(a => (a.innerText || '').trim())
                        .filter(t => t && t.length < 40),
                    text: ((document.body && document.body.innerText) || '').slice(0, 300),
                })
                """
            )
            logger.warning(
                "Fondazione (%s|%s): bottone 'Fonda' non trovato. debug=%s",
                x, y, debug,
            )
            return ActionResult(
                False, "bottone di fondazione non trovato (tile occupato? coloni mancanti?)"
            )

        await sleep_human(0.8, 1.5)

        # Pagina/Dialog di conferma fondazione
        if not await self._click_first(
            page, selectors.MAP_SETTLE_CONFIRM, timeout_ms=3000
        ):
            return ActionResult(False, "conferma di fondazione non trovata")
        await sleep_human(1.5, 2.5)

        logger.info(
            "Fondazione avviata a (%s|%s) dal villaggio %s",
            x, y, village_id,
        )
        return ActionResult(True, f"fondazione avviata a ({x}|{y})")

    async def _train_troops(self, action: Action, village_id: int = 0) -> ActionResult:
        """Addestra truppe nella caserma/scuderia per smaltire il surplus.

        Strategia primaria (batch sizing + priority_units): legge le risorse
        dalla stock bar e i costi unitari di ogni unita' addestrabile dalla
        pagina build.php?id=<slot>. Filtra rigorosamente in base alle
        `priority_units` configurate per il villaggio (evitando esploratori/
        emissari non autorizzati), calcola il lotto massimo sostenibile con un
        margine di sicurezza (95% risorse, 90% grano) e compila l'input della
        truppa scelta. Supporta la scansione di molteplici edifici di
        addestramento (es. Caserma e Scuderia).

        Fallback: se priority_units non e' configurata e la lettura costi
        fallisce, ricade sulla logica legacy (link 'max' o 1 unita').
        """
        params = action.params or {}
        trainer_slots = params.get("trainer_slots")
        if not trainer_slots:
            slot = params.get("trainer_slot")
            trainer_slots = [slot] if slot else []
        if not trainer_slots:
            return ActionResult(False, "slot caserma/scuderia mancante")

        priority_units = params.get("priority_units")
        if priority_units is None:
            vcfg = self.settings.village_config_for(village_id=village_id)
            if vcfg:
                priority_units = vcfg.priority_units or vcfg.allowed_units
            else:
                priority_units = []

        page = self.browser.page
        assert page is not None

        # Scorre gli edifici militari candidati finché trova truppe addestrabili
        for trainer_slot in trainer_slots:
            build_url = f"{selectors.PATH_BUILD}?id={trainer_slot}"
            if village_id > 0:
                build_url += f"&newdid={village_id}"
            await self.browser.goto(build_url)

            # --- Strategia primaria: lotto calcolato su risorse/costi reali con filtro priorita' ---
            training_data = await self._extract_training_data(page)
            if training_data:
                chosen = self._pick_training_batch(
                    training_data, priority_units=priority_units
                )
                if chosen is not None:
                    input_name, quantity, unit_label = chosen
                    try:
                        await page.locator(
                            f"input[name='{input_name}']"
                        ).first.fill(str(quantity), timeout=2000)
                        await sleep_human(
                            self.settings.action_delay_min,
                            self.settings.action_delay_max,
                        )
                        if await self._click_first(
                            page, selectors.TRAIN_SUBMIT, timeout_ms=2000
                        ):
                            await sleep_human(1.0, 2.0)
                            logger.info(
                                "Addestramento avviato: %s x '%s' nello slot %s "
                                "(villaggio %s)",
                                quantity,
                                unit_label or input_name,
                                trainer_slot,
                                village_id,
                            )
                            return ActionResult(
                                True,
                                f"addestramento avviato: {quantity} unita' "
                                f"(slot {trainer_slot})",
                            )
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "Batch sizing fallito sul campo %s nello slot %s: %s",
                            input_name,
                            trainer_slot,
                            exc,
                        )
                else:
                    if priority_units:
                        logger.debug(
                            "Nessuna unita' corrispondente alle priorita' %s "
                            "nello slot %s, tento prossimo edificio...",
                            priority_units,
                            trainer_slot,
                        )
                    else:
                        logger.debug(
                            "Nessuna unita' addestrabile col budget nello slot %s",
                            trainer_slot,
                        )

            # Se nessuna priorità impostata e siamo all'ultimo trainer, prova legacy fallback
            if not priority_units and trainer_slot == trainer_slots[-1]:
                return await self._train_troops_legacy(
                    page, trainer_slot, village_id
                )

        if priority_units:
            return ActionResult(
                False,
                f"nessuna unita' corrispondente alle priorita' {priority_units} addestrabile negli slot {trainer_slots}",
            )
        return ActionResult(
            False,
            f"nessuna truppa addestrabile negli slot {trainer_slots}",
        )

    async def _extract_training_data(self, page) -> dict | None:
        """Legge risorse stock bar e unita' addestrabili (input + costi + label)."""
        try:
            data = await page.evaluate(
                """
                () => {
                    const parseNum = (s) => {
                        if (!s) return NaN;
                        const m = String(s)
                            .replace(/[\\u202f\\u00a0\\s.]/g, '')
                            .match(/\\d+/);
                        return m ? parseInt(m[0], 10) : NaN;
                    };

                    // Stock bar: #l1 legno, #l2 argilla, #l3 ferro, #l4 grano
                    const resSel = {wood:'#l1', clay:'#l2', iron:'#l3', crop:'#l4'};
                    const resources = {};
                    for (const [k, sel] of Object.entries(resSel)) {
                        const el = document.querySelector(sel);
                        let val = NaN;
                        if (el) {
                            const v = el.querySelector('.value');
                            val = parseNum(v ? v.textContent : el.textContent);
                        }
                        resources[k] = isNaN(val) ? 0 : val;
                    }

                    const readCost = (box, r) => {
                        // 1. Cerca elementi icona/risorsa per la risorsa r
                        const icons = box.querySelectorAll(
                            `i.r${r}, span.r${r}, img.r${r}, .r${r}, .resource${r}`
                        );
                        for (const el of icons) {
                            // Se l'elemento stesso ha testo numerico (es. <span class="r1">120</span>)
                            let v = parseNum(el.textContent);
                            if (!isNaN(v) && v > 0) return v;

                            // Se il genitore ha .value (es. <div class="resource"><i class="r1"></i><span class="value">120</span></div>)
                            const parent = el.parentElement;
                            if (parent) {
                                const valEl = parent.querySelector('.value, .cost');
                                if (valEl) {
                                    v = parseNum(valEl.textContent);
                                    if (!isNaN(v) && v > 0) return v;
                                }
                                v = parseNum(parent.textContent);
                                if (!isNaN(v) && v > 0) return v;
                            }

                            // Controlla sibling successivo (es. <img class="r1" /> 120)
                            let n = el.nextSibling;
                            while (n) {
                                v = parseNum(n.textContent || '');
                                if (!isNaN(v) && v > 0) return v;
                                n = n.nextSibling;
                            }
                        }
                        return NaN;
                    };

                    const inputs = Array.from(
                        document.querySelectorAll("input[name^='t']")
                    ).filter(i =>
                        i.type !== 'hidden' && !i.disabled &&
                        /^t\\d+$/.test(i.name)
                    );

                    const units = [];
                    const keys = ['wood', 'clay', 'iron', 'crop'];
                    inputs.forEach(inp => {
                        const box = inp.closest(
                            '.unitWrapper, .innerTrainUnitBox, .troopWrapper,' +
                            ' .troop, .unit, .details, .troopDetails, div.unitContainer, tr, form'
                        ) || document.body;
                        const costs = {};
                        let found = 0;
                        for (let r = 1; r <= 4; r++) {
                            const v = readCost(box, r);
                            costs[keys[r - 1]] = isNaN(v) ? 0 : v;
                            if (!isNaN(v) && v > 0) found++;
                        }
                        let label = '';
                        const titleEl = box.querySelector(
                            '.tit, .troopName, .name, span.unitName, img.unit, img[alt], a[href*="help.php"], a[href*="Card"]'
                        );
                        if (titleEl) {
                            label = (titleEl.getAttribute('alt') ||
                                     titleEl.getAttribute('title') ||
                                     titleEl.innerText ||
                                     titleEl.textContent || '').trim();
                        }
                        units.push({
                            inputName: inp.name,
                            label: label,
                            costs: found >= 3 ? costs : null,
                        });
                    });

                    return {resources, units};
                }
                """
            )
            if not data or not data.get("units"):
                return None

            # Fallback statico per costi e label se il DOM non li ha forniti
            for u in data.get("units") or []:
                inp_name = (u.get("inputName") or "").lower()
                if not u.get("costs") or any(val <= 0 for val in u["costs"].values()):
                    if inp_name in STATIC_TROOP_COSTS:
                        u["costs"] = dict(STATIC_TROOP_COSTS[inp_name])
                if not u.get("label"):
                    if inp_name in STATIC_TROOP_LABELS:
                        u["label"] = STATIC_TROOP_LABELS[inp_name]

            return data
        except Exception as exc:  # noqa: BLE001
            logger.debug("Estrazione dati addestramento fallita: %s", exc)
            return None

    @staticmethod
    def _pick_training_batch(
        training_data: dict,
        priority_units: list[str] | None = None,
        res_margin: float = 0.95,
        crop_margin: float = 0.90,
    ) -> tuple[str, int, str] | None:
        """Sceglie (input_name, quantita', label) massimizzando lo smaltimento.

        Budget = risorse attuali * margine. Se priority_units e' fornita,
        filtra rigorosamente le unita' che corrispondono per sottostringa
        al nome configurato (es. 'Imperiani' -> 'Imperiano', 'Cavalieri' ->
        'Cavalieri del Generale'). Seleziona l'unita' autorizzata che massimizza
        la quota di budget consumata.
        """
        resources = training_data.get("resources") or {}
        budget = {
            "wood": int(resources.get("wood", 0) * res_margin),
            "clay": int(resources.get("clay", 0) * res_margin),
            "iron": int(resources.get("iron", 0) * res_margin),
            "crop": int(resources.get("crop", 0) * crop_margin),
        }

        raw_units = training_data.get("units") or []
        priorities = [
            p.strip().lower()
            for p in (priority_units or [])
            if p and p.strip()
        ]

        candidates: list[dict] = []
        for unit in raw_units:
            costs = unit.get("costs")
            input_name = (unit.get("inputName") or "").strip()
            label = (unit.get("label") or "").strip()
            if not costs or not input_name:
                continue

            priority_rank = -1
            if priorities:
                label_low = label.lower()
                unit_aliases = UNIT_ALIASES.get(input_name.lower(), set())
                matched = False
                for idx, p in enumerate(priorities):
                    # 1. Match diretto per sottostringa su label
                    if (
                        p in label_low
                        or label_low in p
                        or (p.rstrip("si") in label_low if len(p) > 3 else False)
                        or (label_low.rstrip("si") in p if len(label_low) > 3 else False)
                    ):
                        # Evita falsi positivi: se la priorità è generica 'cavalieri', non catturare gli scout ('emissario a cavallo')
                        if "scout" in unit_aliases or "emissari" in label_low or "spia" in label_low or "esplorator" in label_low:
                            if "emissar" not in p and "scout" not in p and "spia" not in p and "esplorat" not in p:
                                continue
                        matched = True
                        priority_rank = idx
                        break

                    # 2. Match via alias di unità / id truppa (es. 'cavalieri' o 'cavalieri del generale' per t5/t6)
                    if unit_aliases:
                        for alias in unit_aliases:
                            if (
                                p in alias
                                or alias in p
                                or (p.rstrip("si") in alias if len(p) > 3 else False)
                                or (alias.rstrip("si") in p if len(alias) > 3 else False)
                            ):
                                matched = True
                                priority_rank = idx
                                break
                    if matched:
                        break

                if not matched:
                    continue  # Esclude truppe non autorizzate (es. scout/emissari)

            max_units = None
            for key in ("wood", "clay", "iron", "crop"):
                cost = int(costs.get(key) or 0)
                if cost <= 0:
                    continue
                quota = budget[key] // cost
                max_units = quota if max_units is None else min(max_units, quota)

            if not max_units or max_units <= 0:
                continue

            total_budget = sum(budget.values()) or 1
            consumed = sum(
                int(costs.get(k) or 0) * max_units
                for k in ("wood", "clay", "iron", "crop")
            )
            score = consumed / total_budget
            candidates.append({
                "input_name": input_name,
                "max_units": max_units,
                "label": label,
                "score": score,
                "priority_rank": priority_rank,
            })

        if not candidates:
            return None

        # Se sono presenti priorita', ordina per rank (priorita' specificata) e poi per score (consumo)
        if priorities:
            candidates.sort(key=lambda c: (c["priority_rank"], -c["score"]))
        else:
            candidates.sort(key=lambda c: -c["score"])

        best = candidates[0]
        return (best["input_name"], best["max_units"], best["label"])

    async def _train_troops_legacy(
        self, page, trainer_slot: int, village_id: int
    ) -> ActionResult:
        """Fallback: link 'max' o inserimento di 1 unita' nel primo input."""
        max_clicked = await self._click_first(
            page, selectors.TRAIN_MAX_LINKS, timeout_ms=1000
        )
        if max_clicked:
            await sleep_human(0.3, 0.7)

        has_value = False
        for sel in selectors.TRAIN_INPUTS:
            try:
                loc = page.locator(sel)
                cnt = await loc.count()
                for i in range(cnt):
                    field = loc.nth(i)
                    val = (await field.input_value(timeout=500)).strip()
                    if val and val != "0":
                        has_value = True
                        break
            except Exception:  # noqa: BLE001
                continue
            if has_value:
                break

        if not has_value:
            filled = False
            for sel in selectors.TRAIN_INPUTS:
                field = page.locator(sel).first
                try:
                    if await field.count() == 0:
                        continue
                    disabled = await field.get_attribute("disabled", timeout=1000)
                    if disabled is not None:
                        continue
                    await field.fill("1", timeout=1000)
                    filled = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            if not filled:
                return ActionResult(
                    False,
                    "nessuna unita' addestrabile trovata (edificio/troops sbloccati?)",
                )

        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )
        if not await self._click_first(page, selectors.TRAIN_SUBMIT, timeout_ms=2000):
            return ActionResult(False, "pulsante di addestramento non trovato")
        await sleep_human(1.0, 2.0)

        logger.info(
            "Addestramento avviato nello slot %s (villaggio %s)",
            trainer_slot,
            village_id,
        )
        return ActionResult(True, f"addestramento avviato (slot {trainer_slot})")

    async def _click_first(
        self, page, selectors_list: list[str], timeout_ms: int = 1500
    ) -> bool:
        """Clicca il primo selettore presente e non disabilitato."""
        for sel in selectors_list:
            locator = page.locator(sel).first
            try:
                if await locator.count() == 0:
                    continue
                disabled = await locator.get_attribute("disabled", timeout=timeout_ms)
                css_class = (await locator.get_attribute("class", timeout=timeout_ms)) or ""
                if disabled is None and "disabled" not in css_class:
                    await locator.click(timeout=timeout_ms)
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _pick_target_suggestion(self, page, target: str) -> bool:
        """Se il campo destinazione mostra un autocomplete, clicca il
        suggerimento che contiene il nome del villaggio target."""
        for sel in selectors.MARKET_TARGET_SUGGESTION:
            items = page.locator(sel)
            try:
                count = await items.count()
            except Exception:  # noqa: BLE001
                continue
            for idx in range(min(count, 8)):
                item = items.nth(idx)
                try:
                    if not await item.is_visible():
                        continue
                    text = (await item.inner_text() or "").lower()
                    if target.lower() in text:
                        await item.click(timeout=800)
                        return True
                except Exception:  # noqa: BLE001
                    continue
        return False