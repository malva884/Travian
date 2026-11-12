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
from .models import (
    Action,
    ActionResult,
    ActionType,
    STATIC_TROOP_LABELS,
)

logger = logging.getLogger(__name__)

_NEWDID = re.compile(r"newdid=(\d+)")

# Snippet JS condiviso: individua gli input quantita' del form di invio
# mercato. Le UI con slider (T4.5+) possono usare nomi diversi da r1-r4:
# si prendono i campi testo/numerici dentro il form che contiene il campo
# destinazione, escludendo nome villaggio, coordinate X/Y e ricerca.
_JS_MARKET_QTY_INPUTS = """
    const isQtyInput = (i) => {
        const t = (i.type || 'text').toLowerCase();
        if (t !== 'text' && t !== 'number' && t !== '') return false;
        const key = ((i.name || '') + ' ' + (i.id || '')).toLowerCase().trim();
        if (/^(x|y)$/.test(key)) return false;
        if (/dname|village|target|player|search|name|message|note/.test(key)) return false;
        return true;
    };
    const isTargetInput = (i) => {
        const t = (i.type || 'text').toLowerCase();
        if (t !== 'text' && t !== 'search' && t !== '') return false;
        const key = ((i.name || '') + ' ' + (i.id || '')).toLowerCase().trim();
        if (/dname|target.*village|village.*name|entervillage/.test(key)) return true;
        if (/^village$|^target$|destination|newvillage|selectvillage|recipient/.test(key)) return true;
        const ph = (i.placeholder || i.getAttribute('aria-label') || '').toLowerCase();
        return /villaggio|village|destinazione|destination/.test(ph);
    };
    const villageNames = Array.from(document.querySelectorAll(
        '.villageList a, .villageList .name, #sidebarBoxVillagelist a, ' +
        '#sidebarBoxVillagelist .name, .villageInput option'
    )).map(e => (e.innerText || '').trim().toLowerCase()).filter(t => t.length > 0);
    const isVillageSelect = (s) => {
        if (!s.options || s.options.length < 2) return false;
        const optTexts = Array.from(s.options).map(o => (o.text || '').toLowerCase());
        return villageNames.some(v => optTexts.some(o => o.includes(v)));
    };
    const findSendForm = () => {
        // Aggiunti i selettori specifici per le schermate moderne di invio risorse nel mercato
        const scopes = document.querySelectorAll(
            '#sendResources, .sendResources, #marketplaceSend, .marketplaceSend, ' +
            '.marketplace, #marketplace, [class*="sendRes" i], [class*="market" i], ' +
            '.contentNavi, #content, .content, main, form, div[class*="send" i]'
        );
        for (const scope of scopes) {
            const inputs = Array.from(scope.querySelectorAll('input'));
            const hasQty = inputs.some(i => isQtyInput(i) && !isTargetInput(i));
            if (!hasQty) continue;
            const hasTarget = inputs.some(isTargetInput) ||
                !!Array.from(scope.querySelectorAll('select')).find(isVillageSelect);
            if (hasTarget) return scope;
        }
        // Fallback di ultima istanza: ancora al form/contenitore che ospita
        // un input risorse (lumber/r1...). MAI document.body: i selettori
        // "scoped" cercherebbero in tutta la pagina e il campo nome
        // destinazione potrebbe agganciare l'input di rinomina villaggio.
        const resInp = document.querySelector(
            'input[name="lumber"], input[name="r1"], input#r1, ' +
            'input[name="clay"], input[name="r2"], input#r2'
        );
        if (resInp) {
            return resInp.closest('form') || resInp.parentElement || null;
        }
        return null;
    };
    const findQtyInputs = () => {
        const scope = findSendForm();
        return scope
            ? Array.from(scope.querySelectorAll('input'))
                  .filter(i => isQtyInput(i) && !isTargetInput(i))
            : [];
    };
    const findTargetInput = () => {
        const scope = findSendForm();
        if (!scope) return null;
        return Array.from(scope.querySelectorAll('input')).find(isTargetInput) || null;
    };
"""

# Clicca il pulsante 'Attiva' del bonus VIDEO (+15% per 8 ore). Il pulsante
# gold '+25% per 7 giorni' (es. "Attiva 5", con prezzo) sta nella STESSA
# card e scalerebbe gold senza conferma: si accetta solo un bottone che
# viene DOPO il testo '+15%' nel DOM e senza cifre (prezzo) nel testo.
# Clicca il pulsante 'Attiva' del bonus VIDEO (+15% per 8 ore).
_JS_CLICK_VIDEO_BONUS = """
    () => {
        // Cerca qualsiasi elemento che contenga il bonus del 15% o la durata di 8 ore
        const elements = Array.from(document.querySelectorAll('div, span, p, td, li, .advantage, .item'));

        for (const el of elements) {
            const t = (el.innerText || '').toLowerCase();
            // Controllo flessibile: basta che trovi il +15% e il riferimento temporale o video
            const has15 = /\\+\\s*15\\s*%/.test(t);
            const hasDuration = /(8|otto)\\s*(ore|hours?|hrs?|h)?\\b/.test(t) || t.includes('video');

            if (has15 && hasDuration) {
                // Cerca il bottone di attivazione all'interno dello stesso blocco o genitore vicino
                let scope = el;
                for (let i = 0; i < 4 && scope; i++) {
                    const btn = Array.from(scope.querySelectorAll(
                        'button, a, input[type="submit"], input[type="button"], .textButtonV1, .green'
                    )).find(b => {
                        if (b.disabled || b.offsetParent === null) return false;
                        const bt = (b.innerText || b.value || '').trim().toLowerCase();
                        // Accetta bottoni di attivazione o visione video ed esclude i prezzi in Gold (es. cifre numeriche)
                        const isActivate = /attiva|activate|guarda|watch|start|gratis|free/.test(bt);
                        const hasGoldPrice = /\\d/.test(bt) && !bt.includes('15');
                        return isActivate && !hasGoldPrice;
                    });

                    if (btn) { 
                        btn.click(); 
                        return true; 
                    }
                    scope = scope.parentElement;
                }
            }
        }
        return false;
    }
"""

# Numero di dialog/modal visibili (baseline per rilevare l'apertura del popup ad)
_JS_DIALOG_COUNT = """
    () => Array.from(document.querySelectorAll(
        '.dialog, .popupWrapper, .modalDialog, [role="dialog"], .overlay, .modal'
    )).filter(el => {
        const r = el.getBoundingClientRect();
        const s = getComputedStyle(el);
        return r.width > 80 && r.height > 60 && s.display !== 'none' && s.visibility !== 'hidden';
    }).length
"""

# Rileva se il popup del video ad e' presente (video, iframe ad o nuovo dialog)
_JS_AD_PRESENT = """
    (baseDialogs) => {
        const isVis = (el) => {
            const r = el.getBoundingClientRect();
            if (r.width < 80 || r.height < 60) return false;
            const s = getComputedStyle(el);
            return s.display !== 'none' && s.visibility !== 'hidden';
        };
        if (Array.from(document.querySelectorAll('video')).some(isVis)) return 'video';
        for (const f of document.querySelectorAll('iframe')) {
            const src = (f.src || '').toLowerCase();
            if (isVis(f) && /ad|azerion|ima|video|improved|pubmatic|doubleclick|advert/.test(src)) return 'iframe-ad';
        }
        const dialogs = Array.from(document.querySelectorAll(
            '.dialog, .popupWrapper, .modalDialog, [role="dialog"], .overlay, .modal'
        )).filter(isVis);
        if (dialogs.length > baseDialogs) return 'dialog';
        return null;
    }
"""

