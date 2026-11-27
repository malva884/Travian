"""Esecutore: traduce un'Action in click/navigazioni sulla pagina."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from . import builder, selectors
from .browser import TravianBrowser, human_click, sleep_human
from .config import Settings
from .models import (
    Action,
    ActionResult,
    ActionType,
    STATIC_TROOP_LABELS,
)
from .radar_store import (
    CAT_MERCHANT,
    CAT_OUTGOING_ATTACK,
    CAT_OUTGOING_RAID,
    radar_store,
)
from .server_profile import ServerProfile, profile_for_url
from .stealth import resource_eta_seconds
from .telemetry_capture import capture_anomaly
from .war import map_distance, travel_seconds

logger = logging.getLogger(__name__)

_NEWDID = re.compile(r"newdid=(\d+)")


def _is_building_url(url: str | None) -> bool:
    """True se l'URL punta a una pagina edificio su qualunque routing Travian.

    Copre il legacy ``build.php?id=N`` e i routing moderni di Travian
    Legends (``/building?id=N`` query-string, ``/build/N`` path-style,
    ``/slot?id=N`` nuova UI): usata dai guard 'siamo ancora
    nell'edificio?' per non abortire falsamente sui server moderni.
    """
    u = (url or "").lower()
    return any(
        tok in u for tok in ("build.php", "/building", "/build/", "/slot")
    )


def _slot_url_check(slot_id: int) -> re.Pattern:
    """Regex 'pagina dello slot N aperta' su tutti i routing Travian."""
    return re.compile(
        rf"(?:(?:/slot|/building|build\.php)\?[^#]*\bid={int(slot_id)}\b"
        rf"|/build/{int(slot_id)}\b)"
    )


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
        # Profilo per-server auto-calibrato (state/server_profile_<host>.json):
        # gid degli slot vuoti, routing_mode, selettori promossi. Caricato
        # lazy alla prima azione: niente accesso FS a costruzione (test).
        self._profile: ServerProfile | None = None

    @property
    def profile(self) -> ServerProfile:
        """Adaptive Server Profile dell'host corrente (lazy)."""
        if self._profile is None:
            try:
                self._profile = profile_for_url(self.settings.server_url)
            except Exception:  # noqa: BLE001
                self._profile = ServerProfile(host="unknown")
        return self._profile

    async def _capture(
        self,
        page,
        error_code: str,
        *,
        village_id: int | str = 0,
        slot_id: int | None = None,
        expected_gid: int | None = None,
        detected_gid: int | None = None,
        extra: dict | None = None,
    ) -> None:
        """Cattura best-effort dell'HTML anomalo in tests/fixtures/captured/.

        Mai bloccante: deduplicata 6h per <code>:<slot|gid> e
        sanitizzata da telemetry_capture.
        """
        try:
            await capture_anomaly(
                page,
                error_code,
                village_id=village_id,
                slot_id=slot_id,
                expected_gid=expected_gid,
                detected_gid=detected_gid,
                profile=self.profile,
                extra=extra,
            )
        except Exception:  # noqa: BLE001
            pass

    async def _has_new_building_ui(self, page) -> bool:
        """True se la pagina mostra l'UI 'nuova costruzione' (slot vuoto).

        Marker: tab di categoria (category=1/2/3), blocchi contratto
        #contract_building*, wrapper .contractNew/.newBuilding — la
        pagina di uno slot OCCUPATO espone invece il dettaglio
        dell'edificio esistente.
        """
        try:
            return bool(
                await page.evaluate(
                    """
                    () => !!document.querySelector(
                        'a[href*="category="], .tabItem[href*="category="], ' +
                        '[id^="contract_building"], .buildingWrapper, ' +
                        '.contractNew, .newBuilding, button.newBuilding, ' +
                        '.contract .contractLink'
                    )
                    """
                )
            )
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    async def _is_upgrade_blocked(block) -> bool:
        """True se il blocco contratto mostra un blocco temporaneo.

        Nuova UI: quando la costruzione e' sospesa (coda interna piena
        senza Plus, requisiti/risorse non ancora pronti) dentro
        #contract_building{gid} NON esiste <button class="green build">
        ma compare <div class="upgradeBlocked"> insieme ai div
        cliccabili .inlineIcon.resource.transfer (modale trasferimento
        risorse eroe). Quei div non devono essere intercettati dal
        fallback di ricerca pulsante.

        Regressione ts30 (Europa 30): la classe .upgradeBlocked compare
        ANCHE su pulsanti secondari Plus ("Costruisci con il capo
        cantiere" / video-riduzione) o su elementi wrapper mentre il
        pulsante primario "Costruisci edifici" (button.green.new) e'
        verde e cliccabile. Il verdetto lo da' SOLO il primario: se
        esiste e non e' disabled/.upgradeBlocked, il blocco e' attivo.
        """
        try:
            primary = block.locator(
                "button.green.new, button.textButtonV1.green.new"
            ).first
            if await primary.count() > 0:
                try:
                    if await primary.is_disabled():
                        return True
                except Exception:  # noqa: BLE001
                    pass
                css = (await primary.get_attribute("class")) or ""
                return "disabled" in css or "upgradeBlocked" in css
            # Nessun pulsante primario: comportamento storico — il blocco
            # e' attivo se nel contratto compare .upgradeBlocked.
            return await block.locator(".upgradeBlocked").count() > 0
        except Exception:  # noqa: BLE001
            return False

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
            case ActionType.UPGRADE_UNITS:
                return await self._upgrade_smithy_units(action, village_id=vid_int)
            case ActionType.RESEARCH_UNIT:
                return await self._research_unit(action, village_id=vid_int)
            case ActionType.START_CELEBRATION:
                return await self._start_celebration(action, village_id=vid_int)
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

    async def _read_contract_cost(self, page) -> dict:
        """Costo {wood,clay,iron,crop} del contratto mostrato ({} = non letto)."""
        try:
            cost = await page.evaluate(
                """
                () => {
                    const out = {};
                    for (const el of document.querySelectorAll(
                        '#contract .resource, #build .resource, ' +
                        '.contract .resource, .showCosts .resource, ' +
                        '#contract [class*="resource"]'
                    )) {
                        const cls = el.className || '';
                        const t = (el.innerText || el.textContent || '')
                            .replace(/[^0-9]/g, '');
                        const n = parseInt(t || '0', 10) || 0;
                        if (/r1|wood|lumber|legno/i.test(cls)) out.wood = n;
                        else if (/r2|clay|argilla/i.test(cls)) out.clay = n;
                        else if (/r3|iron|ferro/i.test(cls)) out.iron = n;
                        else if (/r4|crop|grano|wheat/i.test(cls)) out.crop = n;
                    }
                    return out;
                }
                """
            )
            return cost if isinstance(cost, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    async def _build_queue_count(self, page) -> int | None:
        """Numero di elementi nella coda di costruzione in sidebar.

        Copre markup vecchia e nuova UI; ritorna None se nessun container
        di coda e' riconoscibile nella pagina corrente.
        """
        try:
            count = await page.evaluate(
                """
                () => {
                    // Solo blocchi reali della coda di costruzione:
                    // [class*="construction" i] escluso — matchava
                    // banner/pannelli non legati alla coda (falsi
                    // positivi su queue_before/queue_after).
                    const containers = document.querySelectorAll(
                        '#buildingContract, #building_contract, ' +
                        '.buildingList, .buildingsOverview, ' +
                        '#sidebarBoxBuilding, #sidebarBoxBuildingQueue, ' +
                        '[data-testid*="build-queue" i], ' +
                        '[class*="buildingList" i], ' +
                        '[id*="buildQueue" i], [id*="buildingQueue" i], ' +
                        '[class*="buildQueue" i], [class*="buildingQueue" i]'
                    );
                    if (!containers.length) return null;
                    // Solo righe che rappresentano un lavoro di
                    // costruzione/ampliamento — niente li/tr generici,
                    // intestazioni, separatori, pulsanti NPC/capo
                    // cantiere, voci vuote o invisibili.
                    const rowSel =
                        '.buildingRow, .slotRow, li[data-gid], ' +
                        'tr[data-gid], [class*="queueElement" i], ' +
                        '[class*="buildQueue" i] > li, ' +
                        '[class*="buildQueue" i] > div[data-gid]';
                    // Righe fittizie: slot libero e CTA premium (Gold,
                    // Plus, NPC, capo cantiere, instant, video,
                    // exchange) — non sono lavori in coda.
                    const fakeRe = new RegExp(
                        'slot libero|free slot|npc|capo cantiere|' +
                        'instant|video|exchange|gold|plus', 'i'
                    );
                    const isVisible = (el) => {
                        // offsetParent === null -> display:none o fuori
                        // layout; pos:fixed e' l'unica eccezione legit.
                        const st = getComputedStyle(el);
                        if (st.display === 'none' ||
                            st.visibility === 'hidden') return false;
                        if (el.offsetParent === null &&
                            st.position !== 'fixed') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const rowText = (el) => (
                        (el.innerText || '') + ' ' +
                        (el.getAttribute('title') || '') + ' ' +
                        (el.getAttribute('aria-label') || '')
                    ).trim();
                    const isJobRow = (el) => {
                        if (!isVisible(el)) return false;
                        if (el.closest('thead')) return false;
                        if (el.matches(
                            'button, th, [class*="btn" i], ' +
                            '[class*="header" i], [class*="separator" i], ' +
                            '[class*="npc" i], [class*="foreman" i], ' +
                            '[class*="empty" i], a[href*="npc" i]'
                        )) return false;
                        const txt = rowText(el);
                        if (!txt) return false;
                        // Una riga di lavoro ATTIVO incorpora spesso la
                        // CTA premium 'completa ora' (capo cantiere /
                        // gold / instant): fakeRe sul testo intero la
                        // scartava come finta e queue_after restava 0
                        // con cantiere in corso (fixture 38553).
                        // Evidenza positiva di job = countdown o
                        // data-gid: ha priorita' sul testo premium.
                        const hasJobMark = el.hasAttribute('data-gid') ||
                            !!el.querySelector(
                                '.timer, .dur, .clocks, ' +
                                '[class*="timer" i], ' +
                                '[class*="countdown" i]'
                            );
                        if (!hasJobMark && fakeRe.test(txt)) return false;
                        return true;
                    };
                    // Dedup container annidati: si tengono solo i piu'
                    // esterni (un box dentro un box conta una volta).
                    const topContainers = Array.from(containers).filter(
                        c => !Array.from(containers).some(
                            o => o !== c && o.contains(c)
                        )
                    );
                    let best = 0;
                    for (const c of topContainers) {
                        // Dedup righe annidate: match di qualsiasi
                        // selettore dentro una riga matchata non
                        // ri-conta — si tiene il nodo piu' esterno.
                        const nodes = Array.from(
                            c.querySelectorAll(rowSel)
                        ).filter(isJobRow).filter(
                            (el, _i, arr) => !arr.some(
                                o => o !== el && o.contains(el)
                            )
                        );
                        // Dedup semantica: stesso job ripetuto in DOM
                        // paralleli del container (gid + testo, che
                        // include il countdown → job doppi col medesimo
                        // gid restano distinti).
                        const seen = new Set();
                        let n = 0;
                        for (const el of nodes) {
                            const key =
                                (el.getAttribute('data-gid') || '') +
                                '|' + rowText(el).slice(0, 80);
                            if (seen.has(key)) continue;
                            seen.add(key);
                            n++;
                        }
                        if (n > best) best = n;
                    }
                    return best;
                }
                """
            )
            if count is None:
                return None
            count = int(count)
            # Tetto fisico del gioco: senza Plus (allow_parallel_build
            # False) max 2 lavori in coda, con Plus 3 — coerente con
            # max_parallel di builder.queue_slots_free. Oltre e' rumore
            # di parsing (righe fittizie/annidate sfuggite ai filtri):
            # clamp per non falsare il confronto queue_before/after.
            cap = (
                3
                if getattr(self.settings, "allow_parallel_build", False)
                else 2
            )
            if count > cap:
                logger.warning(
                    "_build_queue_count: %d righe > tetto fisico %d "
                    "(clamp) — markup coda sospetto",
                    count,
                    cap,
                )
                count = cap
            return count
        except Exception:  # noqa: BLE001
            return None

    async def _detect_page_gid(self, page) -> int | None:
        try:
            return await page.evaluate(
                """
                () => {
                    const grab = (el) => {
                        if (!el) return null;
                        const m = (el.className || '').match(/\\bgid(\\d+)\\b|\\bg(\\d+)\\b/);
                        return m ? parseInt(m[1] || m[2], 10) : null;
                    };
                    for (const el of [
                        document.getElementById('build'),
                        document.getElementById('contract'),
                        document.querySelector('.contractLink'),
                        document.querySelector('#content .building, #content .buildingImage'),
                    ]) {
                        const g = grab(el);
                        if (g !== null) return g;
                    }
                    return null;
                }
                """
            )
        except Exception:
            return None

    async def _upgrade_field(self, action: Action, village_id: int = 0) -> ActionResult:
        """Esegue costruzione iniziale (Flusso A) o ampliamento (Flusso B).

        Flusso A (current_level == 0, gid > 0): navigazione al tab di categoria su
        build.php?id=<slot>&category=<cat>, individuazione blocco #contract_building<gid>,
        click sul pulsante primario (button.green.new) e post-check su .buildingList.

        Flusso B (current_level >= 1, gid == 0): navigazione allo slot, click su
        upgradeButtonsContainer button.green.build e post-check su .buildingList.
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

        _res_keys = ("wood", "clay", "iron", "crop")
        stock = {
            k: int((action.params.get("stock") or {}).get(k) or 0)
            for k in _res_keys
        }
        prod_h = dict(action.params.get("prod_h") or {})
        blocker_cost: dict | None = None
        blocker_eta: float | None = None
        last_cost: dict = {}
        # Primo fallimento strutturale incontrato (codice, slot, gid):
        # propagato nel risultato finale se nessun candidato viene
        # avviato — serve a record_upgrade_fail per marcare lo step
        # della Coda Prioritaria come bloccato (spec anti-starvation).
        first_structural: dict | None = None
        # Blocco temporaneo (.upgradeBlocked): propagato nel data solo
        # in assenza di errori strutturali — codice non strutturale,
        # il circuit-breaker dei fail lo ignora di proposito.
        blocked_err: dict | None = None

        for cand_idx, slot_id in enumerate(candidates):
            is_head = cand_idx == 0
            gid = construct.get(int(slot_id), 0)

            if gid > 0:
                # =============================================================
                # FLUSSO A: Costruzione iniziale (current_level == 0)
                # =============================================================
                cat_id = builder.building_category(gid)
                # Routing adattivo: il profilo del server decide se
                # aprire prima il path moderno (/slot?id=N) o il
                # legacy build.php?id=N; il secondo resta di fallback.
                if self.profile.routing_mode == "legacy":
                    build_url = (
                        f"{selectors.PATH_BUILD}?id={int(slot_id)}"
                        f"&category={cat_id}"
                    )
                    alt_url = f"slot?id={int(slot_id)}&category={cat_id}"
                else:
                    build_url = f"slot?id={int(slot_id)}&category={cat_id}"
                    alt_url = (
                        f"{selectors.PATH_BUILD}?id={int(slot_id)}"
                        f"&category={cat_id}"
                    )
                if village_id > 0:
                    build_url += f"&newdid={village_id}"
                    alt_url += f"&newdid={village_id}"
                await self.browser.goto(build_url)

                slot_check = _slot_url_check(slot_id)
                if not slot_check.search(page.url or ""):
                    await self.browser.goto(alt_url)
                if not slot_check.search(page.url or ""):
                    logger.debug(
                        "Slot %s: vista costruzione non aperta (url=%s)",
                        slot_id,
                        page.url,
                    )
                    continue

                # Individuare il blocco #contract_building{gid}
                contract_block = page.locator(
                    f"#contract_building{gid}, .buildingWrapper.gid{gid}"
                ).first
                block_count = 0
                try:
                    block_count = await contract_block.count()
                except Exception:
                    block_count = 0

                if block_count == 0:
                    # Il blocco .buildingWrapper.gid{N} esiste solo nel tab
                    # categoria giusto. Su alcuni server il parametro
                    # category= nella URL viene ignorato/riscritto (la
                    # pagina slot apre il tab di default, es. Risorse):
                    # 1) click mirato sul tab della categoria mappata,
                    #    poi delle altre (fallback cataloghi/skin);
                    # 2) enumerazione generica dei tab della pagina slot
                    #    per markup che non espone href 'category='.
                    if "category=" not in (page.url or ""):
                        logger.debug(
                            "Slot %s: parametro category assente in %s, "
                            "seleziono il tab via DOM",
                            slot_id,
                            page.url,
                        )
                    for cat_try in [cat_id] + [
                        c for c in (1, 2, 3) if c != cat_id
                    ]:
                        tab_loc = page.locator(
                            f".contentNavi a[href*='category={cat_try}'], "
                            f".tabItem[data-tab-id='{cat_try}'], "
                            f".tabItem[data-category='{cat_try}'], "
                            f".tabItem a[href*='category={cat_try}'], "
                            f"[role='tab'][href*='category={cat_try}'], "
                            f"button[onclick*='category={cat_try}'], "
                            f"a[href*='category={cat_try}']"
                        ).first
                        try:
                            if await tab_loc.count() == 0:
                                continue
                            await human_click(page, tab_loc)
                            await sleep_human(0.8, 1.5)
                            block_count = await contract_block.count()
                        except Exception:
                            continue
                        if block_count > 0:
                            break

                    if block_count == 0:
                        # Fallback generico: i tab di categoria nella
                        # nuova UI possono essere JS-driven senza href
                        # 'category=' (tabItem/[role=tab]/nav). Clicca
                        # ogni elemento tab-like nel content e ricontrolla.
                        tabs = page.locator(
                            "#content .tabItem, #content nav a, "
                            "#content .tabs a, #content [role='tab'], "
                            ".contentNavi .tabItem, .contentNavi a"
                        )
                        try:
                            n_tabs = min(await tabs.count(), 8)
                        except Exception:
                            n_tabs = 0
                        for ti in range(n_tabs):
                            t = tabs.nth(ti)
                            try:
                                if not await t.is_visible():
                                    continue
                                await human_click(page, t)
                                await sleep_human(0.8, 1.5)
                                block_count = await contract_block.count()
                            except Exception:
                                continue
                            if block_count > 0:
                                break
                            # Un tab puo' essere un link di navigazione
                            # fuori dalla pagina slot: torna indietro
                            # prima di provare il successivo.
                            if not slot_check.search(page.url or ""):
                                await self.browser.goto(build_url)

                if block_count == 0:
                    # Blocco del gid assente anche dopo il tab: la
                    # pagina mostra il contratto singolo dell'edificio
                    # che occupa lo slot (slot non vuoto) oppure il gid
                    # non e' in elenco (prerequisiti/categoria errata).
                    page_gid = await self._detect_page_gid(page)
                    # Self-healing gid=0: sui server che marcano lo slot
                    # libero con g0/gid0, l'UI di 'nuova costruzione'
                    # (tab categoria/bottoni contract) certifica il vuoto
                    # -> il profilo apprende gid=0 come marker di slot
                    # libero e la costruzione prosegue senza abortire.
                    slot_empty = self.profile.is_empty_gid(page_gid)
                    if (
                        not slot_empty
                        and page_gid == 0
                        and await self._has_new_building_ui(page)
                    ):
                        if self.profile.calibrate_empty_gid(0):
                            logger.warning(
                                "[SELF-HEAL] Calibrato gid=0 come slot "
                                "libero per %s",
                                self.profile.host,
                            )
                        slot_empty = True
                    if (
                        not slot_empty
                        and page_gid is not None
                        and page_gid != gid
                    ):
                        logger.warning(
                            "Slot %s occupato da gid=%s (atteso vuoto per gid=%s): INVALID_SLOT_OCCUPIED",
                            slot_id,
                            page_gid,
                            gid,
                        )
                        await self._capture(
                            page,
                            "INVALID_SLOT_OCCUPIED",
                            village_id=village_id,
                            slot_id=int(slot_id),
                            expected_gid=gid,
                            detected_gid=page_gid,
                        )
                        _err = {
                            "error": "INVALID_SLOT_OCCUPIED",
                            "slot_id": int(slot_id),
                            "gid": int(gid),
                        }
                        if is_head:
                            # Testa prioritaria: niente spesa sui
                            # candidati secondari — blocco esplicito.
                            return ActionResult(
                                False,
                                f"candidato prioritario: slot {slot_id} "
                                f"occupato da gid={page_gid}",
                                data=_err,
                            )
                        first_structural = first_structural or _err
                        continue
                    try:
                        prereq_missing = await page.evaluate(
                            """
                            (gid) => {
                                const body = document.body ? document.body.innerText : '';
                                return /prerequisit|voraussetzung|prerequisite/i.test(body);
                            }
                            """,
                            gid,
                        )
                    except Exception:
                        prereq_missing = False
                    err = "PREREQUISITES_MISSING" if prereq_missing else "BTN_NOT_FOUND"
                    logger.warning(
                        "Contratto building gid=%s non trovato su slot %s: %s",
                        gid,
                        slot_id,
                        err,
                    )
                    await self._capture(
                        page,
                        err,
                        village_id=village_id,
                        slot_id=int(slot_id),
                        expected_gid=gid,
                        detected_gid=page_gid,
                    )
                    _err = {
                        "error": err,
                        "slot_id": int(slot_id),
                        "gid": int(gid),
                    }
                    if is_head:
                        return ActionResult(
                            False,
                            f"candidato prioritario: contratto gid={gid} "
                            f"non trovato su slot {slot_id} ({err})",
                            data=_err,
                        )
                    first_structural = first_structural or _err
                    continue

                # Blocco temporaneo della nuova UI: #contract_building{gid}
                # mostra .upgradeBlocked (coda interna occupata senza
                # Plus, requisiti/risorse non pronti) al posto del
                # pulsante build; i div .inlineIcon.resource.transfer
                # presenti aprono il modale eroe e non devono essere
                # intercettati come CTA. Skip pulito: niente fallback
                # pulsante, niente QUEUE_UNCONFIRMED, codice NON
                # strutturale -> record_upgrade_fail lo ignora.
                if await self._is_upgrade_blocked(contract_block):
                    logger.info(
                        "Costruzione gid=%s su slot %s temporaneamente "
                        "bloccata (.upgradeBlocked): skip candidato",
                        gid,
                        slot_id,
                    )
                    blocked_err = blocked_err or {
                        "error": "UPGRADE_BLOCKED",
                        "slot_id": int(slot_id),
                        "gid": int(gid),
                    }
                    continue

                button = None
                new_btn_selectors = [
                    f"#contract_building{gid} button.green.new",
                    f"#contract_building{gid} button.textButtonV1.green",
                    f"#contract_building{gid} button.green",
                    f"#contract_building{gid} .contractLink button.green",
                    f".buildingWrapper.gid{gid} button.green.new",
                    f".buildingWrapper.gid{gid} button.textButtonV1.green",
                    f".buildingWrapper.gid{gid} button.green",
                    f".buildingWrapper.gid{gid} .contractLink button.green",
                    f"#contract_building{gid} button[type='button'].green",
                    # NIENTE fallback 'button' generico (regressione
                    # ts30: cliccava il primo button del contratto —
                    # Plus 'capo cantiere'/NPC/video — click a vuoto e
                    # QUEUE_UNCONFIRMED). Skin senza classe 'green':
                    # fallback filtrato JS piu' sotto; restano i link
                    # di riga contractLink.
                    f"#contract_building{gid} a.contractLink",
                    f".buildingWrapper.gid{gid} a.contractLink",
                ]
                # CTA da escludere nella scelta del pulsante primario:
                # i secondari Plus/NPC possono condividere 'green'.
                _bad_cta_classes = (
                    "disabled", "gold", "npc", "video", "instant",
                    "exchange", "upgradeblocked",
                )
                _bad_cta_texts = (
                    "npc", "video", "istantane", "instant",
                    "capo cantiere", "exchange",
                )
                for _idx, sel in enumerate(
                    self.profile.ordered_selectors(
                        "new_building_button", new_btn_selectors
                    )
                ):
                    loc = page.locator(sel).first
                    try:
                        if await loc.count() == 0:
                            continue
                        if await loc.is_disabled():
                            continue
                        css = (
                            (await loc.get_attribute("class")) or ""
                        ).lower()
                        if any(t in css for t in _bad_cta_classes):
                            continue
                        # Filtro testo solo sui <button> (i contractLink
                        # contengono anche i testi dei pulsanti interni).
                        if "contractLink" not in sel:
                            try:
                                txt = (
                                    (await loc.inner_text()) or ""
                                ).lower()
                            except Exception:
                                txt = ""
                            if any(t in txt for t in _bad_cta_texts):
                                continue
                        button = loc
                        if _idx > 0:
                            self.profile.promote_selector(
                                "new_building_button", sel
                            )
                        break
                    except Exception:
                        continue

                if button is None:
                    # Fallback filtrato (ticket ts30): valido solo un
                    # button il cui onclick/testo punta alla costruzione
                    # di questo gid — esclusi npc, video, instant,
                    # exchange e i pulsanti Plus 'capo cantiere'.
                    try:
                        marked = await page.evaluate(
                            """
                            (gid) => {
                                const scope = document.querySelector(
                                    '#contract_building' + gid
                                ) || document.querySelector(
                                    '.buildingWrapper.gid' + gid
                                );
                                if (!scope) return false;
                                const badRe = /npc|video|instant|exchange|capo cantiere|gold|upgradeBlocked/i;
                                const txtRe = /build|costruisci|upgrade|migliora|bauen|ausbau/i;
                                for (const b of scope.querySelectorAll('button')) {
                                    const cls = b.className || '';
                                    const oc = b.getAttribute('onclick') || '';
                                    const txt = (b.innerText || '').trim();
                                    if (b.disabled || /disabled/i.test(cls)) continue;
                                    if (badRe.test(cls) || badRe.test(txt)) continue;
                                    const ok = new RegExp(
                                        'a=' + gid + '(?!\\d)'
                                    ).test(oc) ||
                                        /build|dorf|village|slot/i.test(oc) ||
                                        txtRe.test(txt);
                                    if (!ok) continue;
                                    b.setAttribute('data-cascade-cta', '1');
                                    return true;
                                }
                                return false;
                            }
                            """,
                            gid,
                        )
                    except Exception:
                        marked = False
                    if marked:
                        loc = page.locator(
                            f"#contract_building{gid} [data-cascade-cta='1'], "
                            f".buildingWrapper.gid{gid} [data-cascade-cta='1']"
                        ).first
                        try:
                            if await loc.count() > 0:
                                button = loc
                        except Exception:
                            pass

                cost = await self._read_contract_cost(page)
                if cost:
                    last_cost = cost

                if button is None:
                    try:
                        is_none = await page.evaluate(
                            """
                            (gid) => {
                                const el = document.querySelector('#contract_building' + gid + ' .none') ||
                                           document.querySelector('.buildingWrapper.gid' + gid + ' .none');
                                return !!el;
                            }
                            """,
                            gid,
                        )
                    except Exception:
                        is_none = False
                    err = "INSUFFICIENT_RESOURCES" if is_none else "BTN_NOT_FOUND"
                    logger.warning(
                        "Pulsante costruzione non disponibile per gid=%s su slot %s: %s",
                        gid,
                        slot_id,
                        err,
                    )
                    if err == "BTN_NOT_FOUND":
                        await self._capture(
                            page,
                            err,
                            village_id=village_id,
                            slot_id=int(slot_id),
                            expected_gid=gid,
                        )
                        _err = {
                            "error": err,
                            "slot_id": int(slot_id),
                            "gid": int(gid),
                        }
                        if is_head:
                            # Blocco strutturale sulla testa prioritaria:
                            # niente spesa sui candidati secondari.
                            return ActionResult(
                                False,
                                f"candidato prioritario: pulsante "
                                f"costruzione gid={gid} non trovato su "
                                f"slot {slot_id} (BTN_NOT_FOUND)",
                                data=_err,
                            )
                        first_structural = first_structural or _err
                        continue
                    if blocker_cost is None and cost:
                        _def = {
                            k: max(0, int(cost.get(k) or 0) - stock.get(k, 0))
                            for k in _res_keys
                        }
                        if any(v > 0 for v in _def.values()):
                            _eta = resource_eta_seconds(_def, prod_h)
                            if _eta is not None and _eta <= 900.0:
                                return ActionResult(
                                    False,
                                    f"candidato prioritario pagabile tra ~{int(_eta)}s: attendo C1",
                                    data={
                                        "cost": cost,
                                        "error": err,
                                        "slot_id": int(slot_id),
                                    },
                                )
                            blocker_cost, blocker_eta = cost, _eta
                    continue

            else:
                # =============================================================
                # FLUSSO B: Ampliamento (current_level >= 1)
                # =============================================================
                slot_check = _slot_url_check(slot_id)
                # Routing adattivo: 'legacy' prova prima build.php?id=N,
                # gli altri (modern/unknown) la nuova UI /slot?id=N.
                if self.profile.routing_mode == "legacy":
                    build_url = f"{selectors.PATH_BUILD}?id={int(slot_id)}"
                    alt_url = f"slot?id={int(slot_id)}"
                else:
                    build_url = f"slot?id={int(slot_id)}"
                    alt_url = f"{selectors.PATH_BUILD}?id={int(slot_id)}"
                if village_id > 0:
                    build_url += f"&newdid={village_id}"
                    alt_url += f"&newdid={village_id}"
                await self.browser.goto(build_url)

                if not slot_check.search(page.url or ""):
                    await self.browser.goto(alt_url)
                    if not slot_check.search(page.url or ""):
                        logger.debug(
                            "Slot %s: contratto non aperto (url=%s), provo il prossimo",
                            slot_id,
                            page.url,
                        )
                        continue

                try:
                    is_max_level = await page.evaluate(
                        """
                        () => /livello massimo|fully upgraded|max(imal)?(imum)?\\s*level|
                               completamente migliorato|maximale stufe|niveau max/i
                            .test(document.body.innerText || '')
                        """
                    )
                except Exception:
                    is_max_level = False
                if is_max_level:
                    logger.info(
                        "Slot %s gia' al livello massimo nel villaggio %s",
                        slot_id,
                        village_id,
                    )
                    continue

                cost = await self._read_contract_cost(page)
                if cost:
                    last_cost = cost

                button = None
                for _idx, sel in enumerate(
                    self.profile.ordered_selectors(
                        "upgrade_button", selectors.UPGRADE_BUTTON
                    )
                ):
                    locator = page.locator(sel).first
                    try:
                        if await locator.count() == 0:
                            continue
                        if await locator.is_disabled():
                            continue
                        css_class = (await locator.get_attribute("class")) or ""
                        if "disabled" not in css_class and "gold" not in css_class:
                            button = locator
                            if _idx > 0:
                                self.profile.promote_selector(
                                    "upgrade_button", sel
                                )
                            break
                    except Exception:
                        continue

                if button is None:
                    logger.debug(
                        "Slot %s non migliorabile nel villaggio %s",
                        slot_id,
                        village_id,
                    )
                    _err_code = "UPGRADE_BLOCKED"
                    if blocker_cost is None and cost:
                        _def = {
                            k: max(0, int(cost.get(k) or 0) - stock.get(k, 0))
                            for k in _res_keys
                        }
                        if any(v > 0 for v in _def.values()):
                            _err_code = "INSUFFICIENT_RESOURCES"
                            _eta = resource_eta_seconds(_def, prod_h)
                            if _eta is not None and _eta <= 900.0:
                                return ActionResult(
                                    False,
                                    f"candidato prioritario pagabile tra ~{int(_eta)}s: attendo C1",
                                    data={
                                        "cost": cost,
                                        "error": "INSUFFICIENT_RESOURCES",
                                        "slot_id": int(slot_id),
                                    },
                                )
                            blocker_cost, blocker_eta = cost, _eta
                    # Blocco temporaneo anche per gli ampliamenti: senza
                    # slot_id nel data il brain riproporrebbe questo
                    # candidato in testa a ogni ciclo (loop Pizzo).
                    blocked_err = blocked_err or {
                        "error": _err_code,
                        "slot_id": int(slot_id),
                        "gid": 0,
                    }
                    continue

            # Fallback opportunistico se blocker C1 attivo
            if blocker_cost is not None:
                if blocker_eta is None:
                    continue
                _res = {
                    k: max(0, stock.get(k, 0) - int(cost.get(k) or 0))
                    for k in _res_keys
                }
                _def1 = {
                    k: max(0, int(blocker_cost.get(k) or 0) - _res.get(k, 0))
                    for k in _res_keys
                }
                _eta_after = resource_eta_seconds(_def1, prod_h)
                if _eta_after is None or _eta_after - blocker_eta > 1800.0:
                    continue
                logger.info(
                    "Fallback opportunistico: C1 non pagabile "
                    "(ETA %.0fmin) -> avvio slot %s "
                    "(ETA C1 residuo %.0fmin)",
                    blocker_eta / 60.0,
                    slot_id,
                    _eta_after / 60.0,
                )

            await sleep_human(
                self.settings.action_delay_min, self.settings.action_delay_max
            )
            # Post-check: snapshot della coda PRIMA del click
            queue_before = await self._build_queue_count(page)
            # Coda gia' satura: il gioco ignorerebbe il click — evita
            # click a vuoto e falso QUEUE_UNCONFIRMED (transitorio:
            # lo slot resta rinviato finche' la coda non si libera).
            _qcap_pre = (
                3
                if getattr(self.settings, "allow_parallel_build", False)
                else 2
            )
            if queue_before is not None and queue_before >= _qcap_pre:
                logger.info(
                    "Coda costruzioni gia' piena (%s>=%s) su slot %s: "
                    "click evitato — QUEUE_FULL, rinvio slot",
                    queue_before,
                    _qcap_pre,
                    slot_id,
                )
                return ActionResult(
                    False,
                    f"coda costruzioni piena ({queue_before}/{_qcap_pre}): "
                    f"click su slot {slot_id} evitato",
                    data={
                        "error": "QUEUE_FULL",
                        "slot_id": int(slot_id),
                        "gid": int(gid),
                    },
                )
            # Il CTA del contratto puo' essere sotto la piega di /slot
            # (es. #contract_building26 sotto Deposito Segreto/Reggia):
            # portarlo in viewport — il click umano lavora su coordinate.
            try:
                await button.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            await sleep_human(0.2, 0.4)
            # outerHTML del CTA per diagnostica QUEUE_UNCONFIRMED (dopo
            # il redirect l'elemento e' staccato: va letto prima).
            clicked_btn_html = ""
            try:
                clicked_btn_html = (
                    await button.evaluate("el => el.outerHTML") or ""
                )[:200]
            except Exception:  # noqa: BLE001
                clicked_btn_html = ""
            await human_click(page, button)

            # Legends ts30: il click 'Costruisci edifici' esegue un
            # redirect completo /slot?id=N -> /village (dorf2.php). Il
            # check della coda deve partire sulla pagina NUOVA — sulla
            # vecchia /slot produce falsi QUEUE_UNCONFIRMED.
            try:
                await page.wait_for_url(
                    re.compile(r"/(resources|village|dorf[12]\.php)"),
                    timeout=2500,
                )
            except Exception:
                # Click a vuoto: il pulsante ha onclick="build_<token>();
                # return false;" — fallback al click DOM nativo
                # sull'elemento (non su coordinate) prima di attendere
                # il redirect verso /village.
                try:
                    await button.click(force=True, timeout=3000)
                except Exception:
                    pass
            try:
                await page.wait_for_url(
                    re.compile(r"/(resources|village|dorf[12]\.php)"),
                    timeout=8000,
                )
            except Exception:
                pass
            try:
                await page.wait_for_load_state(
                    "domcontentloaded", timeout=8000
                )
            except Exception:
                pass
            await sleep_human(1.0, 2.0)

            # Verifica reale nel DOM: la coda e' cresciuta, oppure lo
            # slot mostra il marker underConstruction, oppure una riga
            # della coda e' legata a slot/gid. ATTENZIONE: il controllo
            # deve restare STRETTISSIMO — la vecchia regex /build/i
            # matchava le classi 'buildingSlot'/'buildingList' stesse e
            # produceva falsi EXECUTED (bug Mercato slot 19).
            confirm_kind = None
            queue_after = await self._build_queue_count(page)
            for _attempt in range(3):
                try:
                    confirm_kind = await page.evaluate(
                        """
                        ({ sid, gid, qbefore }) => {
                            const sidStr = String(sid);
                            const clsOf = (el) => {
                                const c = el ? el.className : '';
                                return typeof c === 'string'
                                    ? c
                                    : (c && c.baseVal) || '';
                            };
                            const isSlotEl = (el) => {
                                if (el.getAttribute('data-aid') === sidStr) {
                                    return true;
                                }
                                const cls = clsOf(el);
                                const m = cls.match(/\\ba(\\d+)\\b/) ||
                                          cls.match(/\\bbuildingSlot(\\d+)\\b/i);
                                return m !== null && m[1] === sidStr;
                            };
                            const busyRe = /underconstruction|inconstruction/i;
                            // 1) Slot marcato 'in costruzione' nella vista
                            for (const el of document.querySelectorAll(
                                '#resourceFieldContainer a[data-aid], ' +
                                '#village_map [data-aid], #villageMap [data-aid], ' +
                                '.buildingSlot, .buildingSlot a'
                            )) {
                                if (!isSlotEl(el)) continue;
                                if (busyRe.test(clsOf(el))) return 'slot-marker';
                                const kids = el.querySelectorAll
                                    ? el.querySelectorAll('*')
                                    : [];
                                for (const k of kids) {
                                    if (busyRe.test(clsOf(k))) {
                                        return 'slot-marker-inner';
                                    }
                                }
                            }
                            // 2) Riga della coda costruzioni legata a slot/gid.
                            //    dorf2 nuova UI: il box NON usa la classe
                            //    'buildingQueue' — copre #building_contract
                            //    (anche non-table), sidebarBoxBuilding e
                            //    qualunque id/classe *queue*.
                            const linkRe = new RegExp(
                                '(build\\\\.php|slot|building)' +
                                '[?&][^"\']*\\\\bid=' + sidStr + '(?![0-9])'
                            );
                            for (const row of document.querySelectorAll(
                                '.buildingList li, #buildingContract .buildingRow, ' +
                                '#buildingContract li, ' +
                                '.buildingsOverview .buildingRow, ' +
                                'table#building_contract tr, ' +
                                '#building_contract tr, ' +
                                '#building_contract li, ' +
                                '#building_contract .buildingRow, ' +
                                '#sidebarBoxBuilding li, ' +
                                '#sidebarBoxBuilding .buildingRow, ' +
                                '#sidebarBoxBuildingQueue li, ' +
                                '#sidebarBoxBuildingQueue [class*="entry" i], ' +
                                '[id*="buildQueue" i] li, ' +
                                '[id*="buildingQueue" i] li, ' +
                                '[class*="buildQueue" i] li, ' +
                                '[class*="buildingQueue" i] li, ' +
                                '[class*="queueElement" i]'
                            )) {
                                const a = row.querySelector(
                                    'a[href*="build.php?id="], ' +
                                    'a[href*="slot?"], ' +
                                    'a[href*="building?"]'
                                ) || row.closest(
                                    'a[href*="build.php?id="], ' +
                                    'a[href*="slot?"], ' +
                                    'a[href*="building?"]'
                                );
                                if (a && linkRe.test(a.getAttribute('href') || '')) {
                                    return 'queue-link';
                                }
                                if (gid > 0) {
                                    const txt = clsOf(row) + ' ' + Array.from(
                                        row.querySelectorAll('*')
                                    ).map(c => clsOf(c)).join(' ');
                                    if (new RegExp(
                                        '\\\\bgid' + gid + '\\\\b', 'i'
                                    ).test(txt)) {
                                        return 'queue-gid';
                                    }
                                }
                            }
                            // 3) La pagina dello slot marca il lavoro in
                            // corso (nuova UI: il contratto mostra lo
                            // stato 'under construction').
                            if (document.querySelector(
                                '#content [class*="underConstruction" i], ' +
                                '#build [class*="underConstruction" i], ' +
                                '#contract [class*="underConstruction" i], ' +
                                '.buildingWrapper [class*="underConstruction" i]'
                            )) {
                                return 'contract-progress';
                            }
                            // 4) Timer in un box coda: conferma solo se
                            //    prima del click non era rilevabile alcuna
                            //    coda (evita falsi positivi su code
                            //    preesistenti di altri edifici).
                            if (qbefore === null || qbefore === 0) {
                                const qbox = document.querySelector(
                                    '#sidebarBoxBuilding, ' +
                                    '#sidebarBoxBuildingQueue, ' +
                                    '[id*="buildQueue" i], ' +
                                    '[id*="buildingQueue" i], ' +
                                    '[class*="buildQueue" i], ' +
                                    '[class*="buildingQueue" i], ' +
                                    '.buildingList, #building_contract'
                                );
                                if (qbox && qbox.querySelector(
                                    '.timer, .dur, .clocks, [class*="timer" i]'
                                )) {
                                    return 'queue-timer';
                                }
                            }
                            return null;
                        }
                        """,
                        {
                            "sid": int(slot_id),
                            "gid": int(gid),
                            "qbefore": queue_before,
                        },
                    )
                except Exception:
                    confirm_kind = None
                if confirm_kind:
                    break
                if _attempt < 2:
                    await asyncio.sleep(1.2)
                    queue_after = await self._build_queue_count(page)
            under_construction = bool(confirm_kind)

            queue_grew = (
                queue_after is not None
                and queue_before is not None
                and queue_after > queue_before
            )
            confirmed = queue_grew or under_construction

            # Coda fisica satura: il gioco ignora il click su qualsiasi
            # candidato — esito transitorio, non anomalia strutturale.
            # Niente QUEUE_UNCONFIRMED/capture; QUEUE_FULL rinvia lo
            # slot via TRANSIENT_EXEC_ERRORS finche' la firma della
            # coda non cambia (fixture 38553: Campo grano attivo, click
            # su 'Amplia' assorbito dal gioco).
            _qcap = (
                3
                if getattr(self.settings, "allow_parallel_build", False)
                else 2
            )
            if (
                not confirmed
                and queue_after is not None
                and queue_after >= _qcap
            ):
                logger.info(
                    "Coda costruzioni piena (%s>=%s): click su slot %s "
                    "non accodato dal gioco — QUEUE_FULL, rinvio slot",
                    queue_after,
                    _qcap,
                    slot_id,
                )
                return ActionResult(
                    False,
                    f"coda costruzioni piena ({queue_after}/{_qcap}): "
                    f"click su slot {slot_id} assorbito dal gioco",
                    data={
                        "error": "QUEUE_FULL",
                        "slot_id": int(slot_id),
                        "gid": int(gid),
                    },
                )

            if not confirmed:
                logger.warning(
                    "Click su costruisci/migliora senza effetto osservabile sullo "
                    "slot %s (coda %s->%s, marker=%s, url=%s, btn=%r): QUEUE_UNCONFIRMED",
                    slot_id,
                    queue_before,
                    queue_after,
                    confirm_kind,
                    page.url,
                    clicked_btn_html,
                )
                await self._capture(
                    page,
                    "QUEUE_UNCONFIRMED",
                    village_id=village_id,
                    slot_id=int(slot_id),
                    expected_gid=gid,
                    extra={
                        "queue_before": queue_before,
                        "queue_after": queue_after,
                        "confirm_kind": confirm_kind,
                        "clicked_button": clicked_btn_html,
                        "page_url": page.url,
                    },
                )
                _err = {
                    "error": "QUEUE_UNCONFIRMED",
                    "slot_id": int(slot_id),
                    "gid": int(gid),
                }
                if is_head:
                    # Click senza effetto sulla testa prioritaria: stop
                    # immediato — i candidati secondari non devono
                    # bruciare le risorse riservate allo step di testa.
                    return ActionResult(
                        False,
                        f"candidato prioritario: click senza effetto su "
                        f"slot {slot_id} (QUEUE_UNCONFIRMED)",
                        data=_err,
                    )
                first_structural = first_structural or _err
                continue

            action_name = "costruzione iniziale" if gid > 0 else "upgrade"
            logger.info(
                "%s avviata sullo slot %s (Villaggio %s, coda %s->%s)",
                action_name.capitalize(),
                slot_id,
                village_id,
                queue_before,
                queue_after,
            )
            return ActionResult(
                True,
                f"{action_name} avviata sullo slot {slot_id}",
                data={
                    "slot_id": int(slot_id),
                    "gid": gid,
                    # Il circuit-breaker STUCK (record_upgrade_exec) conta
                    # solo esecuzioni confermate in coda: skip per coda
                    # piena / click non confermato non lo alimentano.
                    "queue_confirmed": True,
                },
            )

        # Resource-ETA Sleep: riporta il costo del BLOCCANTE (C1) per
        # l'ETA corretta; se nessun bloccante, l'ultimo contratto letto.
        # Se c'e' stato un fallimento strutturale su un candidato non
        # prioritario, il codice errore viaggia nel data per
        # record_upgrade_fail (marcatura step bloccato in dashboard).
        final_cost = blocker_cost or last_cost
        _data: dict = dict(first_structural or blocked_err or {})
        if final_cost:
            _data["cost"] = final_cost
        return ActionResult(
            False,
            "nessun candidato migliorabile (risorse insufficienti o livello max)",
            data=_data,
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

    async def _dump_raid_debug(self, page, tag: str) -> None:
        """Screenshot + HTML del Punto di Raduno dopo un invio raid fallito.

        Hotfix 'ghost execution': ogni mancato invio deve lasciare un
        artefatto diagnostico in debug/<account>/ per ispezionare lo
        stato reale del DOM al momento del fallimento.
        """
        try:
            debug_dir = getattr(self.settings, "debug_dir", None)
            if debug_dir is None:
                return
            debug_dir.mkdir(parents=True, exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in str(tag))
            stamp = time.strftime("%H%M%S")
            (debug_dir / f"raid_fail_{safe}_{stamp}.html").write_text(
                await page.content(), encoding="utf-8"
            )
            await page.screenshot(
                path=str(debug_dir / f"raid_fail_{safe}_{stamp}.png"),
                full_page=True,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Dump debug raid fallito: %s", exc)

    async def _select_send_resources_tab(self, page) -> bool:
        """Clicca sul tab 'Invia risorse' (secondo tab, t=5) nel mercato se non e' gia' attivo."""
        # Se siamo già sulla scheda t=5, la consideriamo già aperta
        current_url = page.url or ""
        if "t=5" in current_url:
            return True

        # 1. Prova prima con selettori specifici per la scheda send (t=5, ecc.)
        for _idx, sel in enumerate(
            self.profile.ordered_selectors(
                "market_tab_send", selectors.MARKET_TAB_SEND
            )
        ):
            try:
                tab_loc = page.locator(sel).first
                if await tab_loc.count() > 0 and await tab_loc.is_visible():
                    await tab_loc.click(timeout=1500)
                    if _idx > 0:
                        self.profile.promote_selector("market_tab_send", sel)
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
        sx = sy = None
        try:
            s_cfg = self.settings.village_config_for(village_id=village_id)
            if s_cfg and s_cfg.x is not None and s_cfg.y is not None:
                sx, sy = s_cfg.x, s_cfg.y
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
            await self._capture(
                page,
                "MARKET_TAB_MISSING",
                village_id=village_id,
                slot_id=int(market_slot) if str(market_slot).isdigit() else None,
                expected_gid=17,
            )
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
            await self._capture(
                page,
                "MARKET_FORM_MISSING",
                village_id=village_id,
                slot_id=int(market_slot) if str(market_slot).isdigit() else None,
                expected_gid=17,
            )
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
        for _idx, sel in enumerate(
            self.profile.ordered_selectors(
                "market_target_input", selectors.MARKET_TARGET_INPUT
            )
        ):
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
                if _idx > 0:
                    self.profile.promote_selector("market_target_input", sel)
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

        if not _is_building_url(page.url):
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
            await self._capture(
                page,
                "MARKET_INPUTS_MISSING",
                village_id=village_id,
                slot_id=int(market_slot) if str(market_slot).isdigit() else None,
                expected_gid=17,
                extra={"target": str(target), "coords": [tx, ty]},
            )
            return ActionResult(
                False,
                f"form di invio mercato non trovato o campi non compilabili "
                f"(mittente '{sender_name}' -> '{target}' ({tx}|{ty})){dump_note}",
            )

        await sleep_human(self.settings.action_delay_min, self.settings.action_delay_max)

        # 5. CONFERMA FINALE: Clic sul pulsante verde "Invia risorse" in fondo alla sezione
        send_buttons = [f"{send_scope} {s}" for s in selectors.MARKET_SEND_BUTTON] + selectors.MARKET_SEND_BUTTON
        clicked_send = await self._click_first(
            page, send_buttons, profile_key="market_send_button"
        )
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
            await self._capture(
                page,
                "MARKET_SEND_BTN_NOT_FOUND",
                village_id=village_id,
                slot_id=int(market_slot) if str(market_slot).isdigit() else None,
                expected_gid=17,
            )
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

        # Radar Settore: vettore mercanti immediato (outbound + ritorno).
        # La pagina corrente mostra gia' la conferma/tabella 'Mercanti in
        # viaggio' — la lettura ETA non costa navigazioni extra.
        try:
            if sx is None or sy is None:
                # Coordinate mittente mancanti: risolvi dal profilo del
                # villaggio attivo (nome letto a pagina o newdid).
                try:
                    _sv = self.settings.village_config_for(
                        str(sender_name or village_id)
                    )
                    if _sv and _sv.x is not None and _sv.y is not None:
                        sx, sy = _sv.x, _sv.y
                except Exception:  # noqa: BLE001
                    pass
            await self._register_merchant_moves(
                page,
                village_key=str(
                    sender_name or village_id
                ),
                sx=sx,
                sy=sy,
                tx=int(tx) if tx is not None else None,
                ty=int(ty) if ty is not None else None,
                target_label=str(target or ""),
                resources=resources_dict,
                market_slot=int(market_slot or 0),
                village_id=int(village_id or 0),
            )
        except Exception as _rad_exc:
            logger.debug(
                "Registrazione radar mercanti fallita: %s", _rad_exc
            )

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

        # Radar Settore: le righe 'tr.out' visibili dopo il lancio
        # alimentano i vettori raid in uscita (ETA reale dal DOM).
        try:
            vname_rad = (
                await self._active_village_name(page)
                or f"newdid {village_id}"
            )
            _sx = _sy = None
            try:
                _scfg = self.settings.village_config_for(
                    village_id=village_id
                )
                if _scfg and _scfg.x is not None:
                    _sx, _sy = _scfg.x, _scfg.y
            except Exception:  # noqa: BLE001
                pass
            _rows = await self._scrape_outgoing_rows(page)
            if _rows:
                self._register_outgoing_radar(
                    village_key=str(vname_rad),
                    sx=_sx,
                    sy=_sy,
                    rows=_rows,
                    category=CAT_OUTGOING_RAID,
                )
        except Exception as _rad_exc:
            logger.debug(
                "Registrazione radar raid fallita: %s", _rad_exc
            )

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
        # Missione spionaggio (Smart Pre-Raid Intelligence): le spie si
        # inviano come attacco normale (c=3) con opzione 'spy' nella
        # pagina di conferma; se il server espone un radio spy dedicato
        # nel form tt=2 viene selezionato li'.
        is_scout = (
            action.type == ActionType.SEND_SCOUT
            or str(params.get("mission") or "") == "scout"
        )
        # Tipo movimento: 4=saccheggio (default), 2=rinforzo (dodge), 3=attacco
        move_type = str(
            params.get("move_type") or ("3" if is_scout else "4")
        )
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
        sent_coords: list[list[int]] = []
        failed_targets: list[dict] = []
        errors: list[str] = []

        for tgt in raw_targets:
            tx = tgt.get("x")
            ty = tgt.get("y")
            troops = dict(tgt.get("troops") or {})
            if tx is None or ty is None:
                errors.append(f"target senza coordinate ({tx}|{ty})")
                failed_targets.append(
                    {"x": tx, "y": ty, "reason": "missing_coords"}
                )
                continue
            if not troops:
                errors.append(
                    f"truppe non specificate per ({tx}|{ty})"
                )
                failed_targets.append(
                    {"x": tx, "y": ty, "reason": "missing_troops"}
                )
                continue
            # Smart Hero Combat Evaluator: l'eroe parte con l'invio solo se
            # il planner (Scenario A/B/C) lo ha incluso. Il fill e' best-
            # effort: se lo slot eroe manca/disabilitato il raid parte
            # comunque senza bloccare l'azione.
            include_hero = bool(
                tgt.get("include_hero") or params.get("include_hero")
            )

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
                        const { x, y, troops, moveType, includeHero, scout } = payload;
                        const pick = (sels) => {
                            for (const s of sels) {
                                const el = document.querySelector(s);
                                if (el) return el;
                            }
                            return null;
                        };
                        const setVal = (el, v) => {
                            el.value = String(v);
                            el.dispatchEvent(new Event('input', {bubbles: true}));
                            // keyup: il validator JS di Travian abilita il
                            // pulsante submit e ricalcola le truppe su
                            // questo evento, non solo su input/change.
                            el.dispatchEvent(new Event('keyup', {bubbles: true}));
                            el.dispatchEvent(new Event('change', {bubbles: true}));
                        };

                        // Coordinate X / Y (T4.x: name="x"/"y",
                        // id #xCoordInput/#yCoordInput)
                        const xIn = pick([
                            'input[name="x"]', '#xCoordInput', '#xCoord',
                            'input[name*="xCoord" i]', 'input#x',
                        ]);
                        const yIn = pick([
                            'input[name="y"]', '#yCoordInput', '#yCoord',
                            'input[name*="yCoord" i]', 'input#y',
                        ]);
                        if (xIn) setVal(xIn, x);
                        if (yIn) setVal(yIn, y);

                        // Radio tipo movimento: 2=Rinforzo, 3=Attacco,
                        // 4=Saccheggio
                        const raidRadio = pick([
                            `input[type="radio"][name="c"][value="${moveType}"]`,
                            `input[name="c"][value="${moveType}"]`,
                            `input[type="radio"][value="${moveType}"]`,
                            `label:has(input[value="${moveType}"]) input`,
                        ]);
                        if (raidRadio) {
                            raidRadio.checked = true;
                            raidRadio.dispatchEvent(
                                new MouseEvent('click', {bubbles: true})
                            );
                            raidRadio.dispatchEvent(
                                new Event('change', {bubbles: true})
                            );
                        }

                        // Missione spionaggio: alcuni layout espongono
                        // un radio 'spy' gia' nel form tt=2 (spy=1 =
                        // esplora risorse e truppe). Se presente viene
                        // selezionato; altrimenti l'opzione si gestisce
                        // nella pagina di conferma.
                        if (scout) {
                            const spyRadio = pick([
                                'input[type="radio"][name="spy"][value="1"]',
                                'input[name="spy"][value="1"]',
                                'input[type="radio"][name="spy"]',
                            ]);
                            if (spyRadio) {
                                spyRadio.checked = true;
                                spyRadio.dispatchEvent(
                                    new Event('change', {bubbles: true})
                                );
                            }
                        }

                        // Quantita' truppe: il form T4.x usa slot
                        // RELATIVI troop[t1]..troop[t10] per le unita'
                        // del villaggio (t11 = eroe). Le chiavi globali
                        // t1..t30 vanno rimappate sullo slot relativo.
                        const filled = [];
                        const missed = [];
                        for (const [key, qty] of Object.entries(troops)) {
                            const q = parseInt(qty, 10) || 0;
                            if (q <= 0) continue;
                            const m = String(key).match(/(\\d+)/);
                            if (!m) { missed.push(String(key)); continue; }
                            const num = parseInt(m[1], 10);
                            const rel = ((num - 1) % 10) + 1;
                            const sels = [
                                `input[name="troop[t${rel}]"]`,
                                `input[name="t${rel}"]`,
                                `input[name*="[t${rel}]"]`,
                            ];
                            // Form a nomi globali (versioni datate):
                            // t11 e' sempre l'eroe, mai riempirlo qui.
                            if (num !== 11) {
                                sels.push(`input[name="troop[t${num}]"]`);
                                sels.push(`input[name="t${num}"]`);
                            }
                            const input = pick(sels);
                            if (input && !input.disabled) {
                                setVal(input, q);
                                filled.push(String(key));
                            } else {
                                missed.push(String(key));
                            }
                        }

                        // Eroe (slot t11): best-effort — se manca o e'
                        // disabilitato il raid parte comunque senza.
                        if (includeHero) {
                            const heroIn = pick([
                                'input[name="t11"]',
                                'input[name="troop[t11]"]',
                                'input[name*="hero" i]', 'input#hero',
                            ]);
                            if (heroIn && !heroIn.disabled) {
                                setVal(heroIn, '1');
                            }
                        }

                        const problems = [];
                        if (!xIn || !yIn) problems.push('coord_inputs');
                        if (!raidRadio) problems.push('move_radio');
                        if (!filled.length) problems.push('troop_inputs');
                        if (missed.length) {
                            problems.push('missed:' + missed.join(','));
                        }
                        return {
                            ok: !!(xIn && yIn && filled.length > 0),
                            reason: problems.join('|'),
                            filled: filled,
                            url: location.href,
                        };
                    }
                    """,
                    {
                        "x": tx, "y": ty, "troops": troops,
                        "moveType": move_type, "includeHero": include_hero,
                        "scout": is_scout,
                    },
                )

                if not fill_res or not fill_res.get("ok"):
                    _reason = str((fill_res or {}).get("reason") or "n/d")
                    logger.error(
                        "Custom raid: compilazione form fallita (%s|%s): %s",
                        tx, ty, _reason,
                    )
                    await self._dump_raid_debug(page, f"fill_{tx}_{ty}")
                    errors.append(
                        f"compilazione form fallita ({tx}|{ty}): {_reason}"
                    )
                    failed_targets.append(
                        {"x": tx, "y": ty, "reason": _reason}
                    )
                    continue

                # Tracciamento diagnostico: quali input truppe hanno
                # ricevuto un valore (correla col motivo di rifiuto
                # quando il server lascia still_on_send_form).
                logger.info(
                    "Custom raid: form compilato (%s|%s): truppe=%s",
                    tx, ty, (fill_res or {}).get("filled"),
                )

                await sleep_human(
                    self.settings.action_delay_min, self.settings.action_delay_max
                )

                # 3. Primo submit: invio ordine truppe (scopato al
                # contenuto del Punto di Raduno: button[type=submit]
                # generico intercetterebbe bottoni di altri form).
                if not await self._click_first(
                    page,
                    [
                        '#build button#btn_ok',
                        '#content button#btn_ok',
                        'button#btn_ok',
                        '#build button[type="submit"]',
                        '#content button[type="submit"]',
                        '#build input[type="submit"]',
                        '#content input[type="submit"]',
                        '#ok',
                        '#build button:has-text("Invia")',
                        '#content button:has-text("Invia")',
                        '#build button:has-text("Send")',
                    ],
                    timeout_ms=3000,
                ):
                    logger.error(
                        "Custom raid: pulsante invio non trovato (%s|%s)",
                        tx, ty,
                    )
                    await self._dump_raid_debug(page, f"submit_{tx}_{ty}")
                    errors.append(f"primo invio fallito per ({tx}|{ty})")
                    failed_targets.append(
                        {"x": tx, "y": ty, "reason": "send_button_missing"}
                    )
                    continue

                await sleep_human(0.8, 1.5)

                # 4. Attesa della pagina di riepilogo (secondo step del
                # flow tt=2): il server puo' rispondere allo stesso form
                # con un box errore rosso (campo mancante, truppe
                # insufficienti). In quel caso il click di conferma
                # sarebbe cieco: loggiamo il messaggio esatto del
                # server e passiamo al target successivo.
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=8000
                    )
                except Exception:  # noqa: BLE001 — nessun reload (SPA)
                    pass
                await sleep_human(0.5, 1.0)

                mid = await page.evaluate(
                    """
                    () => {
                        const vis = (e) => !!(e && (
                            e.offsetParent !== null ||
                            (e.getClientRects && e.getClientRects().length)
                        ));
                        const errs = Array.from(document.querySelectorAll(
                            '.error, .errorBox, .errorMessage, .formError, ' +
                            '.alert, .notice, [class*="error" i]'
                        )).filter(e => vis(e) && (e.innerText || '').trim());
                        // Input coordinate VISIBILI = ancora sul form di
                        // invio. Sul riepilogo tt=2 x/y sono hidden.
                        const coordInputs = Array.from(
                            document.querySelectorAll(
                                'input[name="x"], #xCoordInput, ' +
                                'input[name="y"], #yCoordInput, ' +
                                'input[name*="xCoord" i]'
                            )
                        ).filter(e => vis(e));
                        return {
                            onForm: coordInputs.length > 0,
                            err: errs.slice(0, 3).map(
                                e => (e.innerText || '')
                                    .trim().replace(/\\s+/g, ' ').slice(0, 120)
                            ).join(' | '),
                            hdr: ((document.querySelector(
                                '#content h1, #content h4, h1, h4, ' +
                                '.titleInHeader, .headerText'
                            ) || {}).innerText || '').trim().slice(0, 80),
                        };
                    }
                    """
                )
                if (mid or {}).get("onForm"):
                    _merr = str((mid or {}).get("err") or "nessun messaggio")
                    _mhdr = str((mid or {}).get("hdr") or "")
                    logger.error(
                        "Custom raid: primo submit rifiutato (%s|%s): "
                        "form tt=2 ancora attivo, messaggio server: %s "
                        "(sezione: %s)",
                        tx, ty, _merr, _mhdr,
                    )
                    await self._dump_raid_debug(page, f"reject_{tx}_{ty}")
                    errors.append(
                        f"submit rifiutato ({tx}|{ty}): {_merr}"
                    )
                    failed_targets.append(
                        {"x": tx, "y": ty,
                         "reason": f"submit_rejected:{_merr}"}
                    )
                    continue

                # 4bis. Missione spionaggio: la pagina di conferma
                # espone i radio 'spy' (1 = risorse e truppe, 2 = solo
                # truppe/difese). Selezioniamo l'opzione 1 prima del
                # click di conferma (spec c=4 / Esplora risorse e truppe).
                if is_scout:
                    try:
                        await page.evaluate(
                            """
                            () => {
                                const spy = document.querySelector(
                                    'input[type="radio"][name="spy"][value="1"], ' +
                                    'input[name="spy"][value="1"], ' +
                                    'input[type="radio"][name="spy"]'
                                );
                                if (spy) {
                                    spy.checked = true;
                                    spy.dispatchEvent(
                                        new MouseEvent('click', {bubbles: true})
                                    );
                                    spy.dispatchEvent(
                                        new Event('change', {bubbles: true})
                                    );
                                }
                            }
                            """
                        )
                    except Exception:  # noqa: BLE001 — opzione assente
                        pass

                # 5. Secondo submit: conferma del raid nella schermata
                # riepilogo. ATTENZIONE: sotto la tabella truppe ci sono
                # TRE pulsanti — "Modifica" (verde, PRIMO nel DOM),
                # "Generatore di invio truppe" (gold) e "Conferma"
                # (verde, ULTIMO). Selettori posizionali (#btn_ok,
                # .green, name=s1) catturano "Modifica" che ricarica il
                # form -> falso still_on_send_form. Priorita' alla
                # etichetta testuale, escludendo i bottoni di ritorno.
                confirmed_click = await self._click_first(
                    page,
                    [
                        '#build button:has-text("Conferma")',
                        '#content button:has-text("Conferma")',
                        '#build button:has-text("Confirm")',
                        '#content button:has-text("Confirm")',
                        '#build button[type="submit"]:not(:has-text("Modifica")):not(:has-text("Edit"))',
                        '#content button[type="submit"]:not(:has-text("Modifica")):not(:has-text("Edit"))',
                    ],
                    timeout_ms=3500,
                )
                if not confirmed_click:
                    # Fallback JS limitato all'area contenuto del rally
                    # (#build / #content): scarta esplicitamente i
                    # bottoni di ritorno (Modifica/Edit/Indietro/Back/
                    # Annulla/Cancel) e clicca solo "Conferma".
                    confirmed_click = bool(
                        await page.evaluate(
                            """
                            () => {
                                const scope = document.querySelector('#build, #content');
                                if (!scope) return false;
                                const btns = Array.from(
                                    scope.querySelectorAll('button, input[type="submit"]')
                                ).filter(b => !b.disabled && b.offsetParent !== null);
                                const target = btns.find(b => {
                                    const txt = (b.innerText || b.value || '').trim().toLowerCase();
                                    if (/modifica|edit|indietro|back|annulla|cancel/.test(txt)) {
                                        return false;
                                    }
                                    return /conferma|confirm|ok/.test(txt);
                                });
                                if (!target) return false;
                                target.click();
                                return true;
                            }
                            """
                        )
                    )
                if not confirmed_click:
                    logger.error(
                        "Custom raid: pulsante conferma non trovato (%s|%s)",
                        tx, ty,
                    )
                    await self._dump_raid_debug(page, f"confirm_{tx}_{ty}")
                    errors.append(f"conferma raid fallita per ({tx}|{ty})")
                    failed_targets.append(
                        {"x": tx, "y": ty, "reason": "confirm_missing"}
                    )
                    continue

                # 6. Anti-ghost: l'invio conta solo con riscontro visivo.
                # Se il gioco ha rifiutato il submit (validazione, truppe
                # insufficienti) la pagina resta sul form tt=2 o mostra
                # un box di errore: senza questo controllo il click sul
                # pulsante veniva registrato come "raid inviato" pur
                # senza alcun movimento truppe reale.
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=8000
                    )
                except Exception:  # noqa: BLE001 — nessun reload (SPA)
                    pass
                await sleep_human(0.6, 1.2)
                verify = await page.evaluate(
                    """
                    () => {
                        const url = location.href || '';
                        const vis = (e) => !!(e && (
                            e.offsetParent !== null ||
                            (e.getClientRects && e.getClientRects().length)
                        ));
                        const errs = Array.from(document.querySelectorAll(
                            '.error, .errorBox, .errorMessage, .formError, ' +
                            '.alert.error, .notice.error, ' +
                            '[class*="error" i]'
                        )).filter(e => vis(e) && (e.innerText || '').trim());
                        if (errs.length) {
                            return {
                                ok: false,
                                reason: 'error_box:' + errs.slice(0, 3).map(
                                    e => (e.innerText || '')
                                        .trim().slice(0, 80)
                                ).join(' | '),
                                url: url,
                            };
                        }
                        const stillForm =
                            /tt=2(?:&|$)/.test(url) &&
                            Array.from(document.querySelectorAll(
                                'input[name="x"], #xCoordInput, ' +
                                'input[name="y"], #yCoordInput, ' +
                                'input[name*="xCoord" i]'
                            )).some(e => vis(e));
                        if (stillForm) {
                            return {
                                ok: false,
                                reason: 'still_on_send_form' +
                                    (errs.length ? ' | ' + errs.slice(0, 3)
                                        .map(e => (e.innerText || '')
                                            .trim().replace(/\\s+/g, ' ')
                                            .slice(0, 80))
                                        .join(' | ') : ''),
                                url: url,
                            };
                        }
                        const mov = document.querySelector(
                            '#movements, .troopMovements, ' +
                            '[id*="troopMovement" i], .movement'
                        );
                        return { ok: true, url: url, movement: !!mov };
                    }
                    """
                )
                if not (verify or {}).get("ok"):
                    _vreason = str((verify or {}).get("reason") or "n/d")
                    logger.error(
                        "Custom raid: invio NON confermato (%s|%s): %s "
                        "(anti-ghost)",
                        tx, ty, _vreason,
                    )
                    await self._dump_raid_debug(page, f"verify_{tx}_{ty}")
                    errors.append(
                        f"invio non confermato ({tx}|{ty}): {_vreason}"
                    )
                    failed_targets.append(
                        {"x": tx, "y": ty, "reason": _vreason}
                    )
                    continue

                sent_count += 1
                sent_coords.append([int(tx), int(ty)])
                logger.info(
                    "Raid dinamico confermato a (%s|%s) con truppe %s",
                    tx, ty, troops,
                )
                # Radar Settore: vettore raid in uscita. L'ETA si legge
                # dalle righe 'tr.out' se la pagina mostra gia' la
                # panoramica movimenti; altrimenti si stima da
                # distanza/velocita' truppe (scalata server_speed).
                try:
                    vname_rad = (
                        await self._active_village_name(page)
                        or f"newdid {village_id}"
                    )
                    _sx = _sy = None
                    try:
                        _scfg = self.settings.village_config_for(
                            village_id=village_id
                        )
                        if _scfg and _scfg.x is not None:
                            _sx, _sy = _scfg.x, _scfg.y
                    except Exception:  # noqa: BLE001
                        pass
                    _rows = await self._scrape_outgoing_rows(page)
                    _secs = 0.0
                    for _r in _rows:
                        if _r.get("cx") == int(tx) and _r.get("cy") == int(ty):
                            _secs = float(_r.get("secs") or 0.0)
                            break
                    if _secs <= 0 and _sx is not None:
                        _secs = travel_seconds(
                            troops, _sx, _sy, int(tx), int(ty)
                        ) / max(
                            0.5,
                            float(getattr(
                                self.settings, "server_speed", 1.0
                            ) or 1.0),
                        )
                    if _secs > 0:
                        _troops_sum = ", ".join(
                            f"{v} {STATIC_TROOP_LABELS.get(k, k)}"
                            for k, v in troops.items()
                            if int(v or 0) > 0
                        )
                        _rs = self._radar()
                        if _rs is not None:
                            _rs.register(
                                str(vname_rad),
                                {
                                    "category": (
                                        CAT_OUTGOING_ATTACK
                                        if move_type == "3"
                                        else CAT_OUTGOING_RAID
                                    ),
                                    "direction": "outbound",
                                    "from_x": _sx,
                                    "from_y": _sy,
                                    "to_x": int(tx),
                                    "to_y": int(ty),
                                    "counterpart": f"({tx}|{ty})",
                                    "label": f"({tx}|{ty})",
                                    "started_at": time.time(),
                                    "arrives_at": time.time() + _secs,
                                    "details": {
                                        "troops_summary": _troops_sum,
                                    },
                                },
                            )
                except Exception as _rad_exc:
                    logger.debug(
                        "Registrazione radar raid fallita: %s", _rad_exc
                    )
                await sleep_human(1.0, 2.5)

            except Exception as exc:
                logger.error(
                    "Custom raid: eccezione invio (%s|%s): %s",
                    tx, ty, exc, exc_info=True,
                )
                try:
                    await self._dump_raid_debug(page, f"exc_{tx}_{ty}")
                except Exception:  # noqa: BLE001
                    pass
                errors.append(f"eccezione su ({tx}|{ty}): {exc}")
                failed_targets.append(
                    {"x": tx, "y": ty, "reason": str(exc)}
                )

        _data = {
            "sent_coords": sent_coords,
            "failed_targets": failed_targets,
        }
        if sent_count > 0:
            return ActionResult(
                True,
                f"inviati {sent_count}/{len(raw_targets)} raid dinamici "
                f"({', '.join(errors) if errors else 'tutti riusciti'})",
                data=_data,
            )
        return ActionResult(
            False,
            f"nessun raid dinamico inviato: {'; '.join(errors)}",
            data=_data,
        )

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

    async def _start_celebration(
        self, action: Action, village_id: int = 0
    ) -> ActionResult:
        """Avvia una festa nel Municipio (Culture Points Booster).

        Naviga a build.php?id=<town_hall_slot> e clicca il link della
        festa richiesta (default: 'small' = Piccola Festa). Se una
        festa e' gia' in corso o le risorse non bastano il link non e'
        cliccabile: l'azione fallisce in modo pulito e il chiamante
        riprova al prossimo ciclo.
        """
        params = action.params or {}
        th_slot = int(
            params.get("town_hall_slot") or params.get("slot_id") or 0
        )
        if th_slot <= 0:
            return ActionResult(False, "slot Municipio mancante (town_hall_slot)")
        kind = str(params.get("kind") or "small").strip().lower()

        page = self.browser.page
        assert page is not None

        url = f"{selectors.PATH_BUILD}?id={th_slot}"
        if village_id > 0:
            url += f"&newdid={village_id}"
        await self.browser.goto(url)
        await sleep_human(1.0, 1.5)

        info = await page.evaluate(
            """
            () => {
                const text = (document.body && document.body.innerText) || '';
                // Una festa in corso mostra il countdown nel Municipio.
                const running = /celebration|festa|feier|fete|fiesta/i.test(text)
                    && /(\\d+:\\d{2}:\\d{2})/.test(text);
                const links = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const href = a.getAttribute('href') || '';
                    const label = (a.innerText || a.textContent || '')
                        .trim().toLowerCase();
                    if (/celebrat|festa|feier|fete|fiesta|great|small|piccola|grande|gro(ss|ß)|gran/i
                            .test(href + ' ' + label)) {
                        links.push({ href, label });
                    }
                });
                return { running, links };
            }
            """
        ) or {}

        links = info.get("links") or []
        if not links:
            why = (
                "festa gia' in corso o link assente"
                if info.get("running")
                else "nessun link festa nel Municipio "
                     "(risorse insufficienti?)"
            )
            return ActionResult(False, why)

        # Preferisci la festa 'small' (piccola): la grande costa di piu'
        # e serve solo per i Punti Cultura massivi del chiefing.
        pick = None
        if kind == "small":
            pick = next(
                (
                    l
                    for l in links
                    if any(
                        w in l.get("label", "") + l.get("href", "")
                        for w in ("small", "piccola", "kleine", "petite")
                    )
                ),
                None,
            )
        if pick is None:
            pick = links[0]
        href = pick.get("href")
        if not href:
            return ActionResult(False, "link festa senza href")

        await self.browser.goto(href)
        await sleep_human(1.0, 2.0)
        logger.info(
            "Festa avviata al Municipio slot %s (villaggio %s): %s",
            th_slot, village_id, pick.get("label") or href,
        )
        return ActionResult(
            True, f"festa avviata al Municipio (slot {th_slot})"
        )

    async def _upgrade_smithy_units(
        self, action: Action, village_id: int = 0
    ) -> ActionResult:
        """Accoda un potenziamento unita' in Fucina (smithy_auto_upgrade).

        Naviga a build.php?id=<smithy_slot>, rileva la tribu' dalle icone
        unita' della pagina, risolve gli slot astratti del preset
        (infantry_off, ram, ...) nei numeri unita' corretti e clicca il
        primo upgrade sotto target_level. I link di potenziamento T4
        portano 'a=<unit>' nell'href, quindi il click avvia il lavoro
        nella coda ricerca (parallela alla coda costruzioni).
        """
        params = action.params or {}
        smithy_slot = int(
            params.get("smithy_slot") or params.get("slot_id") or 0
        )
        if smithy_slot <= 0:
            return ActionResult(False, "slot Fucina mancante (smithy_slot)")
        target_level = int(params.get("target_level") or 20)
        wanted_units = list(params.get("units") or [])

        page = self.browser.page
        assert page is not None

        url = f"{selectors.PATH_BUILD}?id={smithy_slot}"
        if village_id > 0:
            url += f"&newdid={village_id}"
        await self.browser.goto(url)
        await sleep_human(1.0, 1.5)

        from .presets import TRIBE_UNIT_SLOTS  # import ritardato: no cicli

        outcome = await page.evaluate(
            """
            (args) => {
                const slots = args.slots || [];
                const target = args.target_level || 20;
                const slotMap = args.slot_map || {};
                const busyWords =
                    /max|pieno|fully|complet|in coda|queue|risorse insufficienti/i;

                // 1. Tribu' dalle icone unita' della pagina
                let tribe = '';
                for (const el of document.querySelectorAll(
                    '[class*="unit u"], .unit'
                )) {
                    const m = (el.getAttribute('class') || '')
                        .match(/\\bu(\\d+)\\b/);
                    if (!m) continue;
                    const n = parseInt(m[1], 10);
                    if (n >= 1 && n <= 10) { tribe = 'romans'; break; }
                    if (n >= 11 && n <= 20) { tribe = 'teutons'; break; }
                    if (n >= 21 && n <= 30) { tribe = 'gauls'; break; }
                }
                if (!tribe) {
                    return {ok: false, why: 'tribu non rilevata in fucina'};
                }

                // 2. Insieme unita' richieste (slot astratti o tN/uN diretti)
                const wanted = new Set();
                for (const s of slots) {
                    const k = String(s).trim().toLowerCase();
                    const direct = k.match(/^[ut](\\d+)$/);
                    if (direct) { wanted.add(parseInt(direct[1], 10)); continue; }
                    const mp = slotMap[k];
                    if (mp && mp[tribe]) {
                        wanted.add(parseInt(String(mp[tribe]).slice(1), 10));
                    }
                }

                // 3. Link di potenziamento unita': T4 usa 'a=<unit>' nel
                //    contratto della Fucina. Cerca nella riga il livello
                //    corrente e verifica che sia sotto target.
                const candidates = [];
                for (const a of document.querySelectorAll('a[href*="a="]')) {
                    const href = a.getAttribute('href') || '';
                    const am = href.match(/[?&]a=(\\d+)/);
                    if (!am) continue;
                    const uN = parseInt(am[1], 10);
                    if (uN < 1 || uN > 30) continue;
                    if (wanted.size && !wanted.has(uN)) continue;
                    const row = a.closest(
                        'tr, li, .buildingDetail, .details, div'
                    ) || a.parentElement;
                    const txt = (row && row.innerText) || '';
                    if (busyWords.test(txt)) continue;
                    const lm = txt.match(
                        /(?:livello|level|liv\\.?)\\s*[:.]?\\s*(\\d+)/i
                    );
                    const lvl = lm ? parseInt(lm[1], 10) : null;
                    if (lvl !== null && lvl >= target) continue;
                    candidates.push({uN: uN, lvl: lvl, el: a});
                }
                if (!candidates.length) {
                    return {
                        ok: false,
                        why: 'nessun upgrade fucina disponibile (max/coda/risorse)',
                    };
                }
                candidates.sort((a, b) => (a.lvl ?? 999) - (b.lvl ?? 999));
                const pick = candidates[0];
                pick.el.click();
                return {ok: true, unit: 'u' + pick.uN, level: pick.lvl};
            }
            """,
            {
                "slots": wanted_units,
                "target_level": target_level,
                "slot_map": TRIBE_UNIT_SLOTS,
            },
        ) or {}

        if not outcome.get("ok"):
            return ActionResult(
                False,
                str(outcome.get("why") or "upgrade fucina non disponibile"),
            )

        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )
        logger.info(
            "Fucina: avviato upgrade %s (liv. %s) nello slot %s, villaggio %s",
            outcome.get("unit"), outcome.get("level"),
            smithy_slot, village_id,
        )
        return ActionResult(
            True,
            f"upgrade fucina avviato per {outcome.get('unit')} "
            f"(liv. {outcome.get('level')})",
        )

    def _radar(self):
        """RadarStore dell'account corrente (None se non inizializzabile)."""
        try:
            return radar_store(self.settings.account_id)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    async def _scrape_outgoing_rows(page) -> list[dict]:
        """Righe 'tr.out' della panoramica movimenti sulla pagina corrente.

        Ritorna [{secs, cx, cy, count, label}] — usato dopo un invio di
        truppe (raid/attacco) per registrare il vettore sul radar senza
        navigazioni aggiuntive. Lista vuota se il DOM non le espone.
        """
        try:
            rows = await page.evaluate(
                """
                () => {
                    const out = [];
                    const parseRow = (row) => {
                        const rowText = (row.innerText || '').trim();
                        const timerEl = row.querySelector(
                            '.timer, span.timer, .dur'
                        );
                        const ttxt = timerEl
                            ? (timerEl.textContent || '') : rowText;
                        const m = ttxt.match(
                            /(?:(\\d+)\\s*[-+]\\s*)?(?:(\\d+):)?(\\d+):(\\d{2})/
                        );
                        let secs = 0;
                        if (m) {
                            secs =
                                (parseInt(m[1] || 0, 10) * 86400) +
                                (parseInt(m[2] || 0, 10) * 3600) +
                                (parseInt(m[3] || 0, 10) * 60) +
                                parseInt(m[4] || 0, 10);
                        }
                        let cx = null, cy = null;
                        const mapLink = row.querySelector(
                            'a[href*="karte"]'
                        );
                        if (mapLink) {
                            const href =
                                mapLink.getAttribute('href') || '';
                            const hm = href.match(
                                /x=(-?\\d+)[^0-9-]*y=(-?\\d+)/
                            );
                            if (hm) {
                                cx = parseInt(hm[1], 10);
                                cy = parseInt(hm[2], 10);
                            }
                        }
                        if (cx === null) {
                            const cm = rowText.match(
                                /\\((-?\\d{1,3})\\s*\\|\\s*(-?\\d{1,3})\\)/
                            );
                            if (cm) {
                                cx = parseInt(cm[1], 10);
                                cy = parseInt(cm[2], 10);
                            }
                        }
                        let count = 0;
                        row.querySelectorAll('td, span').forEach(el => {
                            const t = (el.innerText || '').trim();
                            if (/^\\d+$/.test(t) && t.length < 7) {
                                count += parseInt(t, 10);
                            }
                        });
                        const pLink = row.querySelector(
                            'a[href*="spieler"], a[href*="profile"], ' +
                            'a[href*="karte"]'
                        );
                        out.push({
                            secs: secs,
                            cx: isNaN(cx) ? null : cx,
                            cy: isNaN(cy) ? null : cy,
                            count: count,
                            label: rowText.slice(0, 140),
                            who: pLink ? (pLink.innerText || '').trim()
                                       : '',
                        });
                    };
                    document.querySelectorAll(
                        'table.troop_details tr.out, ' +
                        '.troop_details tr.out, #content tr.out, ' +
                        '#content table.troop_details tr.out'
                    ).forEach(parseRow);
                    return out;
                }
                """
            )
            return [r for r in (rows or []) if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            return []

    def _register_outgoing_radar(
        self,
        *,
        village_key: str,
        sx,
        sy,
        rows: list[dict],
        category: str,
        troops_summary: str = "",
    ) -> None:
        """Registra i vettori in partenza nel RadarStore."""
        rs = self._radar()
        if rs is None:
            return
        now = time.time()
        for row in rows or []:
            secs = float(row.get("secs") or 0.0)
            if secs <= 0:
                continue
            who = str(row.get("who") or row.get("label") or "")[:80]
            summary = troops_summary or (
                f"{int(row.get('count') or 0)} truppe"
                if int(row.get("count") or 0) > 0
                else ""
            )
            rs.register(
                village_key,
                {
                    "category": category,
                    "direction": "outbound",
                    "from_x": sx,
                    "from_y": sy,
                    "to_x": row.get("cx"),
                    "to_y": row.get("cy"),
                    "counterpart": who,
                    "label": who,
                    "started_at": now,
                    "arrives_at": now + secs,
                    "details": {"troops_summary": summary},
                },
            )

    async def _register_merchant_moves(
        self,
        page,
        *,
        village_key: str,
        sx,
        sy,
        tx,
        ty,
        target_label: str,
        resources: dict | None = None,
        market_slot: int = 0,
        village_id: int = 0,
    ) -> None:
        """Vettore Mercanti outbound + segmento di ritorno sul radar.

        L'ETA si legge dalla riga di conferma / tabella 'Mercanti in
        viaggio' (primo .timer del contenuto); se assente si stima dalla
        distanza e dalla velocita' mercanti della tribu' (campi/ora,
        scalata per server_speed). Il segmento di ritorno dura il doppio
        dell'andata come da spec radar.
        """
        rs = self._radar()
        if rs is None:
            return
        eta_secs = 0.0
        try:
            eta_secs = float(
                await page.evaluate(
                    """
                    () => {
                        const scope = document.querySelector(
                            '#content, .content, main, .buildingDetails'
                        ) || document.body;
                        const timers = Array.from(scope.querySelectorAll(
                            '.timer, span.timer, td.timer, .dur'
                        ));
                        const pick = timers.length
                            ? (timers[0].textContent || '')
                            : (scope.innerText || '');
                        const m = pick.match(
                            /(?:(\\d+)\\s*[-+]\\s*)?(?:(\\d+):)?(\\d+):(\\d{2})/
                        );
                        return m
                            ? (parseInt(m[1] || 0, 10) * 86400) +
                              (parseInt(m[2] || 0, 10) * 3600) +
                              (parseInt(m[3] || 0, 10) * 60) +
                              parseInt(m[4] || 0, 10)
                            : 0;
                    }
                    """
                )
                or 0.0
            )
        except Exception:  # noqa: BLE001
            eta_secs = 0.0
        if eta_secs <= 0:
            # Stima: velocita' mercanti per tribu' (campi/ora, server x1).
            speed = {
                "romans": 16.0, "teutons": 12.0, "gauls": 24.0,
            }.get(str(self.settings.tribe or "").lower(), 16.0)
            try:
                if sx is not None and sy is not None and tx is not None:
                    eta_secs = (
                        map_distance(sx, sy, tx, ty) / speed * 3600.0
                    )
                    eta_secs /= max(
                        0.5,
                        float(getattr(
                            self.settings, "server_speed", 1.0
                        ) or 1.0),
                    )
            except Exception:  # noqa: BLE001
                eta_secs = 0.0
        # Nessun return anticipato: con ETA ignota si salta solo la
        # registrazione del vettore appena inviato, ma il sync della
        # tabella 'Mercanti in viaggio' sotto viene eseguito comunque
        # (copre anche gli invii manuali dell'utente).
        now = time.time()
        arrives = now + eta_secs
        label = str(target_label or "")
        if tx is not None and ty is not None and f"({tx}" not in label:
            label = f"{label} ({tx}|{ty})".strip()
        res_details = {
            str(k): int(v)
            for k, v in (resources or {}).items()
            if isinstance(v, (int, float)) and int(v) > 0
        }
        _n_reg = 0
        if eta_secs > 0:
            rs.register(
                village_key,
                {
                    "category": CAT_MERCHANT,
                    "direction": "outbound",
                    "from_x": sx,
                    "from_y": sy,
                    "to_x": tx,
                    "to_y": ty,
                    "counterpart": label,
                    "label": label,
                    "started_at": now,
                    "arrives_at": arrives,
                    "details": {"resources": res_details},
                },
            )
            # Segmento di ritorno dei mercanti: parte quando arrivano e
            # impiega la stessa durata dell'andata.
            rs.register(
                village_key,
                {
                    "category": CAT_MERCHANT,
                    "direction": "return",
                    "from_x": tx,
                    "from_y": ty,
                    "to_x": sx,
                    "to_y": sy,
                    "counterpart": label,
                    "label": f"Ritorno mercanti {label}".strip(),
                    "started_at": arrives,
                    "arrives_at": arrives + eta_secs,
                    "details": {"resources": {}},
                },
            )
            _n_reg = 2
            # Vettore speculare inbound: se la destinazione e' un
            # villaggio proprio dell'account, il radar lo visualizza
            # anche quando l'utente seleziona il villaggio ricevente.
            try:
                for _key, _vcfg in (
                    getattr(self.settings, "villages", {}) or {}
                ).items():
                    if (
                        tx is not None
                        and ty is not None
                        and getattr(_vcfg, "x", None) == int(tx)
                        and getattr(_vcfg, "y", None) == int(ty)
                    ):
                        dest_key = (
                            str(getattr(_vcfg, "name", "") or "")
                            or str(_key)
                        )
                        if dest_key == village_key:
                            break
                        rs.register(
                            dest_key,
                            {
                                "category": CAT_MERCHANT,
                                "direction": "inbound",
                                "from_x": sx,
                                "from_y": sy,
                                "to_x": tx,
                                "to_y": ty,
                                "counterpart": village_key,
                                "label": (
                                    f"Mercanti da {village_key} {label}"
                                ).strip(),
                                "started_at": now,
                                "arrives_at": arrives,
                                "details": {"resources": res_details},
                            },
                        )
                        _n_reg += 1
                        break
            except Exception:  # noqa: BLE001
                pass
        logger.info(
            "Radar: villaggio '%s' — invio mercanti verso '%s', "
            "ETA %.0fs, %d vettori registrati",
            village_key, label or "?", eta_secs, _n_reg,
        )
        # Sync live della tabella 'Mercanti in viaggio': la pagina del
        # Mercato elenca TUTTE le spedizioni attive, comprese quelle
        # avviate manualmente dal giocatore. Per i mercanti in partenza
        # si sincronizza anche il segmento di ritorno (stessa durata).
        try:
            from .sensors import TravianParser

            rows = await TravianParser(page).read_market_movements(
                market_slot=int(market_slot or 0),
                village_id=int(village_id or 0),
            )
            if rows:
                now2 = time.time()
                synced: list[dict] = []
                for row in rows:
                    secs2 = float(row.get("secs") or 0.0)
                    if secs2 <= 0:
                        continue
                    arr2 = now2 + secs2
                    cx = row.get("cx")
                    cy = row.get("cy")
                    who = str(
                        row.get("counterpart") or row.get("label") or ""
                    )[:80]
                    rres = {
                        str(k): int(v)
                        for k, v in (row.get("resources") or {}).items()
                        if isinstance(v, (int, float)) and int(v) > 0
                    }
                    if str(row.get("direction") or "") == "inbound":
                        synced.append(
                            {
                                "category": CAT_MERCHANT,
                                "direction": "inbound",
                                "from_x": cx,
                                "from_y": cy,
                                "to_x": sx,
                                "to_y": sy,
                                "counterpart": who,
                                "label": who or "Mercanti in arrivo",
                                "arrives_at": arr2,
                                "details": {"resources": rres},
                            }
                        )
                    else:
                        synced.append(
                            {
                                "category": CAT_MERCHANT,
                                "direction": "outbound",
                                "from_x": sx,
                                "from_y": sy,
                                "to_x": cx,
                                "to_y": cy,
                                "counterpart": who,
                                "label": who or "Mercanti in viaggio",
                                "arrives_at": arr2,
                                "details": {"resources": rres},
                            }
                        )
                        # Rientro previsto: parte all'arrivo e dura
                        # quanto l'andata (spec radar mercanti).
                        synced.append(
                            {
                                "category": CAT_MERCHANT,
                                "direction": "return",
                                "from_x": cx,
                                "from_y": cy,
                                "to_x": sx,
                                "to_y": sy,
                                "counterpart": who,
                                "label": (
                                    f"Ritorno mercanti {who}".strip()
                                ),
                                "started_at": arr2,
                                "arrives_at": arr2 + secs2,
                                "details": {"resources": {}},
                            }
                        )
                if synced:
                    rs.sync_categories(
                        village_key,
                        synced,
                        frozenset({CAT_MERCHANT}),
                    )
                logger.info(
                    "Radar: villaggio '%s' — %d righe 'Mercanti in "
                    "viaggio' lette, %d vettori sincronizzati",
                    village_key, len(rows), len(synced),
                )
        except Exception as _mkt_exc:  # noqa: BLE001
            logger.warning(
                "Sync radar tabella mercanti fallita: %s", _mkt_exc
            )

    def _building_path_for(self, slot_id: int) -> str:
        """Path ``?id=<slot>`` coerente col routing rilevato del server.

        Mirror del sensore: profilo 'modern' -> ``building?id=N``,
        altrimenti ``build.php?id=N`` (i server moderni reindirizzano).
        """
        try:
            page = getattr(self.browser, "page", None)
            prof = profile_for_url(
                (page.url if page is not None else "")
                or self.settings.server_url
            )
            if prof.routing_mode == "modern":
                return f"building?id={int(slot_id)}"
        except Exception:  # noqa: BLE001
            pass
        return f"{selectors.PATH_BUILD}?id={int(slot_id)}"

    async def _research_unit(
        self, action: Action, village_id: int = 0
    ) -> ActionResult:
        """Avvia la ricerca di un'unita' in Accademia (gid 22).

        ``params``: ``academy_slot`` (slot dorf2 dell'Accademia),
        ``unit`` (chiave 'tN'/'uN' o nome) e ``label`` opzionale. Apre
        la pagina dello slot con URL compatibile col routing rilevato,
        individua la riga dell'unita' (icona 'unit uN' o testo label),
        clicca il pulsante 'Ricerca' e verifica la comparsa del timer
        di ricerca attiva.
        """
        params = action.params or {}
        try:
            academy_slot = int(
                params.get("academy_slot") or params.get("slot_id") or 0
            )
        except (TypeError, ValueError):
            academy_slot = 0
        if academy_slot <= 0:
            return ActionResult(
                False, "slot Accademia mancante (academy_slot)"
            )
        unit = str(
            params.get("unit") or params.get("unit_code") or ""
        ).strip()
        unit_label = str(
            params.get("label") or params.get("unit_label") or ""
        ).strip()
        if not unit and not unit_label:
            return ActionResult(
                False, "unita' da ricercare mancante (unit)"
            )

        page = self.browser.page
        assert page is not None

        url = self._building_path_for(academy_slot)
        if village_id > 0:
            url += f"&newdid={village_id}"
        await self.browser.goto(url)
        await sleep_human(0.8, 1.4)

        outcome = await page.evaluate(
            """
            (args) => {
                const want = String(args.unit || '')
                    .toLowerCase().trim();
                const wantLabel = String(args.label || '')
                    .toLowerCase().trim();
                const dm = want.match(/^[ut](\\d{1,2})$/);
                const num = dm ? parseInt(dm[1], 10) : 0;
                const RE = new RegExp(
                    'ricerca|ricerc|research|erforsch|recherche|' +
                    'estudia|estudi', 'i');
                const scope = document.querySelector(
                    '#content, #build, .buildingDetails, .content, main'
                ) || document.body;
                const enabledCtl = (row) => Array.from(
                    row.querySelectorAll(
                        'button, a[href], input[type="submit"], ' +
                        'input[type="button"], [role="button"]'
                    )
                ).filter(c => !(
                    c.disabled ||
                    c.getAttribute('disabled') !== null ||
                    /\\b(disabled|grey|inactive)\\b/i
                        .test(c.className || '') ||
                    c.getAttribute('aria-disabled') === 'true'
                ));
                const researchCtl = (row) => enabledCtl(row).find(c =>
                    RE.test(String(c.innerText || c.value || '')) ||
                    /[?&](a|t)=\\d+/.test(c.getAttribute('href') || '')
                );
                const rows = [];
                for (const icon of scope.querySelectorAll(
                    '[class*="unit u"], [class*="unit_u"], ' +
                    'img.unit, i.unit, span.unit'
                )) {
                    const m = (icon.getAttribute('class') || '')
                        .match(/\\bu(\\d{1,2})\\b/);
                    if (!m) continue;
                    const uN = parseInt(m[1], 10);
                    const row = icon.closest(
                        'tr, li, .research, .troop, .unitRow, ' +
                        '.details, .item, .researchRow'
                    ) || icon.closest('div');
                    if (row) rows.push({uN, row});
                }
                let found = false;
                for (const {uN, row} of rows) {
                    if (num) {
                        if (uN !== num) continue;
                    } else if (wantLabel) {
                        const txt = (row.innerText || '').toLowerCase();
                        if (!txt.includes(wantLabel)) continue;
                    } else {
                        continue;
                    }
                    found = true;
                    const ctl = researchCtl(row);
                    if (!ctl) {
                        return {
                            ok: false,
                            why: 'pulsante Ricerca assente o ' +
                                'disabilitato (risorse/prerequisiti)',
                        };
                    }
                    ctl.click();
                    return {ok: true, u: uN};
                }
                if (!found) {
                    return {
                        ok: false,
                        why: 'riga unita non trovata in Accademia',
                    };
                }
                return {ok: false, why: 'ricerca non avviabile'};
            }
            """,
            {"unit": unit, "label": unit_label},
        ) or {}

        if not outcome.get("ok"):
            return ActionResult(
                False,
                str(outcome.get("why") or "ricerca non avviabile"),
            )

        await sleep_human(1.0, 1.6)

        # Verifica: il timer della ricerca deve comparire nell'Accademia.
        try:
            active = await page.evaluate(
                """
                () => {
                    const scope = document.querySelector(
                        '#content, #build, .buildingDetails, ' +
                        '.content, main'
                    ) || document.body;
                    return !!scope.querySelector(
                        '.timer, .dur, .clocks, [class*="timer" i]'
                    );
                }
                """
            )
        except Exception:  # noqa: BLE001
            active = False
        unit_ref = unit_label or unit
        if not active:
            logger.warning(
                "Accademia: click ricerca %s eseguito ma timer "
                "non rilevato (pagina %s)",
                unit_ref, (page.url or "")[:120],
            )
            return ActionResult(
                False,
                f"ricerca {unit_ref}: timer non rilevato dopo il click",
            )
        logger.info(
            "Accademia: ricerca avviata per %s (slot %s, villaggio %s)",
            unit_ref, academy_slot, village_id,
        )
        return ActionResult(
            True, f"ricerca avviata in Accademia: {unit_ref}"
        )

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
        spent: dict[str, int] = {}
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
            costs_by_input: dict[str, dict] = {}
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
                costs_by_input[input_name] = costs
                for key in ("wood", "clay", "iron", "crop"):
                    cost = int(costs.get(key) or 0)
                    budget[key] -= cost * int(affordable)

            if not fills:
                continue

            filled_ok = False
            slot_trained: list[str] = []
            slot_spent: dict[str, int] = {}
            for input_name, qty, label in fills:
                try:
                    await page.locator(
                        f"input[name='{input_name}']"
                    ).first.fill(str(qty), timeout=2000)
                    remaining[input_name] = remaining.get(input_name, 0) - qty
                    if remaining[input_name] <= 0:
                        remaining.pop(input_name, None)
                    slot_trained.append(f"{qty}x {label}")
                    for key, ucost in (costs_by_input.get(input_name) or {}).items():
                        slot_spent[key] = slot_spent.get(key, 0) + (
                            int(ucost or 0) * qty
                        )
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
                trained.extend(slot_trained)
                for key, amount in slot_spent.items():
                    spent[key] = spent.get(key, 0) + amount
                logger.info(
                    "Replenishment: addestramento avviato nello slot %s "
                    "(villaggio %s): %s",
                    trainer_slot, village_id, ", ".join(slot_trained),
                )

        if trained:
            return ActionResult(
                True,
                f"rimpiazzo avviato: {', '.join(trained)}",
                data={"spent": spent},
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
        self,
        page,
        selectors_list: list[str],
        timeout_ms: int = 1500,
        profile_key: str | None = None,
    ) -> bool:
        """Clicca il primo selettore presente, visibile e non disabilitato.

        Con ``profile_key`` la lista viene riordinata dal profilo del
        server (il selettore promosso vince) e un match a indice > 0
        promuove il selettore vincente per i cicli successivi.
        """
        if profile_key:
            ordered = self.profile.ordered_selectors(
                profile_key, selectors_list
            )
        else:
            ordered = list(selectors_list)
        for _idx, sel in enumerate(ordered):
            locator = page.locator(sel).first
            try:
                if await locator.count() == 0:
                    continue
                if not await locator.is_visible():
                    continue
                disabled = await locator.get_attribute("disabled", timeout=timeout_ms)
                css_class = (await locator.get_attribute("class", timeout=timeout_ms)) or ""
                if disabled is None and "disabled" not in css_class:
                    await locator.click(timeout=timeout_ms)
                    if profile_key and _idx > 0:
                        self.profile.promote_selector(profile_key, sel)
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
        self,
        village_id: int = 0,
        village_name: str = "",
        claim_ctx: dict | None = None,
    ) -> int:
        """Apre il pannello task del villaggio attivo e riscuote i premi.

        Flusso:
        1. click sull'icona questmaster (#questmasterButton) se presente
           e riscossione premi nel dialog;
        2. pagine dedicate UI moderna `/tasks?t=village` e
           `/tasks?t=general`, poi legacy `questmaster.php`: in ognuna si
           scansionano i premi (contenuto [L,A,F,G] + XP/PC/truppe) e si
           clicca 'Raccogli'/'Collect'/'Claim' solo su quelli che
           passano la policy;
        3. bottoni disabilitati (magazzino pieno) vengono saltati senza
           errore: si riprova al ciclo successivo;
        4. chiusura del dialog.

        ``claim_ctx`` attiva il riscatto ON-DEMAND (spec 4): i premi sono
        una riserva non depredabile — ``task_reward_claimable`` li
        incassa solo se sbloccano un obiettivo pendente senza overflow e
        senza attacchi in arrivo. ``None`` = modalita' legacy (riscuote
        tutto, per chiamanti storici).

        Ritorna il numero di premi effettivamente riscossi.
        """
        if village_id > 0:
            await self._ensure_village(village_id)

        page = self.browser.page
        assert page is not None

        claimed = 0
        skipped = 0
        held = 0

        # 0. Gate sul badge di notifica: se l'avatar del Questmaster non
        # segnala task completate (indicatore assente o azzerato) si evita
        # la navigazione sulle pagine /tasks (spec 4). Indicatore non
        # rilevato = stato ignoto: si procede comunque con la scansione.
        try:
            badge = await self._tasks_pending_badge(page)
            if badge is False:
                logger.debug(
                    "Task villaggio '%s': nessun badge sul Questmaster, "
                    "scansione saltata",
                    village_name or village_id or "attivo",
                )
                return 0
        except Exception:  # noqa: BLE001
            pass

        # newdid preservato nelle URL delle pagine task: in T4.6 i premi
        # sono localizzati per insediamento e la navigazione nuda su
        # tasks?t=... tornerebbe al villaggio default di sessione.
        newdid_q = f"&{selectors.PARAM_NEWDID}={village_id}" if village_id > 0 else ""

        # 1. Widget task (icona questmaster), se il server la espone
        opened = await self._click_first(
            page, selectors.QUEST_MASTER_BUTTON, timeout_ms=2000
        )
        if opened:
            await sleep_human(1.0, 1.8)
            n, h, s = await self._claim_task_rewards_on_page(page, claim_ctx)
            claimed += n
            held = max(held, h)
            skipped = max(skipped, s)

        # 2. Pagine task dedicate (UI moderna) + legacy questmaster.php
        for path in (
            selectors.PATH_TASKS_VILLAGE + newdid_q,
            selectors.PATH_TASKS_GENERAL + newdid_q,
            selectors.PATH_QUESTMASTER + (f"?{selectors.PARAM_NEWDID}={village_id}" if village_id > 0 else ""),
        ):
            # Task generali (spec 4): riscuote solo con almeno il 30% di
            # spazio libero in magazzino E granaio — i premi globali non
            # devono andare in overflow su un villaggio quasi pieno.
            if path.startswith(selectors.PATH_TASKS_GENERAL):
                try:
                    if not await self._storage_has_headroom(page, 0.30):
                        logger.info(
                            "Task generali saltate in '%s': meno del 30%% "
                            "di spazio libero in magazzino/granaio",
                            village_name or village_id or "attivo",
                        )
                        continue
                except Exception:  # noqa: BLE001
                    pass
            try:
                await self.browser.goto(path)
                await sleep_human(0.8, 1.2)
            except Exception:  # noqa: BLE001 — pagina assente su questo server
                continue
            n, h, s = await self._claim_task_rewards_on_page(page, claim_ctx)
            claimed += n
            held = max(held, h)
            skipped = max(skipped, s)

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
        if held:
            logger.info(
                "Task villaggio '%s': %d premi trattenuti come riserva "
                "protetta (on-demand: nessun obiettivo sbloccabile)",
                village_name or village_id or "attivo",
                held,
            )
        return claimed

    async def _scan_task_rewards(self, page) -> dict:
        """Inventario dei premi cliccabili nella vista task corrente.

        Per ogni pulsante 'Raccogli'/'Collect'/'Claim' legge il contenuto
        del premio SENZA cliccarlo: quantita' per risorsa, flag
        non-risorsa (XP eroe, Punti Cultura, truppe) e stato
        enabled/disabled. Legge anche stock e capienze dalla stock bar
        globale, usate dai check di overflow della policy on-demand.
        Ogni pulsante viene taggato con ``data-tk-reward`` per il click
        selettivo di ``_click_task_reward``.
        """
        res = await page.evaluate(
            """
            () => {
                const toInt = (s) => {
                    const m = String(s || '')
                        .replace(/[\\u2212\\u2010-\\u2015]/g, '-')
                        .match(/-?[\\d.\\s']+/);
                    if (!m) return 0;
                    return parseInt(m[0].replace(/[^0-9-]/g, ''), 10) || 0;
                };
                const capOf = (sels) => {
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (!el) continue;
                        const blob = [
                            el.getAttribute('title') || '',
                            el.innerText || '',
                        ].join(' ');
                        const m = blob.match(
                            /(\\d[\\d.\\s]*)[|/]{1,2}\\s*(\\d[\\d.\\s]*)/
                        );
                        if (m) return toInt(m[2]);
                    }
                    return 0;
                };
                const stock = {
                    wood: toInt((document.querySelector('#l1 .value, #l1') || {}).innerText),
                    clay: toInt((document.querySelector('#l2 .value, #l2') || {}).innerText),
                    iron: toInt((document.querySelector('#l3 .value, #l3') || {}).innerText),
                    crop: toInt((document.querySelector('#l4 .value, #l4') || {}).innerText),
                };
                const claimRx =
                    /(raccogl|riscuot|collect|claim|récupér|belohnung)/i;
                const skipRx = /(chiudi|close|cancel|annulla|abbrechen)/i;
                const rowRx =
                    /task|quest|reward|mission|achievement|objective/i;
                const resCls = {
                    wood: /\\br1\\b|wood|lumber|legno|holz|bois|madera/i,
                    clay: /\\br2\\b|clay|argilla|lehm|arcilla/i,
                    iron: /\\br3\\b|iron|ferro|eisen|hierro/i,
                    crop: /\\br4\\b|crop|grain|grano|wheat|getreide|bl[eé]|trigo|cereal/i,
                };
                const scopes = ['#tasks', '.tasks', '#dialogContent',
                                '.quest', '.task', '#content', 'body'];
                document.querySelectorAll('[data-tk-reward]').forEach(
                    (e) => e.removeAttribute('data-tk-reward')
                );
                const seen = new Set();
                const buttons = [];
                for (const scope of scopes) {
                    for (const root of document.querySelectorAll(scope)) {
                        root.querySelectorAll(
                            'button, a, input[type="submit"], input[type="button"]'
                        ).forEach((el) => {
                            if (seen.has(el)) return;
                            const text = (el.innerText || el.value ||
                                          el.textContent || '').trim();
                            if (!text || !claimRx.test(text) ||
                                skipRx.test(text)) return;
                            seen.add(el);
                            buttons.push(el);
                        });
                    }
                }
                const rewards = [];
                buttons.forEach((el, i) => {
                    el.setAttribute('data-tk-reward', String(i));
                    const cls = (el.className || '').toLowerCase();
                    const disabled = !!(
                        el.disabled ||
                        el.getAttribute('aria-disabled') === 'true' ||
                        cls.includes('disabled') ||
                        cls.includes('inactive') ||
                        cls.includes('grey') || cls.includes('gray')
                    );
                    // Riga premio: primo antenato con classe task/quest;
                    // fallback sul genitore diretto del pulsante.
                    let row = el.parentElement;
                    let depth = 0;
                    while (row && row !== document.body && depth < 7) {
                        const rc = row.className;
                        if (typeof rc === 'string' && rowRx.test(rc)) break;
                        if (row.id === 'content') break;
                        row = row.parentElement;
                        depth++;
                    }
                    if (!row || row === document.body) row = el.parentElement;
                    const rowText = (row && row.innerText) || '';
                    const resources = {wood: 0, clay: 0, iron: 0, crop: 0};
                    if (row && row.querySelectorAll) {
                        for (const [res, rx] of Object.entries(resCls)) {
                            for (const r of row.querySelectorAll(
                                '[class], img[alt], [title]'
                            )) {
                                const marker = [
                                    (typeof r.className === 'string'
                                        ? r.className : ''),
                                    r.getAttribute('alt') || '',
                                    r.getAttribute('title') || '',
                                ].join(' ');
                                if (!rx.test(marker)) continue;
                                const near = [
                                    r.innerText || '',
                                    (r.nextSibling &&
                                        r.nextSibling.textContent) || '',
                                    (r.parentElement &&
                                        r.parentElement.innerText) || '',
                                ];
                                for (const t of near) {
                                    if (t.length > 40) continue;
                                    const n = toInt(t);
                                    if (n > 0) {
                                        resources[res] = Math.max(
                                            resources[res], n
                                        );
                                        break;
                                    }
                                }
                            }
                        }
                    }
                    // Fallback testuale: '200 Legno' oppure 'Legno: 200'.
                    const resTxt = {
                        wood: /(legno|wood|lumber|holz|bois|madera)/i,
                        clay: /(argilla|clay|lehm|arcilla)/i,
                        iron: /(ferro|iron|eisen|hierro)/i,
                        crop: /(grano|crop|grain|wheat|getreide|bl[eé]|trigo|cereal)/i,
                    };
                    for (const [res, rx] of Object.entries(resTxt)) {
                        if (resources[res] > 0) continue;
                        const m1 = rowText.match(new RegExp(
                            "(\\d[\\d.\\s']*)\\s*" + rx.source));
                        const m2 = rowText.match(new RegExp(
                            rx.source + "[^0-9]{0,6}(\\d[\\d.\\s']*)"));
                        const n = toInt(m1 && m1[1]) || toInt(m2 && m2[1]);
                        if (n > 0) resources[res] = n;
                    }
                    const blob = rowText + ' ' + (
                        (row && row.getAttribute &&
                            row.getAttribute('title')) || '');
                    rewards.push({
                        idx: i,
                        text: (el.innerText || el.value || '')
                            .trim().slice(0, 40),
                        disabled: disabled,
                        resources: resources,
                        xp: /xp|experien|esperienza|erfahrung|exp[ée]rience/i
                            .test(blob),
                        cp: /cultur|kultur/i.test(blob),
                        troops: /truppe|troops|truppen|tropas|soldat/i
                            .test(blob),
                    });
                });
                return {
                    rewards: rewards,
                    stock: stock,
                    wh_cap: capOf([
                        '#stockBar .warehouse',
                        '.stockBarButton.warehouse',
                        'li.warehouse', '.warehouse']),
                    gr_cap: capOf([
                        '#stockBar .granary',
                        '.stockBarButton.granary',
                        'li.granary', '.granary']),
                };
            }
            """
        )
        return res if isinstance(res, dict) else {}

    async def _click_task_reward(self, page, idx: int) -> bool:
        """Clicca il pulsante premio taggato dall'ultima scansione."""
        try:
            return bool(
                await page.evaluate(
                    """
                    (i) => {
                        const el = document.querySelector(
                            '[data-tk-reward="' + i + '"]'
                        );
                        if (!el) return false;
                        el.click();
                        return true;
                    }
                    """,
                    idx,
                )
            )
        except Exception:  # noqa: BLE001
            return False

    async def _claim_task_rewards_on_page(
        self, page, claim_ctx: dict | None
    ) -> tuple[int, int, int]:
        """Riscuote i premi della vista corrente secondo la policy.

        ``claim_ctx=None`` -> modalita' legacy: clicca ogni premio
        abilitato (chiamanti storici). Con contesto il riscatto e'
        on-demand: ``task_reward_claimable`` decide premio per premio e
        dopo ogni riscatto la pagina viene ri-scansionata, perche' stock
        e contenuti cambiano (catene di task incluse).

        Ritorna (claimed, held, skipped): held = premi abilitati ma
        trattenuti dalla policy, skipped = pulsanti disabilitati
        (magazzino pieno).
        """
        claimed = 0
        held = 0
        skipped = 0
        legacy = claim_ctx is None
        for _ in range(8):
            scan = await self._scan_task_rewards(page)
            rewards = scan.get("rewards") or []
            enabled = [r for r in rewards if not r.get("disabled")]
            skipped = max(
                skipped, sum(1 for r in rewards if r.get("disabled"))
            )
            if not enabled:
                break
            live_ctx = dict(claim_ctx or {})
            live_ctx["stock"] = scan.get("stock") or {}
            live_ctx["wh_cap"] = int(scan.get("wh_cap") or 0)
            live_ctx["gr_cap"] = int(scan.get("gr_cap") or 0)
            pick = next(
                (
                    r
                    for r in enabled
                    if legacy or task_reward_claimable(r, live_ctx)
                ),
                None,
            )
            if pick is None:
                held = len(enabled)
                break
            if not await self._click_task_reward(
                page, int(pick.get("idx") or 0)
            ):
                break
            claimed += 1
            await sleep_human(0.5, 0.9)
        return claimed, held, skipped

    async def _tasks_pending_badge(self, page) -> bool | None:
        """Indicatore di task completate sull'avatar del Questmaster.

        Ritorna True (badge con pending), False (Questmaster presente ma
        nessun badge/numero — navigazione /tasks evitabile), None quando
        l'indicatore non e' rilevabile su questo server (stato ignoto).
        """
        try:
            res = await page.evaluate(
                """
                (sels) => {
                    const qm = document.querySelector(
                        '#questmasterButton, .questmasterButton, ' +
                        'a[href*="tasks"], #questmaster a'
                    );
                    if (!qm) return { found: false, pending: false };
                    for (const s of sels) {
                        for (const el of document.querySelectorAll(s)) {
                            if (el.offsetParent === null && el.offsetWidth === 0)
                                continue;
                            const num = parseInt(
                                (el.innerText || el.textContent || '')
                                    .replace(/[^0-9]/g, '') || '0', 10);
                            // Badge visibile: con numero > 0 o comunque
                            // presente come marcatori grafico.
                            if (num > 0) return { found: true, pending: true };
                            return { found: true, pending: false };
                        }
                    }
                    return { found: false, pending: false };
                }
                """,
                selectors.QUEST_PENDING_BADGE,
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(res, dict) or not res.get("found"):
            return None
        return bool(res.get("pending"))

    async def _storage_has_headroom(
        self, page, min_free_ratio: float = 0.30
    ) -> bool:
        """True se magazzino E granaio hanno >= min_free_ratio di spazio libero.

        Legge la stock bar (#l1..l4 + capacita' dai tooltip warehouse/
        granary). Dato non leggibile -> True (non si blocca la raccolta
        su un layout sconosciuto).
        """
        try:
            data = await page.evaluate(
                """
                () => {
                    const toInt = (s) => {
                        const m = String(s || '')
                            .replace(/[\\u2212\\u2010-\\u2015]/g, '-')
                            .match(/-?[\\d.\\s']+/);
                        if (!m) return 0;
                        return parseInt(m[0].replace(/[^0-9-]/g, ''), 10) || 0;
                    };
                    const capOf = (sels) => {
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            if (!el) continue;
                            const blob = [
                                el.getAttribute('title') || '',
                                el.innerText || '',
                            ].join(' ');
                            // Tooltip tipico: "3300||8000" oppure "…/8000"
                            const m = blob.match(/(\\d[\\d.\\s]*)[|/]{1,2}\\s*(\\d[\\d.\\s]*)/);
                            if (m) return toInt(m[2]);
                        }
                        return 0;
                    };
                    return {
                        wood: toInt((document.querySelector('#l1 .value, #l1') || {}).innerText),
                        clay: toInt((document.querySelector('#l2 .value, #l2') || {}).innerText),
                        iron: toInt((document.querySelector('#l3 .value, #l3') || {}).innerText),
                        crop: toInt((document.querySelector('#l4 .value, #l4') || {}).innerText),
                        wh_cap: capOf(['#stockBar .warehouse', '.stockBarButton.warehouse',
                                       'li.warehouse', '.warehouse']),
                        gr_cap: capOf(['#stockBar .granary', '.stockBarButton.granary',
                                       'li.granary', '.granary']),
                    };
                }
                """
            )
        except Exception:  # noqa: BLE001
            return True
        if not isinstance(data, dict):
            return True
        wh_cap = int(data.get("wh_cap") or 0)
        gr_cap = int(data.get("gr_cap") or 0)
        if wh_cap <= 0 or gr_cap <= 0:
            return True
        used_wh = max(
            int(data.get("wood") or 0),
            int(data.get("clay") or 0),
            int(data.get("iron") or 0),
        )
        used_gr = int(data.get("crop") or 0)
        free_wh = (wh_cap - used_wh) / wh_cap
        free_gr = (gr_cap - used_gr) / gr_cap
        return free_wh >= min_free_ratio and free_gr >= min_free_ratio


# ---------------------------------------------------------------------------
# Riscatto premi on-demand (spec 4): i premi task sono una riserva non
# depredabile — si incassano solo quando sbloccano una spesa concreta.
# ---------------------------------------------------------------------------

# Margine anti-overflow: nessuna risorsa puo' superare il 92% della capienza.
TASK_CLAIM_CAP_RATIO = 0.92

_RES_KEYS = ("wood", "clay", "iron", "crop")


def task_reward_claimable(reward: dict, ctx: dict) -> bool:
    """True se il premio scansionato va riscosso ORA.

    ``reward``: riga di ``_scan_task_rewards`` ({resources, xp, cp,
    troops, disabled}). ``ctx``: contesto decisionale — ``stock``,
    ``wh_cap`` e ``gr_cap`` (lettura live dalla pagina task),
    ``target_costs`` (lista di costi {wood..crop} degli obiettivi
    pendenti del villaggio), ``threat_incoming`` (attacco entro la
    finestra anti-raid), ``crop_balance`` (bilancio grano netto,
    None = non letto).

    Regole:
    1. Premi non-risorsa (XP, PC, oggetti): sempre liberi; premi-truppe
       solo se il bilancio grano regge il nuovo upkeep;
    2. Anti-raid: minaccia in arrivo -> i premi-risorse restano protetti
       nell'interfaccia task fino a pericolo cessato;
    3. Zero overflow: per ogni risorsa assegnata, stock+premio <= 92%
       della capienza;
    4. Trigger di utilita': un obiettivo NON pagabile con lo stock
       diventa pagabile sommando il premio (spesa nello stesso ciclo,
       subito dopo il riscatto).
    """
    res = reward.get("resources") or {}
    res_total = sum(int(res.get(k) or 0) for k in _RES_KEYS)
    if res_total <= 0:
        if reward.get("troops"):
            cb = ctx.get("crop_balance")
            if cb is not None and int(cb) < 0:
                return False
        return True
    if ctx.get("threat_incoming"):
        return False
    stock = ctx.get("stock") or {}
    wh_cap = int(ctx.get("wh_cap") or 0)
    gr_cap = int(ctx.get("gr_cap") or 0)
    for r in _RES_KEYS:
        add = int(res.get(r) or 0)
        if add <= 0:
            continue
        cap = gr_cap if r == "crop" else wh_cap
        # Capienza non verificabile su una risorsa che il premio
        # assegna: fail-closed, non si rischia l'overflow della riserva.
        if cap <= 0:
            return False
        if int(stock.get(r) or 0) + add > cap * TASK_CLAIM_CAP_RATIO:
            return False
    for cost in ctx.get("target_costs") or []:
        if not cost:
            continue
        payable_now = all(
            int(stock.get(r) or 0) >= int(cost.get(r) or 0)
            for r in _RES_KEYS
        )
        payable_with = all(
            int(stock.get(r) or 0) + int(res.get(r) or 0)
            >= int(cost.get(r) or 0)
            for r in _RES_KEYS
        )
        if not payable_now and payable_with:
            return True
    return False