# Conta i marker di bonus gia' attivi ("+15% attivo per 07:59:00")
_JS_ACTIVE_BONUS_MARKS = """
    () => ((document.body.innerText || '')
        .match(/attiv[oa]?\\s+per\\s+\\d|active\\s+for\\s+\\d/gi) || []).length
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
            case ActionType.TRAIN_SETTLERS:
                return await self._train_settlers(action, village_id=vid_int)
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
        UPGRADE_BUTTON non e' presente: per le NUOVE costruzioni il brain
        passa params['construct'] = {slot_id: gid} e si apre il contratto
        diretto dorf2.php?a=<gid>&id=<slot> (T4.x), dove il pulsante di
        avvio costruzione e' lo stesso UPGRADE_BUTTON.
        """
        candidates: list[int] = action.params.get("candidates") or []
        if not candidates:
            return ActionResult(False, "nessun candidato per l'upgrade")

        # slot_id -> gid per le nuove costruzioni su aree vuote
        construct_raw = action.params.get("construct") or {}
        construct: dict[int, int] = {}
        for k, v in construct_raw.items():
            try:
                construct[int(k)] = int(v)
            except (TypeError, ValueError):
                continue

        page = self.browser.page
        assert page is not None

        for slot_id in candidates:
            # Costruisce l'URL di build includendo il newdid per garantire l'isolamento del villaggio
            gid = construct.get(int(slot_id), 0)
            if gid > 0:
                # Nuova costruzione su area vuota: contratto diretto del gid
                build_url = f"{selectors.PATH_DORF2}?a={gid}&id={slot_id}"
            else:
                build_url = f"{selectors.PATH_BUILD}?id={slot_id}"
            if village_id > 0:
                build_url += f"&newdid={village_id}"

            await self.browser.goto(build_url)

            # Verifica preventiva: la pagina segnala livello massimo raggiunto?
            try:
                is_max_level = await page.evaluate(
                    """
                    () => /livello massimo|fully upgraded|max(imal)?(imum)?\\s*level|
                           completamente migliorato|maximale stufe|niveau max/i
                        .test(document.body.innerText || '')
                    """
                )
            except Exception:  # noqa: BLE001
                is_max_level = False
            if is_max_level:
                logger.info(
                    "Slot %s gia' al livello massimo nel villaggio %s: cerco alternativa",
                    slot_id,
                    village_id,
                )
                continue

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

        # 3. Attiva i bonus video (+15% per 8 ore), una risorsa alla volta:
        # click 'Attiva' -> popup video -> chiusura automatica a fine video.
        # Baseline dialog/pagine per rilevare il popup; i marker
        # '+15% attivo per' verificano l'accredito reale.
        base_dialogs = await page.evaluate(_JS_DIALOG_COUNT)
        base_pages = len(page.context.pages) if page.context else 1

        activated_count = 0
        fails = 0
        for _ in range(4):
            if fails >= 2:
                break
            active_before = await page.evaluate(_JS_ACTIVE_BONUS_MARKS)
            clicked = await page.evaluate(_JS_CLICK_VIDEO_BONUS)
            if not clicked:
                break

            logger.info(
                "Video bonus: popup avviato, attesa riproduzione (%d/4 gia' accreditati)",
                activated_count,
            )
            completed = await self._wait_for_video_ad_completion(
                page,
                base_dialog_count=base_dialogs,
                base_page_count=base_pages,
            )

            active_after = await page.evaluate(_JS_ACTIVE_BONUS_MARKS)
            if active_after > active_before:
                activated_count += 1
                logger.info("Bonus +15%% accreditato (%d/4 risorse)", activated_count)
            else:
                fails += 1
                logger.warning(
                    "Attivazione non verificata (popup chiuso=%s): tentativo %d/2",
                    completed,
                    fails,
                )
            await sleep_human(1.5, 2.5)

        # 4. Chiude il dialog del negozio
        await self._click_first(page, selectors.SHOP_DIALOG_CLOSE, timeout_ms=2000)
        await sleep_human(0.5, 1.0)

        if activated_count > 0:
            logger.info("Attivati con successo %d bonus risorsa +15%% (8 ore)", activated_count)
            return ActionResult(
                True, f"attivati {activated_count} bonus produzione risorsa (+15% 8h)"
            )
        if fails > 0:
            return ActionResult(
                False,
                "popup video non confermato: nessun bonus accreditato",
            )
        return ActionResult(
            True, "bonus +15% già attivi o nessun bonus video attivabile"
        )

    async def _wait_for_video_ad_completion(
        self,
        page,
        base_dialog_count: int = 0,
        base_page_count: int = 1,
        appear_timeout: float = 15.0,
        max_wait_seconds: float = 100.0,
    ) -> bool:
        """Attende apertura e chiusura autonoma del popup del video ad.

        Fase 1 (max `appear_timeout`s): il popup deve APPARIRE — overlay con
        <video>/iframe ad in pagina oppure nuova scheda del browser. Se non
        appare, il click non e' andato a buon fine (evita falsi positivi).
        Fase 2 (max `max_wait_seconds`s): il popup si chiude da solo a fine
        video; se resta aperto dopo la fine del video si prova la chiusura
        manuale via X del dialog che contiene l'ad (mai lo shop).
        """
        # --- fase 1: attesa apertura popup ---
        start = time.time()
        opened = False
        while time.time() - start < appear_timeout:
            try:
                if len(page.context.pages) > base_page_count:
                    opened = True
                    break
            except Exception:  # noqa: BLE001
                pass
            if await page.evaluate(_JS_AD_PRESENT, base_dialog_count):
                opened = True
                break
            await asyncio.sleep(0.6)

        if not opened:
            logger.warning(
                "Popup video non rilevato entro %.0fs: click non riuscito?",
                appear_timeout,
            )
            return False

        logger.debug("Popup video aperto: attesa termine riproduzione")

        # --- fase 2: attesa chiusura autonoma a fine video ---
        start = time.time()
        ended_since: float | None = None
        while time.time() - start < max_wait_seconds:
            popup_open = False
            try:
                popup_open = len(page.context.pages) > base_page_count
            except Exception:  # noqa: BLE001
                pass

            state = None
            if not popup_open:
                state = await page.evaluate(_JS_AD_PRESENT, base_dialog_count)
            if not popup_open and not state:
                await asyncio.sleep(1.5)  # lascia completare l'accredito AJAX
                logger.debug("Popup video chiuso")
                return True

            if ended_since is None and await self._video_ended_anywhere(page):
                ended_since = time.time()
                logger.debug("Video terminato: attesa chiusura popup")

            # Video finito da >10s ma popup ancora aperto -> chiusura via X
            if ended_since and time.time() - ended_since > 10:
                if await self._close_ad_overlay(page):
                    await asyncio.sleep(1.5)
                    logger.debug("Popup video chiuso manualmente dopo fine video")
                    return True
                ended_since = time.time()  # riprova dopo altri 10s

            await asyncio.sleep(1.5)

        # Timeout: popup mai chiuso -> chiusura forzata e segnala fallimento
        await self._close_ad_overlay(page)
        logger.warning("Popup video non chiuso entro %.0fs", max_wait_seconds)
        return False

    async def _video_ended_anywhere(self, page) -> bool:
        """True se un <video> (pagina o iframe ad) ha raggiunto la fine."""
        js = """
            () => Array.from(document.querySelectorAll('video'))
                .some(v => v.ended || (v.duration && v.currentTime >= v.duration - 0.5))
        """
        try:
            if await page.evaluate(js):
                return True
        except Exception:  # noqa: BLE001
            pass
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                if await frame.evaluate(js):
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    async def _close_ad_overlay(self, page) -> bool:
        """Clicca la X del dialog che contiene il video ad (mai lo shop)."""
        try:
            return bool(await page.evaluate("""
                () => {
                    const dialogs = document.querySelectorAll(
                        '.dialog, .popupWrapper, .modalDialog, [role="dialog"], .modal'
                    );
                    for (const d of dialogs) {
                        const r = d.getBoundingClientRect();
                        if (r.width < 80 || r.height < 60) continue;
                        const hasAd = d.querySelector('video') || d.querySelector(
                            'iframe[src*="ad" i], iframe[src*="video" i], ' +
                            'iframe[src*="azerion" i], iframe[src*="ima" i]'
                        );
                        if (!hasAd) continue;
                        const c = d.querySelector(
                            '.close, .cancel, .closeButton, .dialogClose, ' +
                            '[class*="close" i], button[title*="chiud" i], ' +
                            'button[title*="close" i], a[title*="close" i]'
                        );
                        if (c) { c.click(); return true; }
                    }
                    return false;
                }
            """))
        except Exception:  # noqa: BLE001
            return False

    async def _dump_market_html(self, page) -> None:
        """Salva l'HTML della pagina mercato in debug/<account>/market_state.html."""
        try:
            debug_dir = getattr(self.settings, "debug_dir", None)
            if debug_dir is None:
                return
            debug_dir.mkdir(parents=True, exist_ok=True)
            (debug_dir / "market_state.html").write_text(
                await page.content(), encoding="utf-8"
            )
            await page.screenshot(
                path=str(debug_dir / "market_state.png"), full_page=True
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Dump HTML mercato fallito: %s", exc)

    async def _select_send_resources_tab(self, page) -> bool:
        """Clicca sul tab 'Invia risorse' (secondo tab, t=5) nel mercato se non e' gia' attivo."""
        # Se siamo già sulla scheda t=5, la consideriamo già aperta
        current_url = page.url or ""
        if "t=5" in current_url:
            return True

        # 1. Prova prima con selettori specifici per la scheda send (t=5, ecc.)
        for sel in selectors.MARKET_TAB_SEND:
            try:
                tab_loc = page.locator(sel).first
                if await tab_loc.count() > 0 and await tab_loc.is_visible():
                    await tab_loc.click(timeout=1500)
                    return True
            except Exception:  # noqa: BLE001
                continue

        # 2. Fallback JS avanzato (ricerca per href 't=5', per testo, o per posizione)
        try:
            clicked = await page.evaluate(
                """
                () => {
                    const linkT5 = document.querySelector('a[href*="t=5"]');
                    if (linkT5) {
                        linkT5.click();
                        return true;
                    }

                    const tabs = Array.from(document.querySelectorAll(
                        '.tabItem, .container.tabItem, a, button, div, .tab, div[class*="tab"]'
                    ));
                    const needles = [
                        'invia risorse', 'send resources', 'ressourcen senden',
                        'envoyer des ressources', 'enviar recursos',
                    ];
                    const sendTab = tabs.find(el => {
                        const text = (el.innerText || '').trim().toLowerCase();
                        return needles.some(n => text === n || text.includes(n));
                    });
                    if (sendTab) {
                        if (/active|selected|current/i.test(sendTab.className || '') ||
                            sendTab.getAttribute('aria-selected') === 'true') {
                            return true;
                        }
                        sendTab.click();
                        return true;
                    }
                    // Fallback posizionale: secondo tab della barra mercato (Invia risorse)
                    const altTab = document.querySelector(
                        '.tabItem:nth-child(2), .tabItem.normal:nth-child(2), ' +
                        '.contentNavi .tabItem:nth-child(2), nav.tabItem:nth-child(2)'
                    );
                    if (altTab) {
                        altTab.click();
                        return true;
                    }
                    return false;
                }
                """
            )
            return bool(clicked)
        except Exception:  # noqa: BLE001
            return False

    async def _calculate_capped_resources(
        self, page, requested_resources: dict
    ) -> dict:
        """Limita le risorse alla portata totale dei mercanti disponibili.

        Legge 'Mercanti disponibili: N' (o il pattern N/M) dalla pagina e
        moltiplica per la portata unitaria della tribu' (Romani 500,
        Galli 750, Teutoni 1000 — rilevata dalle icone unit; fallback
        conservativo 500).
        """
        merchant_data = await page.evaluate(
            """
            () => {
                const bodyText = document.body.innerText || '';
                let match = bodyText.match(/Mercanti disponibili:\\s*(\\d+)/i);
                let available = match ? parseInt(match[1], 10) : -1;
                if (available < 0) {
                    const m2 = bodyText.match(
                        /(?:mercanti|merchants?|haendler|händler|comerciantes?|marchands?)[^0-9]{0,20}([0-9]+)[^0-9]{0,5}[/][^0-9]{0,5}([0-9]+)/i
                    );
                    if (m2) available = parseInt(m2[1], 10);
                }
                if (available < 0) available = 1;  // non rilevato: ipotesi minima

                // Tribu' dalle icone unit presenti in pagina (u1-10 Romani,
                // u11-20 Teutoni, u21-30 Galli)
                let tribe = '';
                for (const el of document.querySelectorAll('[class*="unit u"]')) {
                    const m = (el.getAttribute('class') || '').match(/\\bu(\\d+)\\b/);
                    if (!m) continue;
                    const u = parseInt(m[1], 10);
                    if (u >= 1 && u <= 10) { tribe = 'romans'; break; }
                    if (u >= 11 && u <= 20) { tribe = 'teutons'; break; }
                    if (u >= 21 && u <= 30) { tribe = 'gauls'; break; }
                }
                const capacityPerMerchant =
                    tribe === 'teutons' ? 1000 :
                    tribe === 'gauls' ? 750 : 500;

                return {
                    available: available,
                    capacity: available * capacityPerMerchant,
                };
            }
            """
        ) or {}

        max_capacity = int(merchant_data.get("capacity") or 0)
        total_requested = sum(int(v or 0) for v in requested_resources.values())
        if total_requested <= 0:
            return {}
        if max_capacity <= 0:
            return {}
        if total_requested <= max_capacity:
            return requested_resources  # un solo viaggio sufficiente

        scale_factor = max_capacity / total_requested
        capped = {
            res: int(int(amount or 0) * scale_factor)
            for res, amount in requested_resources.items()
        }
        logger.info(
            "Carico risorse ridimensionato da %d a %d per rispettare la "
            "capienza dei mercanti (disponibili %d, capienza %d)",
            total_requested, sum(capped.values()),
            int(merchant_data.get("available") or 0), max_capacity,
        )
        return capped

    async def _resolve_village_coords_from_sidebar(
        self, page, target_name: str
    ) -> tuple[int | None, int | None]:
        """Cerca nella sidebar dei villaggi le coordinate (X, Y) associate al nome del villaggio target.

        Supporta sia Travian T4 classico sia T4.6 con contenitori separati:
        .coordinates .coordinateX, .coordinateY, span.coordinateBox, etc.
        Inoltre verifica la configurazione dei villaggi in Settings come fallback primario.
        """
        if not target_name:
            return None, None

        # 1. Fallback immediato su Settings se il villaggio e' gia' configurato
        try:
            if hasattr(self, "settings") and self.settings:
                vcfg = self.settings.village_config_for(target_name)
                if vcfg and vcfg.x is not None and vcfg.y is not None:
                    return vcfg.x, vcfg.y
        except Exception:  # noqa: BLE001
            pass

        # 2. Ispezione avanzata DOM pagina attiva
        try:
            coords = await page.evaluate(
                """
                (needle) => {
                    const norm = (s) => (s || '').trim().toLowerCase();
                    const targetNorm = norm(needle);

                    const entries = Array.from(document.querySelectorAll(
                        '#sidebarBoxVillagelist .villageList .entry, ' +
                        '.villageList .entry, .villageList li, ' +
                        '#sidebarBoxVillagelist li, [id*="villagelist" i] li'
                    ));

                    for (const entry of entries) {
                        const nameEl = entry.querySelector('.name, .villageName, .entryName, label, .headline') || entry;
                        const rawName = (nameEl?.innerText || nameEl?.textContent || '').trim();
                        const cleaned = rawName
                            .replace(/\\s*\\(\\s*[-+]?\\d+\\s*[|/]\\s*[-+]?\\d+\\s*\\)/g, '')
                            .replace(/\\s+/g, ' ')
                            .trim();

                        const fullText = (entry.innerText || entry.textContent || '').trim();

                        if (norm(cleaned) === targetNorm || norm(rawName).includes(targetNorm) || norm(fullText).includes(targetNorm)) {
                            // A) Cerca elementi dedicati coordinate T4.6 (.coordinateX / .coordinateY)
                            const xEl = entry.querySelector('.coordinateX, [class*="coordinateX"], .x');
                            const yEl = entry.querySelector('.coordinateY, [class*="coordinateY"], .y');
                            if (xEl && yEl) {
                                const parseCoord = (txt) => {
                                    const m = (txt || '').match(/[-+]?\\d+/);
                                    return m ? parseInt(m[0], 10) : null;
                                };
                                const px = parseCoord(xEl.innerText || xEl.textContent);
                                const py = parseCoord(yEl.innerText || yEl.textContent);
                                if (px !== null && py !== null) {
                                    return [px, py];
                                }
                            }

                            // B) Cerca regex standard (X|Y) o (X/Y) nel testo dell'entry
                            const m = fullText.match(/\\(\\s*([-+]?\\d+)\\s*[|/]\\s*([-+]?\\d+)\\s*\\)/) ||
                                      rawName.match(/\\(\\s*([-+]?\\d+)\\s*[|/]\\s*([-+]?\\d+)\\s*\\)/);
                            if (m) {
                                return [parseInt(m[1], 10), parseInt(m[2], 10)];
                            }

                            // B2) Link mappa karte.php?x=..&y=.. dentro l'entry
                            const karteLink = entry.querySelector('a[href*="karte.php"]');
                            if (karteLink) {
                                const kh = karteLink.href || karteLink.getAttribute('href') || '';
                                const kx = kh.match(/[?&]x=([-+]?\\d+)/);
                                const ky = kh.match(/[?&]y=([-+]?\\d+)/);
                                if (kx && ky) {
                                    return [parseInt(kx[1], 10), parseInt(ky[1], 10)];
                                }
                            }

                            // C) Controlla attributi data (data-x, data-y)
                            const dataX = entry.getAttribute('data-x');
                            const dataY = entry.getAttribute('data-y');
                            if (dataX !== null && dataY !== null) {
                                return [parseInt(dataX, 10), parseInt(dataY, 10)];
                            }
                        }
                    }

                    // Fallback globale su qualsiasi link con newdid o testo del villaggio
                    const allLinks = Array.from(document.querySelectorAll('a[href*="newdid"], a[href*="dorf"]'));
                    for (const a of allLinks) {
                        const text = (a.innerText || a.textContent || '').trim();
                        if (norm(text).includes(targetNorm)) {
                            const m = text.match(/\\(\\s*([-+]?\\d+)\\s*[|/]\\s*([-+]?\\d+)\\s*\\)/);
                            if (m) {
                                return [parseInt(m[1], 10), parseInt(m[2], 10)];
                            }
                        }
                    }
                    return null;
                }
                """,
                str(target_name),
            )
            if coords and len(coords) == 2:
                return coords[0], coords[1]
        except Exception:  # noqa: BLE001
            pass
        return None, None

    async def _send_resources(self, action: Action, village_id: int = 0) -> ActionResult:
        """Invia risorse tramite il Mercato secondo la sequenza obbligatoria.

        Sequenza con validazione rigida nome villaggio & coordinate:
        1. Navigazione all'edificio (build.php?id=<market_slot>&gid=17)
        2. Selezione del tab 'Invia risorse' (secondo tab, icona frecce di scambio)
        3. Controllo parametri obbligatori: sia target_village che coordinate (X, Y)
           devono essere presenti (se non nei params, risolti dalla sidebar)
        4. Compilazione contestuale: nome villaggio target E coordinate (X, Y)
        5. Inserimento risorse capped (r1, r2, r3, r4) filtrate dalla capienza mercanti
        6. Clic sul pulsante di conferma finale "Invia risorse"
        """
        params = action.params or {}
        market_slot = params.get("market_slot", 24)
        target = params.get("target_village") or params.get("target")
        resources_dict: dict[str, int] = params.get("resources", {})
        if not market_slot or not target or not resources_dict:
            return ActionResult(False, "parametri di invio incompleti: mercato, target o risorse mancanti")

        page = self.browser.page
        assert page is not None

        # Risoluzione preventiva coordinate target (se passate in params o da configurazione)
        tx = params.get("target_x")
        ty = params.get("target_y")
        if tx is None or ty is None:
            try:
                t_cfg = self.settings.village_config_for(str(target))
                if t_cfg and t_cfg.x is not None and t_cfg.y is not None:
                    tx, ty = t_cfg.x, t_cfg.y
            except Exception:  # noqa: BLE001
                pass

        # 1. Naviga all'URL del mercato associato (t=5 = Invia risorse; con x e y precompila nativamente)
        build_url = f"{selectors.PATH_BUILD}?id={market_slot}&gid=17&t=5"
        if tx is not None and ty is not None:
            build_url += f"&x={tx}&y={ty}"
        if village_id > 0:
            build_url += f"&newdid={village_id}"

        await self.browser.goto(build_url)
        await sleep_human(1.0, 1.5)

        # Identita' del mittente per i log (nome + coordinate se note)
        sender_name = await self._active_village_name(page) or f"newdid {village_id}"
        sender_coords = ""
        try:
            s_cfg = self.settings.village_config_for(village_id=village_id)
            if s_cfg and s_cfg.x is not None and s_cfg.y is not None:
                sender_coords = f"({s_cfg.x}|{s_cfg.y})"
        except Exception:  # noqa: BLE001
            pass

        # Verifica preventiva presenza edificio mercato
        is_market_valid = await page.evaluate("""
            () => {
                const bodyText = ((document.body && document.body.innerText) || '').toLowerCase();
                const titleText = ((document.querySelector('h1, h2, .title')?.innerText) || '').toLowerCase();
                if (titleText.includes('edificio') || titleText.includes('costruisci') ||
                    bodyText.includes('questo edificio non esiste') ||
                    bodyText.includes('this building does not exist')) {
                    return false;
                }
                return true;
            }
        """)
        if not is_market_valid:
            logger.info("Villaggio slot %s: Mercato (gid=17) non presente o non valido in questo slot", market_slot)
            return ActionResult(False, "mercato non presente in questo villaggio o slot errato")

        # 2. STEP CRITICO: Clicca sul tab "Invia risorse" (secondo tab)
        clicked_tab = await self._select_send_resources_tab(page)
        if not clicked_tab:
            return ActionResult(False, "impossibile trovare o cliccare il tab 'Invia risorse'")

        await sleep_human(0.8, 1.2)

        # 2.1 RISOLUZIONE E CONTROLLO DEI PARAMETRI OBBLIGATORI (Nome Villaggio + Coordinate X, Y)
        # L'agente non deve procedere se manca anche solo uno dei due riferimenti spaziali.
        tx = params.get("target_x")
        ty = params.get("target_y")
        if tx is None or ty is None:
            resolved_x, resolved_y = await self._resolve_village_coords_from_sidebar(page, str(target))
            if resolved_x is not None and resolved_y is not None:
                tx, ty = resolved_x, resolved_y
                logger.info(
                    "Coordinate per '%s' risolte dalla sidebar: (%d|%d)",
                    target, tx, ty
                )

        if tx is None or ty is None:
            logger.error(
                "Invio risorse bloccato: mancano le coordinate (X, Y) per il villaggio target '%s'. "
                "Entrambi i riferimenti spaziali (nome e coordinate) sono obbligatori.",
                target,
            )
            return ActionResult(
                False,
                f"riferimenti spaziali incompleti: coordinate (X, Y) mancanti per il target '{target}'",
            )

        # Verifica preliminare mercanti disponibili
        market_status = None
        for attempt in range(4):
            market_status = await page.evaluate(_JS_MARKET_QTY_INPUTS + """
                () => {
                    const bodyText = (document.body && document.body.innerText) || '';
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
            if market_status and market_status.get("inputsCount", 0) > 0:
                break
            # Ritenta il click sul tab se il form non si sblocca subito
            await self._select_send_resources_tab(page)
            await sleep_human(0.5, 0.9)

        if market_status and market_status.get("noMerchants"):
            m_info = market_status.get("merchants") or {}
            avail = m_info.get("available", 0)
            total = m_info.get("total", 1)
            logger.info("Mercato slot %s: nessun mercante disponibile (%d/%d in viaggio)", market_slot, avail, total)
            return ActionResult(
                False,
                f"nessun mercante disponibile al mercato ({avail}/{total} in viaggio)",
            )

        # Attesa del form operativo di invio
        has_form = False
        for attempt in range(6):
            has_form = await page.evaluate(_JS_MARKET_QTY_INPUTS + """
                () => {
                    const f = findSendForm();
                    if (!f) return false;
                    f.setAttribute('data-tts-sendform', '1');
                    return true;
                }
            """)
            if has_form:
                break
            if attempt >= 1:
                await self._select_send_resources_tab(page)
            await sleep_human(0.5, 0.9)

        if not has_form:
            current_url = page.url if page else "N/A"
            page_title = await page.title() if page else "N/A"
            logger.error("DEBUG MERCATO FALLITO -> URL corrente: %s | Titolo pagina: %s", current_url, page_title)
            try:
                if hasattr(self, '_dump_market_html'):
                    await self._dump_market_html(page)
            except Exception as e:
                logger.error("Errore durante il dump HTML del mercato: %s", e)
            return ActionResult(
                False,
                "form di invio mercato non caricato (tab 'Invia risorse' assente?)",
            )

        send_scope = "[data-tts-sendform='1']"
        village_name_before = await self._active_village_name(page)

        # 3. COMPILAZIONE CONTESTUALE: NOME VILLAGGIO TARGET & COORDINATE (X, Y)
        # Compilazione nome villaggio (esclude categoricamente campi di rinomina)
        target_filled = False
        for sel in selectors.MARKET_TARGET_INPUT:
            if any(bad in sel.lower() for bad in ["rename", "dname", "renamer"]):
                continue
            locator = page.locator(f"{send_scope} {sel}").first
            try:
                if await locator.count() == 0 or await locator.is_disabled():
                    continue
                # Mai compilare input dentro header/sidebar/liste villaggi:
                # ospitano il campo di rinomina del villaggio attivo.
                in_bad_container = await locator.evaluate(
                    "el => !!el.closest("
                    "'#sidebarBoxVillagelist, #sidebarBoxVillageList, "
                    ".villageList, .sidebar, header, .topBar, "
                    ".topNavigation, #villageNameField, .villageName')"
                )
                if in_bad_container:
                    continue
                name_attr = (await locator.get_attribute("name") or "").lower()
                if "dname" in name_attr or "rename" in name_attr:
                    continue
                await locator.fill("", timeout=500)
                await locator.press_sequentially(str(target), delay=40, timeout=1500)
                target_filled = True
                break
            except Exception:
                continue

        if not target_filled:
            target_filled = await page.evaluate(_JS_MARKET_QTY_INPUTS + """
                (targetName) => {
                    const inp = findTargetInput();
                    if (inp && !inp.disabled) {
                        const nameAttr = ((inp.name || '') + ' ' + (inp.id || '')).toLowerCase();
                        if (nameAttr.includes('dname') || nameAttr.includes('rename')) return false;

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

        # Clic sul suggerimento dell'autocomplete del mercato (se appare)
        await sleep_human(0.3, 0.6)
        await self._pick_target_suggestion(page, str(target))

        # Compilazione contestuale dei box coordinate dedicati (X, Y)
        coords_filled = await page.evaluate("""
            ([x, y]) => {
                const form = document.querySelector('[data-tts-sendform="1"]') || document;
                const ix = form.querySelector(
                    'input[name="x"], input#xCoord, input[id*="xCoord"], .coordinateX input, input.xCoord, input#x'
                ) || document.querySelector('input[name="x"], input#xCoord, input[id*="xCoord"]');
                const iy = form.querySelector(
                    'input[name="y"], input#yCoord, input[id*="yCoord"], .coordinateY input, input.yCoord, input#y'
                ) || document.querySelector('input[name="y"], input#yCoord, input[id*="yCoord"]');

                let filledAny = false;
                if (ix && !ix.disabled) {
                    ix.focus();
                    ix.value = String(x);
                    ix.dispatchEvent(new Event('input', {bubbles: true}));
                    ix.dispatchEvent(new Event('change', {bubbles: true}));
                    filledAny = true;
                }
                if (iy && !iy.disabled) {
                    iy.focus();
                    iy.value = String(y);
                    iy.dispatchEvent(new Event('input', {bubbles: true}));
                    iy.dispatchEvent(new Event('change', {bubbles: true}));
                    filledAny = true;
                }
                return filledAny;
            }
        """, [int(tx), int(ty)])

        if not target_filled and not coords_filled:
            return ActionResult(
                False,
                f"impossibile compilare ne' il nome villaggio ne' le coordinate per il target '{target}' ({tx}|{ty})",
            )

        if "build.php" not in (page.url or ""):
            return ActionResult(False, "selezione destinazione uscita dal mercato - invio annullato")

        # Guard antironima: se il fill del target ha alterato un campo di
        # rinomina fuori dal form mercato (header/sidebar/topbar), ripristina
        # il valore originale prima che l'autosave lo confermi.
        if village_name_before:
            try:
                renamed_fields = await page.evaluate(
                    """
                    (expected) => {
                        const fields = document.querySelectorAll(
                            '#sidebarBoxVillagelist input, #sidebarBoxVillageList input, ' +
                            '.villageList input, .sidebar input, header input, ' +
                            '.topBar input, .topNavigation input, #villageNameField, ' +
                            '.villageName input'
                        );
                        const changed = [];
                        for (const el of fields) {
                            const t = (el.type || 'text').toLowerCase();
                            if (t !== 'text' && t !== 'search' && t !== '') continue;
                            if (el.value && el.value.trim() !== expected.trim()) {
                                el.value = expected;
                                el.dispatchEvent(new Event('input', {bubbles: true}));
                                el.dispatchEvent(new Event('change', {bubbles: true}));
                                changed.push(el.name || el.id || '?');
                            }
                        }
                        return changed;
                    }
                    """,
                    village_name_before,
                )
                if renamed_fields:
                    logger.error(
                        "Campi rinomina villaggio alterati dal fill mercato: %s - valori ripristinati",
                        renamed_fields,
                    )
            except Exception:  # noqa: BLE001
                pass

        # 4. INSERIMENTO RISORSE CAPPED (Calcolo capienza mercanti e popolamento r1-r4)
        capped_resources = await self._calculate_capped_resources(page, resources_dict)
        if not capped_resources or sum(int(v or 0) for v in capped_resources.values()) <= 0:
            return ActionResult(
                False,
                "capacita' mercanti insufficiente per il carico richiesto",
            )

        filled = 0
        for rtype, sel_list in selectors.MARKET_SEND_INPUTS.items():
            amount = int(capped_resources.get(rtype, 0) or 0)
            if amount <= 0:
                continue
            # Prova prima con selettori scoped al form, poi a livello di pagina
            candidate_selectors = [f"{send_scope} {s}" for s in sel_list] + sel_list
            for sel in candidate_selectors:
                field = page.locator(sel).first
                try:
                    if await field.count() == 0 or await field.is_disabled():
                        continue
                    await field.fill(str(amount), timeout=1000)
                    filled += 1
                    break
                except Exception:
                    continue

        if filled == 0:
            filled = await page.evaluate("""
                (res) => {
                    let cnt = 0;
                    const map = { wood: 'lumber', clay: 'clay', iron: 'iron', crop: 'crop' };
                    const mapIdx = { wood: 1, clay: 2, iron: 3, crop: 4 };
                    for (const [k, v] of Object.entries(res)) {
                        const num = parseInt(v, 10);
                        if (!num || num <= 0) continue;
                        const resName = map[k];
                        const idx = mapIdx[k];
                        const selectors = [
                            `input[name="${resName}"]`,
                            `input#${resName}`,
                            `#r${idx}`,
                            `input[name="r${idx}"]`,
                            `input#r${idx}`,
                            `input[name="m2[${idx}]"]`,
                            `#m2_${idx}`,
                            `input[id*="r${idx}"]`,
                            `input[name*="r${idx}"]`
                        ];
                        let inp = null;
                        for (const s of selectors) {
                            inp = document.querySelector(s);
                            if (inp && !inp.disabled) break;
                        }
                        if (inp && !inp.disabled) {
                            inp.focus();
                            inp.value = num;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                            inp.dispatchEvent(new Event('blur', { bubbles: true }));
                            cnt++;
                        }
                    }
                    return cnt;
                }
            """, capped_resources)

        if filled == 0:
            # Dump diagnostico del DOM per capire perche' gli input non si compilano
            dump_note = ""
            try:
                dbg_dir = getattr(self.settings, "debug_dir", None)
                if dbg_dir:
                    dbg_dir.mkdir(parents=True, exist_ok=True)
                    dump_path = dbg_dir / "market_form_dump.html"
                    dump_path.write_text(await page.content(), encoding="utf-8")
                    dump_note = f" (dump DOM: {dump_path})"
            except Exception:  # noqa: BLE001
                pass
            return ActionResult(
                False,
                f"form di invio mercato non trovato o campi non compilabili "
                f"(mittente '{sender_name}' -> '{target}' ({tx}|{ty})){dump_note}",
            )

        await sleep_human(self.settings.action_delay_min, self.settings.action_delay_max)

        # 5. CONFERMA FINALE: Clic sul pulsante verde "Invia risorse" in fondo alla sezione
        send_buttons = [f"{send_scope} {s}" for s in selectors.MARKET_SEND_BUTTON] + selectors.MARKET_SEND_BUTTON
        clicked_send = await self._click_first(page, send_buttons)
        if not clicked_send:
            # Fallback JavaScript mirato al pulsante con testo 'Invia risorse' o 'Send resources'
            clicked_send = await page.evaluate("""
                () => {
                    const buttons = Array.from(document.querySelectorAll('button, input[type="submit"]'));
                    const btn = buttons.find(b => {
                        const t = ((b.innerText || b.value || '')).trim().toLowerCase();
                        return t.includes('invia risorse') || t.includes('send resources');
                    });
                    if (btn && !btn.disabled) {
                        btn.click();
                        return true;
                    }
                    return false;
                }
            """)
        if not clicked_send:
            return ActionResult(False, "pulsante di invio mercato non trovato")
        await sleep_human(1.0, 2.0)

        # Controllo di sicurezza antironima
        if village_name_before:
            village_name_after = await self._active_village_name(page)
            if village_name_after and village_name_after != village_name_before:
                logger.error(
                    "Tentativo di rinomina bloccato! Il villaggio attivo stava per essere cambiato da '%s' a '%s'",
                    village_name_before, village_name_after
                )
                return ActionResult(False, "blocco di sicurezza: impedita modifica nome villaggio")

        # Click di conferma riepilogo T4.x se richiesto dal gioco
        await self._click_first(page, selectors.MARKET_CONFIRM_BUTTON)
        await sleep_human(0.5, 1.0)

        logger.info(
            "Risorse inviate con successo a '%s' (%s|%s) dal mercato slot %s",
            target, tx, ty, market_slot
        )
        return ActionResult(True, f"risorse inviate con successo a '{target}' ({tx}|{ty}) tramite il mercato")

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
                // La sidebar villaggi usa le stesse classi .listEntry:
                // va esclusa per non confonderla con le righe farm list.
                const inSidebar = (n) => !!(n && n.closest && n.closest(
                    'aside, [id*="sidebar" i], [id*="villageList" i], ' +
                    '[id*="villagelist" i], .sidebar, .villageList, #myLinks'
                ));
                const nodes = [];
                const seen = new Set();
                for (const sel of rowsSelectors) {
                    document.querySelectorAll(sel).forEach(n => {
                        if (seen.has(n) || inSidebar(n)) return;
                        seen.add(n); nodes.push(n);
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
                // Fallback layout ignoto: partiamo dalle checkbox dell'area
                // contenuto e risaliamo al contenitore-riga piu' vicino.
                if (!row) {
                    const scope = document.querySelector(
                        '#raidList, .raidList, #raidListTable, ' +
                        'form[name*="farm" i], #content, #build'
                    ) || document.body;
                    const scopeBoxes = Array.from(
                        scope.querySelectorAll('input[type="checkbox"]')
                    ).filter(b => !inSidebar(b));
                    if (scopeBoxes.length) {
                        let cb = scopeBoxes[0];
                        if (wanted) {
                            const m = scopeBoxes.find(b => {
                                const c = b.closest(
                                    'tr, li, .listEntry, .slotRow, .row, div'
                                );
                                return c && (c.innerText || '')
                                    .toLowerCase().includes(wanted);
                            });
                            if (m) cb = m;
                        }
                        row = cb.closest(
                            'tr, li, .listEntry, .slotRow, .row'
                        ) || cb.parentElement;
                    }
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
                () => {
                    const scope = document.querySelector(
                        '#raidList, .raidList, #content, #build'
                    ) || document.body;
                    return {
                        url: location.href,
                        rows: document.querySelectorAll(
                            '.listEntry, tr.farmList, .farmList li'
                        ).length,
                        checkboxes: document.querySelectorAll(
                            'input[type="checkbox"]'
                        ).length,
                        checkboxesContent: scope.querySelectorAll(
                            'input[type="checkbox"]'
                        ).length,
                        raidList: !!document.querySelector(
                            '#raidList, .raidList, #raidListTable, ' +
                            'form[name*="farm" i]'
                        ),
                        text: ((scope && scope.innerText) || '')
                            .slice(0, 300),
                    };
                }
                """
            )
            # Feature non presente in pagina (nessun contenitore lista e
            # nessuna checkbox nell'area contenuto): le farm list su TL sono
            # Gold Club — messaggio distinto dal mismatch di selettori.
            if not debug.get("raidList") and not debug.get("checkboxesContent"):
                logger.warning(
                    "Nessuna farm list disponibile (slot %s): richiede Gold Club "
                    "o lista non ancora creata in-game. debug=%s",
                    rally_slot, debug,
                )
                return ActionResult(
                    False,
                    "nessuna farm list disponibile (Gold Club richiesto o "
                    "lista non creata in-game)",
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

        clicked = await self._click_first(
            page, selectors.FARM_LIST_SEND_BUTTON, timeout_ms=2500
        )
        if not clicked:
            # Fallback DOM-level: :has-text non copre input[submit] (label in
            # `value`) ne' controlli custom; si cerca nel contesto raid list.
            clicked = bool(
                await page.evaluate(
                    """
                    () => {
                        const KEY = /avvia|start|invia|send|lancia|launch|begin|inizia|raid/i;
                        const scope = document.querySelector(
                            '#raidList, .raidList, #raidListTable, ' +
                            'form[name*="farm" i], #content, #build'
                        ) || document.body;
                        const ctl = Array.from(scope.querySelectorAll(
                            'button, input[type="submit"], input[type="button"], ' +
                            'a[href], [role="button"], .button'
                        )).filter(b =>
                            !b.disabled && b.offsetParent !== null &&
                            !/tutte le liste|all lists|markAll/i.test(
                                (b.innerText || '') + ' ' + (b.value || '')
                            )
                        );
                        const pick = ctl.find(b => KEY.test(
                            ((b.innerText || '') + ' ' + (b.value || '') + ' ' +
                             (b.getAttribute('title') || '')).trim()
                        )) || ctl.find(b =>
                            b.tagName === 'BUTTON' || b.type === 'submit'
                        );
                        if (!pick) return false;
                        pick.click();
                        return true;
                    }
                    """
                )
            )
        if not clicked:
            try:
                dbg = await page.evaluate(
                    """
                    () => {
                        const scope = document.querySelector(
                            '#raidList, .raidList, #content, #build'
                        ) || document.body;
                        return {
                            url: location.href,
                            buttons: Array.from(scope.querySelectorAll(
                                'button, input[type="submit"], [role="button"]'
                            )).map(b => ({
                                tag: b.tagName, type: b.type || '',
                                text: (b.innerText || b.value || '')
                                    .trim().slice(0, 40),
                                disabled: !!b.disabled,
                                cls: (b.className || '').toString().slice(0, 60),
                            })).slice(0, 12),
                            tail: (scope.innerText || '').slice(-200),
                        };
                    }
                    """
                )
                logger.warning(
                    "Raid farm list: pulsante avvio non trovato. debug=%s", dbg
                )
            except Exception:
                pass
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
        # Tipo movimento: 4=saccheggio (default), 2=rinforzo (dodge), 3=attacco
        move_type = str(params.get("move_type") or "4")
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
                        const { x, y, troops, moveType } = payload;

                        // Coordinate X / Y
                        const xIn = document.querySelector('input[name="x"], input#xCoordInput, #xCoord');
                        const yIn = document.querySelector('input[name="y"], input#yCoordInput, #yCoord');
                        if (xIn) { xIn.value = String(x); xIn.dispatchEvent(new Event('input', {bubbles: true})); }
                        if (yIn) { yIn.value = String(y); yIn.dispatchEvent(new Event('input', {bubbles: true})); }

                        // Radiobutton tipo movimento: 2=Rinforzo, 3=Attacco, 4=Saccheggio
                        const raidRadio = document.querySelector(
                            `input[type="radio"][value="${moveType}"], ` +
                            `input[name="c"][value="${moveType}"], ` +
                            `label:has(input[value="${moveType}"]) input`
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
                    {"x": tx, "y": ty, "troops": troops, "moveType": move_type},
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

    async def _train_settlers(self, action: Action, village_id: int = 0) -> ActionResult:
        """Verifica e addestra i coloni mancanti (fino a 3) in Residenza/Palazzo.

        Naviga a build.php?id=<expansion_slot>, individua l'input di
        addestramento coloni della tribu' (t10 Romani, t20 Teutoni,
        t30 Galli), calcola quanti coloni mancano rispetto al target e
        compila + invia il form.
        """
        params = action.params or {}
        expansion_slot = int(params.get("expansion_slot") or params.get("trainer_slot") or 0)
        if expansion_slot <= 0:
            return ActionResult(False, "slot Residenza/Palazzo mancante (expansion_slot)")
        target_count = int(params.get("settlers_target") or 3)
        already = int(params.get("already_trained") or 0)

        page = self.browser.page
        assert page is not None

        url = f"{selectors.PATH_BUILD}?id={expansion_slot}"
        if village_id > 0:
            url += f"&newdid={village_id}"
        await self.browser.goto(url)
        await sleep_human(1.0, 1.5)

        # 1. Individua l'input coloni della tribu' e quelli in coda
        settlers_status = await page.evaluate(
            """
            () => {
                const inputs = Array.from(
                    document.querySelectorAll(
                        'input[name="t10"], input[name="t20"], input[name="t30"]'
                    )
                );
                const input = inputs.find(i => !i.disabled);
                // Coloni in coda di addestramento: cerca icone unit u10/u20/u30
                // nelle tabelle di progresso della pagina
                let inTraining = 0;
                document.querySelectorAll(
                    'img.unit, span.unit, i.unit, [class*="unit u"]'
                ).forEach(icon => {
                    const cls = icon.getAttribute('class') || '';
                    const m = cls.match(/u(10|20|30)\\b/);
                    if (!m) return;
                    const row = icon.closest('tr, li, .buildingRow, .troop') || icon.parentElement;
                    const txt = (row && row.innerText) || '';
                    const num = txt.match(/(\\d+)/);
                    if (num && /in (?:addestramento|training)|durata|duration|termina|finished/i.test(txt)) {
                        inTraining += parseInt(num[1], 10) || 0;
                    }
                });
                return {
                    hasInput: !!input,
                    inputName: input ? input.name : null,
                    inTraining: inTraining,
                };
            }
            """
        ) or {}

        if not settlers_status.get("hasInput"):
            return ActionResult(
                False,
                "impossibile addestrare coloni: slot errato o livello "
                "Residenza/Palazzo insufficiente",
            )

        # 2. Quanti coloni mancano al target (presenti + in coda contano)
        in_training = int(settlers_status.get("inTraining") or 0)
        missing = max(0, target_count - already - in_training)
        if missing <= 0:
            return ActionResult(
                True,
                f"coloni gia' sufficienti ({already} pronti, {in_training} in coda)",
            )

        # 3. Compila l'input coloni e invia il form
        input_name = settlers_status["inputName"]
        try:
            field = page.locator(f"input[name='{input_name}']").first
            await field.fill(str(missing), timeout=2000)
        except Exception as exc:  # noqa: BLE001
            return ActionResult(False, f"input coloni '{input_name}' non compilabile: {exc}")

        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )
        if await self._click_first(page, selectors.TRAIN_SUBMIT, timeout_ms=2000):
            await sleep_human(1.0, 2.0)
            logger.info(
                "Addestramento %d colono/i avviato nello slot %s (villaggio %s)",
                missing, expansion_slot, village_id,
            )
            return ActionResult(
                True,
                f"addestramento di {missing} colono/i avviato (slot {expansion_slot})",
            )

        return ActionResult(False, "errore invio form addestramento coloni")

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

        # Modalita' rimpiazzo mirato (Smart Troop Replenishment): quantita'
        # esplicite per unita' calcolate dal deficit target - (attuali+coda).
        unit_quantities = params.get("unit_quantities")
        if unit_quantities:
            return await self._train_troops_targeted(
                page,
                trainer_slots=trainer_slots,
                unit_quantities=dict(unit_quantities),
                village_id=village_id,
            )

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

    async def _train_troops_targeted(
        self,
        page,
        *,
        trainer_slots: list[int],
        unit_quantities: dict[str, int],
        village_id: int = 0,
        res_margin: float = 0.85,
        crop_margin: float = 0.70,
    ) -> ActionResult:
        """Addestra quantita' mirate per unita' (Smart Troop Replenishment).

        Per ogni edificio legge costi/risorse reali, ridimensiona ogni
        quantita' richiesta al massimo sostenibile (margini piu' prudenti
        del batch surplus: 85% risorse, 70% grano per non compromettere la
        stabilita' del grano), compila tutti gli input delle unita'
        richieste e invia il form UNA sola volta per edificio.
        """
        wanted = {
            str(k).lower(): int(v)
            for k, v in unit_quantities.items()
            if int(v or 0) > 0
        }
        if not wanted:
            return ActionResult(False, "nessuna unita' da addestrare nel piano")

        trained: list[str] = []
        remaining = dict(wanted)

        for trainer_slot in trainer_slots:
            if not remaining:
                break
            build_url = f"{selectors.PATH_BUILD}?id={trainer_slot}"
            if village_id > 0:
                build_url += f"&newdid={village_id}"
            await self.browser.goto(build_url)

            training_data = await self._extract_training_data(page)
            if not training_data:
                continue

            resources = training_data.get("resources") or {}
            budget = {
                "wood": int(resources.get("wood", 0) * res_margin),
                "clay": int(resources.get("clay", 0) * res_margin),
                "iron": int(resources.get("iron", 0) * res_margin),
                "crop": int(resources.get("crop", 0) * crop_margin),
            }

            # Per ogni unita' disponibile nel form: quantita' = min(richiesta,
            # budget/costo). Ogni input compilato aggiorna il budget residuo.
            fills: list[tuple[str, int, str]] = []
            for unit in training_data.get("units") or []:
                input_name = (unit.get("inputName") or "").strip().lower()
                label = (unit.get("label") or "").strip() or input_name
                costs = unit.get("costs") or {}
                need = remaining.get(input_name, 0)
                if need <= 0 or not costs:
                    continue
                affordable = need
                for key in ("wood", "clay", "iron", "crop"):
                    cost = int(costs.get(key) or 0)
                    if cost > 0:
                        affordable = min(affordable, budget[key] // cost)
                if affordable <= 0:
                    continue
                fills.append((input_name, int(affordable), label))
                for key in ("wood", "clay", "iron", "crop"):
                    cost = int(costs.get(key) or 0)
                    budget[key] -= cost * int(affordable)

            if not fills:
                continue

            filled_ok = False
            for input_name, qty, label in fills:
                try:
                    await page.locator(
                        f"input[name='{input_name}']"
                    ).first.fill(str(qty), timeout=2000)
                    remaining[input_name] = remaining.get(input_name, 0) - qty
                    if remaining[input_name] <= 0:
                        remaining.pop(input_name, None)
                    trained.append(f"{qty}x {label}")
                    filled_ok = True
                except Exception as exc:  # noqa: BLE001
                    logger.debug(
                        "Fill mirato fallito su %s (slot %s): %s",
                        input_name, trainer_slot, exc,
                    )

            if not filled_ok:
                continue

            await sleep_human(
                self.settings.action_delay_min,
                self.settings.action_delay_max,
            )
            if await self._click_first(page, selectors.TRAIN_SUBMIT, timeout_ms=2000):
                await sleep_human(1.0, 2.0)
                logger.info(
                    "Replenishment: addestramento avviato nello slot %s "
                    "(villaggio %s): %s",
                    trainer_slot, village_id, ", ".join(trained),
                )

        if trained:
            return ActionResult(
                True, f"rimpiazzo avviato: {', '.join(trained)}"
            )
        return ActionResult(
            False,
            "nessuna unita' del piano addestrabile con le risorse attuali",
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

    async def _active_village_name(self, page) -> str | None:
        """Nome del villaggio attivo letto dalla sidebar/testata."""
        for sel in selectors.ACTIVE_VILLAGE_NAME:
            loc = page.locator(sel).first
            try:
                if await loc.count() == 0:
                    continue
                tag = await loc.evaluate("el => el.tagName")
                if tag == "INPUT":
                    val = await loc.input_value(timeout=500)
                else:
                    val = await loc.inner_text(timeout=500)
                val = (val or "").strip()
                if val:
                    return val
            except Exception:  # noqa: BLE001
                continue
        return None

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

    # ------------------------------------------------------------------
    # Task giornaliere / questmaster: riscossione premi per villaggio
    # ------------------------------------------------------------------
    async def claim_village_tasks(
        self, village_id: int = 0, village_name: str = ""
    ) -> int:
        """Apre il pannello task del villaggio attivo e riscuote i premi.

        Flusso:
        1. click sull'icona questmaster (#questmasterButton) se presente
           e riscossione premi nel dialog;
        2. pagine dedicate UI moderna `/tasks?t=village` e
           `/tasks?t=general`, poi legacy `questmaster.php`: in ognuna si
           cliccano i pulsanti verdi 'Raccogli'/'Collect'/'Claim' finche'
           non ne restano;
        3. bottoni disabilitati (magazzino pieno) vengono saltati senza
           errore: si riprova al ciclo successivo;
        4. chiusura del dialog.

        Ritorna il numero di premi effettivamente riscossi.
        """
        if village_id > 0:
            await self._ensure_village(village_id)

        page = self.browser.page
        assert page is not None

        claimed = 0
        skipped = 0

        # 1. Widget task (icona questmaster), se il server la espone
        opened = await self._click_first(
            page, selectors.QUEST_MASTER_BUTTON, timeout_ms=2000
        )
        if opened:
            await sleep_human(1.0, 1.8)
            for _ in range(8):
                n, s = await self._claim_task_buttons(page)
                claimed += n
                skipped = max(skipped, s)
                if n == 0:
                    break
                await sleep_human(0.5, 0.9)

        # 2. Pagine task dedicate (UI moderna) + legacy questmaster.php
        for path in (
            selectors.PATH_TASKS_VILLAGE,
            selectors.PATH_TASKS_GENERAL,
            selectors.PATH_QUESTMASTER,
        ):
            try:
                await self.browser.goto(path)
                await sleep_human(0.8, 1.2)
            except Exception:  # noqa: BLE001 — pagina assente su questo server
                continue
            for _ in range(8):
                n, s = await self._claim_task_buttons(page)
                claimed += n
                skipped = max(skipped, s)
                if n == 0:
                    break
                await sleep_human(0.5, 0.9)

        # 3. Chiudi l'eventuale dialog rimasto aperto
        if opened:
            await self._click_first(
                page, selectors.QUEST_DIALOG_CLOSE, timeout_ms=1200
            )
            await sleep_human(0.3, 0.6)

        if claimed:
            logger.info(
                "Task villaggio '%s': %d premi riscossi",
                village_name or village_id or "attivo",
                claimed,
            )
        else:
            logger.info(
                "Task villaggio '%s': check eseguito, nessun premio da riscuotere",
                village_name or village_id or "attivo",
            )
        if skipped:
            logger.info(
                "Task villaggio '%s': %d premi non riscossi — pulsante "
                "disabilitato (magazzino pieno): retry al prossimo ciclo",
                village_name or village_id or "attivo",
                skipped,
            )
        return claimed

    async def _claim_task_buttons(self, page) -> tuple[int, int]:
        """Clicca un pulsante premio verde cliccabile nella finestra task.

        Cerca in #tasks/.tasks (UI moderna) e nei contenitori legacy i
        bottoni con testo 'Raccogli'/'Collect'/'Claim' (case-insensitive)
        e ne clicca il primo abilitato; i premi possono sbloccare task a
        catena, quindi va richiamato in loop finche' non ne restano.

        Ritorna (clicked, skipped): clicked = 1 se un pulsante e' stato
        cliccato; skipped = n. di pulsanti premio trovati ma disabilitati
        (tooltip magazzino pieno) — da saltare senza errore.
        """
        res = await page.evaluate(
            """
            () => {
                const claimRx = /(raccogl|riscuot|collect|claim|récupér|belohnung)/i;
                const skipRx = /(chiudi|close|cancel|annulla|abbrechen)/i;
                const scopes = ['#tasks', '.tasks', '#dialogContent',
                                '.quest', '.task', '#content', 'body'];
                let els = [];
                for (const scope of scopes) {
                    for (const root of document.querySelectorAll(scope)) {
                        els.push(...root.querySelectorAll(
                            'button, a, input[type="submit"], input[type="button"]'
                        ));
                    }
                }
                let disabledFound = 0;
                for (const el of els) {
                    const text = (el.innerText || el.value || el.textContent || '').trim();
                    if (!text || !claimRx.test(text) || skipRx.test(text)) continue;
                    const cls = (el.className || '').toLowerCase();
                    const disabled = el.disabled ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        cls.includes('disabled') || cls.includes('inactive') ||
                        cls.includes('grey') || cls.includes('gray');
                    if (disabled) { disabledFound++; continue; }
                    if (el.offsetParent === null && el.offsetWidth === 0) continue;
                    el.click();
                    return { clicked: true, disabled: disabledFound };
                }
                return { clicked: false, disabled: disabledFound };
            }
            """
        )
        clicked = 1 if isinstance(res, dict) and res.get("clicked") else 0
        skipped = int(res.get("disabled", 0)) if isinstance(res, dict) else 0
        if clicked:
            await sleep_human(0.5, 0.9)
        return clicked, skipped