"""Sensori: estraggono i dati di gioco tramite JavaScript Injection (page.evaluate) e Selettori CSS."""

from __future__ import annotations

import logging
import re
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urljoin

from playwright.async_api import Page

from . import selectors
from .models import (
    BuildQueueItem,
    Field,
    GameState,
    IncomingAttack,
    Resources,
    ResourceType,
    Village,
    safe_int,
    safe_int_opt,
)

logger = logging.getLogger(__name__)

# Dump diagnostici dei parser (testo della pagina quando una lettura
# non produce dati): stessa convenzione di settings.debug_dir.
_DEBUG_DIR = Path(__file__).resolve().parent.parent / "debug"

_DIGITS = re.compile(r"\d+")
_TIMER = re.compile(r"(?:(\d+)\s*[-+]\s*)?(?:(\d+):)?(\d+):(\d{2})")
_SLOT_ID = re.compile(r"build\.php\?[^#\s]*?\bid=(\d+)")
_SLOT_CLASS = re.compile(r"\ba(\d+)\b")
_NEWDID = re.compile(r"newdid=(\d+)")

# parole chiave (multi-lingua) per mappare un campo al suo tipo di risorsa
_RESOURCE_KEYWORDS: dict[ResourceType, tuple[str, ...]] = {
    ResourceType.WOOD: ("wood", "legno", "holz", "bois", "madera", "boschivo"),
    ResourceType.CLAY: ("clay", "argilla", "lehm", "argile", "barro"),
    ResourceType.IRON: ("iron", "ferro", "eisen", "fer", "hierro", "miniera"),
    ResourceType.CROP: (
        "crop",
        "grano",
        "getreide",
        "cereale",
        "wheat",
        "ble",
        "trigo",
        "campo",
    ),
}


def parse_int(text: str | None) -> int:
    """Estrae il primo intero da una stringa tipo '1 234 / 8 000'."""
    if not text:
        return 0
    cleaned = text.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    match = _DIGITS.search(cleaned)
    return int(match.group()) if match else 0


def parse_capacity(text: str | None) -> int:
    """Estrae la capacita' dal tooltip 'corrente||capacita''."""
    if not text:
        return 0
    cleaned = text.replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    if "||" in cleaned:
        cleaned = cleaned.split("||", 1)[1]
    elif "/" in cleaned:
        cleaned = cleaned.split("/", 1)[1]
    return parse_int(cleaned)


def parse_timer(text: str | None) -> int:
    """Converte 'HH:MM:SS', 'MM:SS' o '0:12:34' in secondi."""
    if not text:
        return 0
    match = _TIMER.search(text.strip())
    if not match:
        return 0
    days = int(match.group(1) or 0)
    hours = int(match.group(2) or 0)
    minutes = int(match.group(3) or 0)
    seconds = int(match.group(4) or 0)
    return int(timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds).total_seconds())


def _italian_plurals(word: str) -> list[str]:
    """Plurali possibili di una parola italiana (euristica semplice)."""
    w = str(word or "").strip().lower()
    if len(w) < 2:
        return []
    if w.endswith("io"):
        return [w[:-1], w[:-1] + "i"]  # legionario->legionari(/i)
    if w.endswith("o"):
        return [w[:-1] + "i"]
    if w.endswith("a"):
        return [w[:-1] + "e"]
    if w.endswith("e"):
        return [w[:-1] + "i"]
    if w.endswith("ca") or w.endswith("ga"):
        return [w + "he"]
    return []


def _abs_game_url(current_url: str, path: str) -> str:
    """URL di gioco assoluto dalla root del dominio.

    urljoin(base, 'dorf1.php') con path relativo risolve nella
    sottocartella corrente: dopo una visita al baule eroe la base e'
    /hero/ e la navigazione finisce su /hero/dorf1.php (pagina senza il
    DOM delle truppe). Forzando il leading '/' la destinazione e'
    sempre /dorf1.php qualunque sia la pagina di partenza.
    """
    return urljoin(current_url or "", "/" + str(path).lstrip("/"))


def _troop_label_map() -> dict[str, str]:
    """{'legionario': 'u1', 'legionari': 'u1', ...} per il parser truppe.

    Mappa i nomi localizzati delle unita' (singolare + plurale italiano)
    nelle chiavi icona 'uN', usata dal fallback testuale di
    ``_read_troops_dom`` quando il markup non espone le classi 'unit uN'
    (spec bugfix parser truppe / Potenza Militare=0).
    """
    try:
        from .models import STATIC_TROOP_LABELS  # import ritardato: no cicli
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for tkey, name in STATIC_TROOP_LABELS.items():
        low = str(name or "").strip().lower()
        if not low or not tkey.startswith("t"):
            continue
        ukey = "u" + tkey[1:]
        out[low] = ukey
        # plurale italiano via _italian_plurals:
        # (Legionario->Legionari, Paladino->Paladini,
        #  Catapulta->Catapulte, Falange->Falangi)
        for plural in _italian_plurals(low):
            out[plural] = ukey
        # nomi composti: plurale della prima parola
        # ("catapulta da fuoco" -> "catapulte da fuoco")
        if " " in low:
            first, rest = low.split(" ", 1)
            for plural in _italian_plurals(first):
                out[plural + " " + rest] = ukey
    return out


def guess_resource_type(name: str) -> ResourceType | None:
    lowered = name.lower()
    for res_type, keywords in _RESOURCE_KEYWORDS.items():
        if any(kw in lowered for kw in keywords):
            return res_type
    return None


async def _first_locator(page: Page, selector_list: list[str]):
    for sel in selector_list:
        locator = page.locator(sel)
        try:
            if await locator.count() > 0:
                return locator
        except Exception:  # noqa: BLE001 - selettore non valido
            continue
    return None


async def _text_of(locator, selector_list: list[str]) -> str:
    """Testo del primo sotto-elemento trovato dentro `locator`."""
    for sel in selector_list:
        sub = locator.locator(sel)
        try:
            if await sub.count() > 0:
                return (await sub.first.inner_text()).strip()
        except Exception:  # noqa: BLE001
            continue
    return ""


class TravianParser:
    """Sensore e Parser ad alte prestazioni: JS Injection + selettori CSS."""

    # Suffix delle tab della Residenza/Palazzo provate da
    # read_culture_points: il riepilogo CP vive su una tab diversa a
    # seconda della versione/skin ('' = panoramica di default).
    _CP_TAB_SUFFIXES = ("", "&tt=3", "&tt=2", "&tt=4", "&tt=1")

    def __init__(self, page: Page) -> None:
        self.page = page
        # Tab Residenza che l'ultima volta conteneva il riepilogo CP:
        # evita di riprovare tutte le tab a ogni ciclo.
        self._cp_tab: str | None = None
        # Anti-detection: navigazione via click DOM quando possibile
        # (impostata dall'orchestrator da settings.dom_navigation).
        self.dom_nav: bool = True

    async def _goto(self, url: str, **kwargs) -> None:
        """Naviga verso ``url`` preferendo i click DOM al GET diretto.

        Tenta prima ``navigate_via_dom`` (sidebar newdid, slot mappa,
        navbar); se non copre il caso ripiega su ``page.goto``:
        la resilienza viene sempre prima del realismo.
        """
        if self.dom_nav:
            try:
                from .browser import navigate_via_dom
                if await navigate_via_dom(self.page, url):
                    return
            except Exception:
                pass
        kwargs.setdefault("wait_until", "domcontentloaded")
        await self.page.goto(url, **kwargs)

    async def _dump_page_text(self, tag: str) -> None:
        """Salva URL + innerText in debug/<tag>.txt per diagnosi parser."""
        try:
            _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
            txt = await self.page.evaluate(
                "() => (document.body && document.body.innerText) || ''"
            )
            (_DEBUG_DIR / f"{tag}.txt").write_text(
                f"URL: {self.page.url}\n\n{txt}", encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    async def get_village_state(self) -> Dict[str, Any]:
        """Raccoglie lo stato completo del villaggio corrente in formato dizionario."""
        resources = await self.extract_resources()
        building_queue = await self.extract_building_queue()
        incoming_attacks = await self.check_incoming_attacks()
        village_name = await self.extract_village_name()

        return {
            "resources": resources,
            "building_queue": building_queue,
            "incoming_attacks": incoming_attacks,
            "village_name": village_name,
        }

    async def extract_resources(self) -> Dict[str, Dict[str, int]]:
        """
        Estrae le risorse attuali e i limiti dei magazzini tramite JavaScript Injection.
        Supporta selettori T4.4, T4.5, T4.6 e layout internazionali moderni.
        """
        try:
            res_data: dict = await self.page.evaluate("""
                () => {
                    const cleanNum = (text) => {
                        if (!text) return 0;
                        let raw = text.replace(/[\u202f\u00a0]/g, ' ').trim();
                        if (raw.includes('/')) raw = raw.split('/')[0];
                        if (raw.includes('||')) raw = raw.split('||')[0];
                        const match = raw.match(/\\d[\\d\\s.,']*/);
                        if (!match) return 0;
                        const digitsOnly = match[0].replace(/[^\\d]/g, '');
                        return parseInt(digitsOnly, 10) || 0;
                    };

                    const parseCap = (text) => {
                        if (!text) return 0;
                        let raw = text.replace(/[\u202f\u00a0]/g, ' ').trim();
                        if (raw.includes('||')) raw = raw.split('||')[1];
                        else if (raw.includes('/')) raw = raw.split('/')[1];
                        const match = raw.match(/\\d[\\d\\s.,']*/);
                        if (!match) return 0;
                        const digitsOnly = match[0].replace(/[^\\d]/g, '');
                        return parseInt(digitsOnly, 10) || 0;
                    };

                    const getResValue = (selectors) => {
                        for (const sel of selectors) {
                            try {
                                const el = document.querySelector(sel);
                                if (el) {
                                    const val = cleanNum(el.innerText || el.textContent || el.getAttribute('value') || '');
                                    if (val > 0) return val;
                                    const raw = (el.innerText || el.textContent || '').trim();
                                    if (raw === '0') return 0;
                                }
                            } catch (e) {}
                        }
                        return 0;
                    };

                    // Legno (Wood / Lumber)
                    const wood = getResValue([
                        '#stockBar #l1 .value', '#stockBar #l1', '#l1 .value', '#l1', 'span#l1', 'li#l1',
                        '#stockBarResource1 .value', '#stockBarResource1', '#stockBar .stockBarButton.resource1 .value',
                        '#stockBar .resource1 .value', '#stockBar .wood .value', '#stockBar .r1 .value',
                        '.stockBarButton.wood .value', '.stockBarButton.resource1 .value', '.wood .value', '.resource1 .value',
                        '#stockBar [class*="wood"] .value', '#stockBar [class*="resource1"] .value',
                        '#stockBar #stockBarResources li:nth-child(1) .value'
                    ]);

                    // Argilla (Clay)
                    const clay = getResValue([
                        '#stockBar #l2 .value', '#stockBar #l2', '#l2 .value', '#l2', 'span#l2', 'li#l2',
                        '#stockBarResource2 .value', '#stockBarResource2', '#stockBar .stockBarButton.resource2 .value',
                        '#stockBar .resource2 .value', '#stockBar .clay .value', '#stockBar .r2 .value',
                        '.stockBarButton.clay .value', '.stockBarButton.resource2 .value', '.clay .value', '.resource2 .value',
                        '#stockBar [class*="clay"] .value', '#stockBar [class*="resource2"] .value',
                        '#stockBar #stockBarResources li:nth-child(2) .value'
                    ]);

                    // Ferro (Iron)
                    const iron = getResValue([
                        '#stockBar #l3 .value', '#stockBar #l3', '#l3 .value', '#l3', 'span#l3', 'li#l3',
                        '#stockBarResource3 .value', '#stockBarResource3', '#stockBar .stockBarButton.resource3 .value',
                        '#stockBar .resource3 .value', '#stockBar .iron .value', '#stockBar .r3 .value',
                        '.stockBarButton.iron .value', '.stockBarButton.resource3 .value', '.iron .value', '.resource3 .value',
                        '#stockBar [class*="iron"] .value', '#stockBar [class*="resource3"] .value',
                        '#stockBar #stockBarResources li:nth-child(3) .value'
                    ]);

                    // Grano (Crop)
                    const crop = getResValue([
                        '#stockBar #l4 .value', '#stockBar #l4', '#l4 .value', '#l4', 'span#l4', 'li#l4',
                        '#stockBarResource4 .value', '#stockBarResource4', '#stockBar .stockBarButton.resource4 .value',
                        '#stockBar .resource4 .value', '#stockBar .crop .value', '#stockBar .r4 .value',
                        '.stockBarButton.crop .value', '.stockBarButton.resource4 .value', '.crop .value', '.resource4 .value',
                        '#stockBar [class*="crop"] .value', '#stockBar [class*="resource4"] .value',
                        '#stockBar #stockBarResources li:nth-child(4) .value'
                    ]);

                    // Capienza Magazzino (Warehouse)
                    const whCapEl = document.querySelector(
                        '#stockBar .warehouse .capacity .value, #stockBar .warehouse .capacity, #stockBar .warehouse .value, .warehouse .capacity, .warehouse .value, #stockBarWarehouse .value, #stockBarWarehouse'
                    );
                    const whEl = document.querySelector('#stockBar .warehouse, #stockBarWarehouse, .warehouse, .stockBarButton.warehouse, [id*="warehouse" i]');
                    let warehouse = cleanNum(whCapEl?.innerText || whCapEl?.textContent || '');
                    if (!warehouse && whEl) {
                        warehouse = parseCap(whEl.getAttribute('title') || whEl.getAttribute('aria-label') || whEl.innerText || whEl.textContent || '0');
                    }

                    // Capienza Granaio (Granary)
                    const grCapEl = document.querySelector(
                        '#stockBar .granary .capacity .value, #stockBar .granary .capacity, #stockBar .granary .value, .granary .capacity, .granary .value, #stockBarGranary .value, #stockBarGranary'
                    );
                    const grEl = document.querySelector('#stockBar .granary, #stockBarGranary, .granary, .stockBarButton.granary, [id*="granary" i]');
                    let granary = cleanNum(grCapEl?.innerText || grCapEl?.textContent || '');
                    if (!granary && grEl) {
                        granary = parseCap(grEl.getAttribute('title') || grEl.getAttribute('aria-label') || grEl.innerText || grEl.textContent || '0');
                    }

                    return {
                        current: { wood, clay, iron, crop },
                        capacity: { warehouse, granary }
                    };
                }
            """)
            if res_data and isinstance(res_data, dict):
                return res_data
        except Exception as exc:
            logger.debug("JS evaluate per risorse fallito, ricado su parser DOM: %s", exc)

        # Fallback Selettori DOM
        res = await self._read_resources()
        return {
            "current": {
                "wood": res.wood,
                "clay": res.clay,
                "iron": res.iron,
                "crop": res.crop,
            },
            "capacity": {
                "warehouse": res.warehouse_capacity,
                "granary": res.granary_capacity,
            },
        }

    async def extract_building_queue(self) -> List[Dict[str, str]]:
        """Estrae gli edifici o i campi attualmente in costruzione e i relativi timer."""
        try:
            queue: list = await self.page.evaluate("""
                () => {
                    const items = [];
                    // Cerca il contenitore della coda di costruzione (.buildingList, #buildingContract, etc.)
                    const queueElements = document.querySelectorAll('.buildingList li, #buildingContract .buildingRow, .buildingsOverview .buildingRow, table#building_contract tr');

                    queueElements.forEach(el => {
                        const nameEl = el.querySelector('.name, .building, img[alt]');
                        let name = nameEl?.innerText?.trim() || nameEl?.getAttribute('alt')?.trim() || '';
                        const durationEl = el.querySelector('.buildDuration, .timer, .dur, .clocks');
                        let duration = durationEl?.innerText?.trim() || '';
                        // Slot di origine dal link build.php?id=N (se la riga lo contiene)
                        const link = el.querySelector('a[href*="build.php?id="]') || el.closest('a[href*="build.php?id="]');
                        let slotId = null;
                        if (link) {
                            const m = (link.getAttribute('href') || '').match(/build\\.php[?&]id=(\\d+)/);
                            if (m) slotId = parseInt(m[1], 10);
                        }
                        if (name) {
                            items.push({ building: name, time_left: duration, slot_id: slotId });
                        }
                    });
                    return items;
                }
            """)
            if isinstance(queue, list):
                return queue
        except Exception as exc:
            logger.debug("JS evaluate per coda costruzione fallito: %s", exc)

        # Fallback DOM locator (non richiama read_build_queue: ricorsione)
        for sel in (
            ".buildingList li",
            "#buildingContract .buildingRow",
            ".buildingsOverview .buildingRow",
            "table#building_contract tr",
        ):
            rows = self.page.locator(sel)
            try:
                count = await rows.count()
            except Exception:
                continue
            if count == 0:
                continue
            for i in range(count):
                try:
                    row = rows.nth(i)
                    name_el = row.locator(".name, .building, img[alt]").first
                    name = ""
                    if await name_el.count() > 0:
                        name = (
                            (await name_el.inner_text()).strip()
                            or (await name_el.get_attribute("alt") or "").strip()
                        )
                    dur_el = row.locator(".buildDuration, .timer, .dur, .clocks").first
                    duration = ""
                    if await dur_el.count() > 0:
                        duration = (await dur_el.inner_text()).strip()
                    slot_id = None
                    link = row.locator("a[href*='build.php?id=']").first
                    if await link.count() > 0:
                        m = _SLOT_ID.search(await link.get_attribute("href") or "")
                        if m:
                            slot_id = int(m.group(1))
                    if name:
                        items.append(
                            {"building": name, "time_left": duration, "slot_id": slot_id}
                        )
                except Exception:
                    continue
            break
        return items

    async def check_incoming_attacks(self) -> bool:
        """Controlla se ci sono movimenti di truppe ostili diretti verso il villaggio."""
        try:
            has_attacks = await self.page.evaluate("""
                () => {
                    // Rileva l'indicatore di attacco rosso nella barra laterale o nel villaggio
                    const attackIndicator = document.querySelector(
                        '#sidebarBoxActiveTroopMovements tr.att, #sidebarBoxActiveTroopmovements tr.att, .troopMovements .att1, .mov.att1, .troopMovements .attack, img.att1, [class*="att1"]'
                    );
                    return attackIndicator !== null;
                }
            """)
            return bool(has_attacks)
        except Exception as exc:
            logger.debug("JS evaluate per attacchi fallito: %s", exc)
            return (await self._read_incoming_attacks()) > 0

    async def get_all_villages(self) -> list[Village]:
        """Estrae rigorosamente tutti i villaggi presenti nella sidebar dell'account."""
        try:
            # Attende brevemente che il box della lista villaggi o elementi villaggio siano presenti
            try:
                await self.page.wait_for_selector(
                    '#sidebarBoxVillagelist, #sidebarBoxVillageList, [id*="villagelist" i], [id*="villageList" i], .villageList, a[href*="newdid="]',
                    timeout=3000,
                )
            except Exception:
                pass

            villages_data = await self.page.evaluate("""
                () => {
                    const list = [];
                    const seenIds = new Set();

                    // Normalizza il segno meno Unicode (U+2212 '−' e trattini
                    // U+2010-U+2015) usato da Travian: parseInt('−15') -> NaN.
                    const toInt = (s) => parseInt(String(s).replace(/[\\u2212\\u2010-\\u2015]/g, '-'), 10);
                    const coordRe = /\\(\\s*([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\s*[|/]\\s*([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\s*\\)/;

                    const cleanVillageName = (raw) => {
                        if (!raw) return '';
                        let text = raw.replace(/[\\u202d\\u202c\\r\\n]/g, ' ');
                        // Rimuove coordinate tipo (10|20), (-5|12), (−5|12), ( 10 | 20 )
                        text = text.replace(new RegExp(coordRe.source, 'g'), ' ');
                        text = text.replace(/\\s+/g, ' ').trim();
                        return text;
                    };

                    // Estrae (x, y) da un link mappa karte.php?x=..&y=.. dentro lo scope
                    const parseKarteCoords = (scopeEl) => {
                        if (!scopeEl || !scopeEl.querySelectorAll) return null;
                        const links = scopeEl.querySelectorAll('a[href*="karte.php"]');
                        for (const a of links) {
                            const href = a.href || a.getAttribute('href') || '';
                            const mx = href.match(/[?&]x=([-+]?\\d+)/);
                            const my = href.match(/[?&]y=([-+]?\\d+)/);
                            if (mx && my) return [parseInt(mx[1], 10), parseInt(my[1], 10)];
                        }
                        return null;
                    };

                    // Chiudi eventuali modali/banner di benvenuto che oscurano i dati
                    try {
                        const dismissBtn = document.querySelector('.dialogCloseButton, button.dialogButtonCancel, a.dialogCloseButton, .modalClose, button.cmp-cta-accept');
                        if (dismissBtn && typeof dismissBtn.click === 'function') dismissBtn.click();
                    } catch (e) {}

                    // 1. Cerca il contenitore della lista villaggi nella sidebar
                    const containers = document.querySelectorAll(
                        '#sidebarBoxVillagelist, #sidebarBoxVillageList, [id*="villagelist" i], [id*="villageList" i], .villageList, .villagesList, #villageList, #villagesList, #sidebarAfterContent .villageListWrapper'
                    );

                    const entrySelectors = '.listEntry, .entry, li, tr, [data-did]';

                    containers.forEach(container => {
                        const entries = container.querySelectorAll(entrySelectors);
                        entries.forEach(entry => {
                            // Cerca data-did o link interno con newdid
                            let vid = entry.getAttribute('data-did') || entry.getAttribute('data-village-id') || entry.getAttribute('data-newdid') || '';
                            let linkEl = entry.querySelector('a[href*="newdid="], a[href*="newdid"], a') || (entry.tagName === 'A' ? entry : null);
                            let href = linkEl?.href || '';

                            if (!vid && href) {
                                const match = href.match(/[?&]newdid=(\\d+)/) || href.match(/newdid=(\\d+)/);
                                if (match) vid = match[1];
                            }

                            const nameEl = entry.querySelector('.name, .villageName, .entryName, label, .headline') || linkEl || entry;
                            const rawText = nameEl?.innerText || nameEl?.textContent || entry.innerText || '';
                            const coordMatch = rawText.match(coordRe);
                            const karteCoords = coordMatch ? null : parseKarteCoords(entry);
                            let nameText = cleanVillageName(rawText);

                            const isActive = entry.classList.contains('active') || linkEl?.classList.contains('active') || entry.getAttribute('aria-current') === 'true';

                            if (vid && !seenIds.has(vid)) {
                                seenIds.add(vid);
                                const finalUrl = (href && href.includes('newdid=')) ? href : `dorf1.php?newdid=${vid}`;
                                list.push({
                                    name: nameText && nameText !== 'Villaggio' ? nameText : `Villaggio_${vid}`,
                                    url: finalUrl,
                                    village_id: vid,
                                    is_active: isActive,
                                    x: coordMatch ? toInt(coordMatch[1]) : (karteCoords ? karteCoords[0] : null),
                                    y: coordMatch ? toInt(coordMatch[2]) : (karteCoords ? karteCoords[1] : null)
                                });
                            }
                        });
                    });

                    // 2. Scansione documento globale se non sono stati trovati o se mancano villaggi
                    const allNewdidLinks = document.querySelectorAll('a[href*="newdid="], a[href*="newdid"], [data-did]');
                    allNewdidLinks.forEach(el => {
                        let vid = el.getAttribute('data-did') || el.getAttribute('data-village-id') || '';
                        let href = el.href || '';
                        if (!vid && href) {
                            const match = href.match(/[?&]newdid=(\\d+)/) || href.match(/newdid=(\\d+)/);
                            if (match) vid = match[1];
                        }
                        if (vid && !seenIds.has(vid)) {
                            seenIds.add(vid);
                            const nameEl = el.querySelector('.name, .villageName') || el;
                            const rawText = nameEl?.innerText || nameEl?.textContent || '';
                            const coordMatch = rawText.match(coordRe);
                            const karteScope = el.closest('li, tr, .entry, .listEntry, [data-did]') || el;
                            const karteCoords = coordMatch ? null : parseKarteCoords(karteScope);
                            let nameText = cleanVillageName(rawText);
                            list.push({
                                name: nameText && nameText !== 'Villaggio' ? nameText : `Villaggio_${vid}`,
                                url: href || `dorf1.php?newdid=${vid}`,
                                village_id: vid,
                                is_active: el.classList.contains('active'),
                                x: coordMatch ? toInt(coordMatch[1]) : (karteCoords ? karteCoords[0] : null),
                                y: coordMatch ? toInt(coordMatch[2]) : (karteCoords ? karteCoords[1] : null)
                            });
                        }
                    });

                    // 3. Verifica villaggio attivo corrente
                    const activeNameEl = document.querySelector(
                        '#sidebarBoxVillagelist .active .name, #sidebarBoxVillageList .active .name, [id*="villagelist" i] .active .name, .villageList .active .name, #villageNameField, .villageName'
                    );
                    const currentActiveName = cleanVillageName(activeNameEl?.innerText || activeNameEl?.textContent || '');

                    // Se abbiamo trovato elementi ma nessuno è marcato attivo, prova ad associare l'attivo
                    if (currentActiveName && list.length > 0 && !list.some(v => v.is_active)) {
                        const found = list.find(v => v.name.toLowerCase() === currentActiveName.toLowerCase());
                        if (found) found.is_active = true;
                    }

                    return list;
                }
            """)

            from .models import Village
            villages = [
                Village(
                    name=v["name"],
                    url=v["url"],
                    village_id=safe_int(v.get("village_id")),
                    is_active=bool(v.get("is_active", False)),
                    x=safe_int_opt(v.get("x")),
                    y=safe_int_opt(v.get("y")),
                )
                for v in (villages_data or [])
            ]

            if villages:
                return villages
        except Exception as e:
            logger.debug(f"Errore nell'estrazione della lista villaggi JS: {e}")

        # Fallback Selettori DOM
        return await self._dom_extract_villages()

    async def _dom_extract_villages(self) -> list[Village]:
        """Fallback DOM-based per estrarre i villaggi."""
        from .models import Village
        villages: list[Village] = []
        seen_ids: set[int] = set()

        for sel in selectors.VILLAGE_LIST_ENTRIES:
            locators = self.page.locator(sel)
            try:
                count = await locators.count()
            except Exception:
                continue
            if count == 0:
                continue

            for i in range(count):
                entry = locators.nth(i)
                try:
                    # Estrae did da attributo
                    vid_attr = await entry.get_attribute("data-did")
                    vid = safe_int(vid_attr)

                    # Cerca link
                    link_el = entry.locator("a[href*='newdid']").first
                    href = ""
                    if await link_el.count() > 0:
                        href = (await link_el.get_attribute("href")) or ""
                        if not vid:
                            match = _NEWDID.search(href)
                            if match:
                                vid = safe_int(match.group(1))

                    name = await _text_of(entry, selectors.VILLAGE_ENTRY_NAME)
                    if not name:
                        name = (await entry.inner_text()).strip()

                    cleaned_name = re.sub(r"\(\s*[-+]?\d+\s*[|/]\s*[-+]?\d+\s*\)", "", name).strip()
                    if vid and vid not in seen_ids:
                        seen_ids.add(vid)
                        villages.append(
                            Village(
                                name=cleaned_name or f"Villaggio_{vid}",
                                url=href or f"dorf1.php?newdid={vid}",
                                village_id=vid,
                            )
                        )
                except Exception:
                    continue

            if villages:
                break

        return villages

    async def extract_village_name(self) -> str:
        """Estrae il nome del villaggio attualmente attivo."""
        try:
            name: str = await self.page.evaluate("""
                () => {
                    const selectors = [
                        '#sidebarBoxVillagelist .active .name',
                        '#sidebarBoxVillageList .active .name',
                        '[id*="villagelist" i] .active .name',
                        '[id*="villageList" i] .active .name',
                        '.villageList .active .name',
                        '.villagesList .active .name',
                        '#villageNameField',
                        '.villageName',
                        '#sidebarBoxVillagelist .active',
                        '#sidebarBoxVillageList .active',
                        '[data-did].active .name',
                        '[data-did].active'
                    ];
                    for (const sel of selectors) {
                        try {
                            const el = document.querySelector(sel);
                            if (el) {
                                let text = (el.innerText || el.textContent || '').trim();
                                text = text.replace(/[\\u202d\\u202c\\r\\n]/g, ' ');
                                text = text.replace(/\\(\\s*[-+]?\\d+\\s*[|/]\\s*[-+]?\\d+\\s*\\)/g, ' ');
                                text = text.replace(/\\s+/g, ' ').trim();
                                if (text && text.length > 0 && text !== 'Villaggio') return text;
                            }
                        } catch (e) {}
                    }
                    return '';
                }
            """)
            if name:
                return name
        except Exception:
            pass
        return await self._read_village_name()

    # --- Metodi per GameState Tipizzato ---

    async def read_state(self) -> GameState:
        """Restituisce l'oggetto GameState tipizzato per il Brain decisionale."""
        res_data = await self.extract_resources()
        curr = res_data.get("current", {})
        caps = res_data.get("capacity", {})

        resources = Resources(
            wood=safe_int(curr.get("wood", 0)),
            clay=safe_int(curr.get("clay", 0)),
            iron=safe_int(curr.get("iron", 0)),
            crop=safe_int(curr.get("crop", 0)),
            warehouse_capacity=safe_int(caps.get("warehouse", 0)),
            granary_capacity=safe_int(caps.get("granary", 0)),
        )

        incoming_attacks_count = await self._read_incoming_attacks()
        if incoming_attacks_count == 0 and await self.check_incoming_attacks():
            incoming_attacks_count = 1

        state = GameState(
            resources=resources,
            incoming_attacks=incoming_attacks_count,
            village_name=await self.extract_village_name(),
            village_id=self._current_newdid(),
        )
        return state

    def _current_newdid(self) -> int:
        """newdid del villaggio attivo, estratto dall'URL corrente (0 = default)."""
        match = _NEWDID.search(self.page.url or "")
        return safe_int(match.group(1)) if match else 0

    async def read_fields(self) -> list[Field]:
        """Slot 1-18 dal dorf1 (campi risorsa)."""
        fields: list[Field] = []
        try:
            raw_fields: list = await self.page.evaluate("""
                () => {
                    const list = [];
                    const slots = document.querySelectorAll(
                        '#resourceFieldContainer .buildingSlot, #village_map .buildingSlot, #villageMap .buildingSlot, .buildingSlot, a[href*="build.php?id="], area'
                    );
                    slots.forEach(slot => {
                        const href = slot.getAttribute('href') || '';
                        const cls = slot.getAttribute('class') || '';
                        let slotId = null;
                        const matchId = href.match(/build\\.php[?&]id=(\\d+)/);
                        if (matchId) {
                            slotId = parseInt(matchId[1], 10);
                        } else {
                            const matchCls = cls.match(/\\ba(\\d+)\\b/);
                            if (matchCls) slotId = parseInt(matchCls[1], 10);
                        }
                        const title = slot.getAttribute('title') || slot.getAttribute('alt') || slot.innerText || '';
                        if (slotId !== null) {
                            list.push({ slot_id: slotId, title: title.trim() });
                        }
                    });
                    return list;
                }
            """)
            if raw_fields and isinstance(raw_fields, list):
                for item in raw_fields:
                    s_id = item.get("slot_id")
                    title = item.get("title", "")
                    if s_id is not None:
                        fields.append(
                            Field(
                                slot_id=s_id,
                                name=title,
                                level=parse_int(title),
                                resource_type=guess_resource_type(title),
                            )
                        )
                if fields:
                    logger.debug("Campi letti tramite JS Injection: %d", len(fields))
                    return fields
        except Exception as exc:
            logger.debug("JS evaluate per campi fallito: %s", exc)

        # Fallback DOM Locator
        container = await _first_locator(self.page, selectors.VILLAGE_MAP)
        if container is None:
            logger.warning("Mappa villaggio non trovata: aggiorna selectors.VILLAGE_MAP")
            return fields

        for sel in selectors.VILLAGE_FIELD:
            locators = container.locator(sel)
            try:
                count = await locators.count()
            except Exception:
                continue
            if count == 0:
                continue
            for i in range(count):
                try:
                    el = locators.nth(i)
                    slot_id = await self._extract_slot_id(el)
                    if slot_id is None:
                        continue
                    title = (await el.get_attribute("title")) or ""
                    fields.append(
                        Field(
                            slot_id=slot_id,
                            name=title.strip(),
                            level=parse_int(title),
                            resource_type=guess_resource_type(title),
                        )
                    )
                except Exception:
                    continue
            break
        logger.debug("Campi letti tramite DOM locator: %d", len(fields))
        return fields

    async def read_interior_fields(self) -> list[Field]:
        """Slot 19-40 dal dorf2 (edifici interni): slot_id, gid, nome e livello.

        Naviga a dorf2.php preservando il newdid corrente. Il `gid` identifica
        il tipo di edificio (es. 10=magazzino, 11=granaio, 0=area vuota), così
        il brain può ragionare sull'infrastruttura senza dipendere dalla lingua.
        """
        page = self.page
        assert page is not None

        current_url = page.url or ""
        if selectors.PATH_DORF2 not in current_url:
            match = _NEWDID.search(current_url)
            target_url = _abs_game_url(current_url, selectors.PATH_DORF2)
            if match:
                target_url += f"?newdid={match.group(1)}"
            await self._goto(target_url)

        try:
            interior_data = await page.evaluate("""
                () => {
                    const buildings = {};
                    const nodes = document.querySelectorAll(
                        '#village_map .buildingSlot, #villageMap .buildingSlot, ' +
                        '.buildingSlot, a[href*="build.php?id="], area[href*="build.php?id="], [data-aid]'
                    );
                    nodes.forEach(el => {
                        const cls = el.getAttribute('class') || '';
                        const inner = el.querySelector('a') || el;

                        // slot id: href build.php?id=N > classe 'aN' > data-aid
                        let slotId = null;
                        const href = (inner.getAttribute && inner.getAttribute('href')) ||
                                     el.getAttribute('href') || '';
                        const idMatch = href.match(/build\\.php[^#\\s]*?\\bid=(\\d+)/) || href.match(/[?&]id=(\\d+)/);
                        const clsMatch = cls.match(/\\ba(\\d+)\\b/);
                        const aid = el.getAttribute('data-aid') || inner.getAttribute?.('data-aid');
                        if (idMatch) slotId = parseInt(idMatch[1], 10);
                        else if (clsMatch) slotId = parseInt(clsMatch[1], 10);
                        else if (aid && /^\\d+$/.test(aid)) slotId = parseInt(aid, 10);

                        // Solo edifici interni del dorf2
                        if (slotId === null || slotId < 19 || slotId > 40) return;

                        // gid: classe 'gN' del markup Travian (g10 magazzino, g11 granaio, g0 vuoto)
                        let gid = -1;
                        const gidMatch = cls.match(/\\bg(\\d+)\\b/);
                        if (gidMatch) gid = parseInt(gidMatch[1], 10);

                        // nome e livello: title/alt/innerText > sotto-elementi dedicati
                        const title = (inner.getAttribute && inner.getAttribute('title')) ||
                                      el.getAttribute('title') || el.getAttribute('alt') || '';
                        const nameEl = el.querySelector('.name, .buildingName');
                        const levelEl = el.querySelector('.level, .labelLevel, .lvl');
                        let name = (title || (nameEl ? nameEl.innerText : '') || el.innerText || '').trim();
                        let level = null;
                        const levelSrc = levelEl ? levelEl.innerText : title;
                        const lvlMatch = (levelSrc || '').match(/(\\d+)/g);
                        if (lvlMatch) level = parseInt(lvlMatch[lvlMatch.length - 1], 10);

                        // Se lo stesso slot compare due volte, tieni la versione piu' ricca
                        const prev = buildings[slotId];
                        if (!prev || (!prev.name && name) || (prev.level === null && level !== null)) {
                            buildings[slotId] = { slot_id: slotId, gid: gid, name: name, level: level };
                        }
                    });
                    return buildings;
                }
            """)
            fields: list[Field] = []
            for item in (interior_data or {}).values():
                try:
                    slot_id = int(item.get("slot_id"))
                except (TypeError, ValueError):
                    continue
                name = str(item.get("name") or "").strip()
                level = item.get("level")
                gid = safe_int(item.get("gid"), -1)
                # Coerenza nome<->gid per Residenza (25) e Palazzo (26):
                # il titolo dell'edificio prevale sulla classe CSS 'gN'
                # quando il markup varia tra skin/server — un Palazzo
                # rilevato come g25 invertirebbe la mutua esclusione.
                lname = name.lower()
                if any(
                    k in lname
                    for k in ("palazz", "reggia", "palace", "palast", "palais")
                ):
                    gid = 26
                elif "residen" in lname:
                    gid = 25
                fields.append(
                    Field(
                        slot_id=slot_id,
                        name=name,
                        level=parse_int(str(level)) if level is not None else 0,
                        resource_type=None,
                        gid=gid,
                    )
                )
            if fields:
                fields.sort(key=lambda f: f.slot_id)
                logger.debug("Edifici interni letti dal dorf2: %d", len(fields))
            return fields
        except Exception as exc:
            logger.debug("Errore nella lettura degli edifici interni: %s", exc)
            return []

    async def read_production(self, village_id: int = 0) -> dict:
        """Legge produzione oraria e bilancio netto grano.

        Ritorna {"production": {ResourceType: int}, "crop_balance": int|None}.

        Fonte primaria: i globali JS dello stock bar (``window.production``,
        ``resources.production``, ``Travian.Game.production``) presenti in
        OGNI pagina di gioco T4.x — l1..l3 produzione oraria e l4 ritmo
        netto del contatore grano (anche negativo). Zero navigazioni.
        Fallback: la tabella di production.php, con righe identificate
        sia per icona (img.r1-r4, indipendente dalla lingua) sia per
        nome risorsa localizzato.
        """
        page = self.page
        if page is None:
            return {}

        # 1) Globali JS dello stock bar: nessuna navigazione necessaria.
        try:
            gdata = await page.evaluate("""
                () => {
                    const num = (v) => {
                        const s = String(v ?? '')
                            .replace(/[\\u2212\\u2010-\\u2015]/g, '-');
                        const m = s.match(/-?\\d[\\d.,\\s]*/);
                        if (!m) return null;
                        const n = parseInt(m[0].replace(/[^\\d-]/g, ''), 10);
                        return isNaN(n) ? null : n;
                    };
                    const pick = (o) =>
                        (o && typeof o === 'object') ? o : null;
                    const cands = [];
                    try { cands.push(pick(window.production)); } catch (e) {}
                    try {
                        cands.push(pick(window.resources &&
                            window.resources.production));
                    } catch (e) {}
                    try {
                        if (window.Travian && Travian.Game) {
                            cands.push(pick(Travian.Game.production));
                            if (Travian.Game.village) {
                                cands.push(pick(
                                    Travian.Game.village.production
                                ));
                            }
                        }
                    } catch (e) {}
                    for (const p of cands) {
                        if (!p) continue;
                        const out = {
                            wood: num(p.l1 ?? p.wood ?? p.lumber),
                            clay: num(p.l2 ?? p.clay),
                            iron: num(p.l3 ?? p.iron),
                            crop: num(p.l4 ?? p.crop ?? p.wheat),
                            // l5 = grano netto (lordo - upkeep truppe):
                            // il contatore del granaio varia a questo ritmo.
                            crop_net: num(p.l5 ?? p.freeCrop ?? p.free_crop
                                          ?? p.cropNet ?? p.crop_net),
                        };
                        if (Object.values(out).some(v => v !== null)) {
                            return out;
                        }
                    }
                    return null;
                }
            """)
        except Exception:
            gdata = None

        if gdata:
            prod: dict[ResourceType, int] = {}
            balance: int | None = None
            for key, rtype in (
                ("wood", ResourceType.WOOD),
                ("clay", ResourceType.CLAY),
                ("iron", ResourceType.IRON),
                ("crop", ResourceType.CROP),
            ):
                val = safe_int_opt(gdata.get(key))
                if val is None:
                    continue
                prod[rtype] = abs(val)
            # l5 = bilancio netto grano (lordo - upkeep); se il globale
            # non lo espone resta None, mai stimato dal lordo.
            balance = safe_int_opt(gdata.get("crop_net"))
            if prod or balance is not None:
                return {"production": prod, "crop_balance": balance}

        # 2) Fallback: tabella di production.php.
        try:
            current_url = page.url or ""
            if "production.php" not in current_url:
                match = _NEWDID.search(current_url)
                target_url = _abs_game_url(current_url, "production.php")
                if match:
                    target_url += f"?newdid={match.group(1)}"
                await self._goto(target_url)

            data = await page.evaluate("""
                () => {
                    const out = { production: {}, crop_balance: null };
                    const resKeys = {
                        wood: ['wood', 'legno', 'holz', 'bois', 'madera',
                               'lumber'],
                        clay: ['clay', 'argilla', 'lehm', 'argile', 'barro'],
                        iron: ['iron', 'ferro', 'eisen', 'fer', 'hierro'],
                        crop: ['crop', 'grano', 'getreide', 'cereale',
                               'cereal', 'wheat', 'blé', 'trigo'],
                    };
                    const iconKeys = {
                        r1: 'wood', r2: 'clay', r3: 'iron', r4: 'crop',
                    };
                    const balKeys = ['bilancio', 'balance', 'saldo',
                                     'equilibrio', 'equilibre',
                                     'crop balance', 'free crop',
                                     'grano libero', 'bilancio del grano'];
                    const toInt = (s) => parseInt(
                        String(s)
                            .replace(/[\\u2212\\u2010-\\u2015]/g, '-')
                            .replace(/[\\s.]/g, ''),
                        10
                    );
                    const rows = document.querySelectorAll(
                        '#production tr, table.production tr, ' +
                        '.productionTable tr, #content table tr'
                    );
                    rows.forEach(row => {
                        const txt = (row.innerText || '').toLowerCase();
                        const nums = (txt.match(/[+\\u2212\\u2010-\\u2015\\-]?\\d[\\d.\\s]*/g) || [])
                            .map(toInt)
                            .filter(n => !isNaN(n));
                        if (!nums.length) return;
                        if (balKeys.some(k => txt.includes(k))) {
                            // Bilancio netto grano: ultimo numero della riga
                            out.crop_balance = nums[nums.length - 1];
                            if (/[\\u2212\\u2010-\\u2015]/.test(txt) || /-\\d/.test(txt)) {
                                out.crop_balance = -Math.abs(out.crop_balance);
                            }
                            return;
                        }
                        // Icona r1-r4: identificazione indipendente
                        // dalla lingua (il nome risorsa puo' mancare).
                        const cls = String(row.className || '') + ' ' +
                            Array.from(row.querySelectorAll('img,td,th'))
                                .map(e => String(e.className || ''))
                                .join(' ');
                        for (const [ic, res] of Object.entries(iconKeys)) {
                            if (new RegExp('\\\\b' + ic + '\\\\b').test(cls)
                                && !(res in out.production)) {
                                out.production[res] = Math.abs(nums[0]);
                                return;
                            }
                        }
                        for (const [key, words] of Object.entries(resKeys)) {
                            if (words.some(w => txt.includes(w))) {
                                if (!(key in out.production)) {
                                    out.production[key] = Math.abs(nums[0]);
                                }
                                return;
                            }
                        }
                    });
                    return out;
                }
            """)
            prod = {}
            for key, val in (data or {}).get("production", {}).items():
                try:
                    prod[ResourceType(key)] = safe_int(val)
                except (ValueError, TypeError):
                    continue
            raw_balance = (data or {}).get("crop_balance")
            # None semantico: bilancio non letto -> il consumer decide
            balance = safe_int_opt(raw_balance)
            if not prod and balance is None:
                await self._dump_page_text("production_parse_fail")
                logger.info(
                    "Produzione non letta da production.php (%s) — "
                    "dump in debug/production_parse_fail.txt",
                    page.url,
                )
                return {}
            return {"production": prod, "crop_balance": balance}
        except Exception as exc:
            logger.info("Errore lettura produzione: %s (%s)", exc, page.url)
            return {}

    async def read_build_queue(self) -> list[BuildQueueItem]:
        """Restituisce gli elementi in coda con timer in secondi."""
        items: list[BuildQueueItem] = []
        raw_queue = await self.extract_building_queue()
        for q in raw_queue:
            name = q.get("building", "")
            duration_str = q.get("time_left", "")
            slot_id = q.get("slot_id")
            if isinstance(slot_id, int):
                # classificazione affidabile via build.php?id=N
                is_res = 1 <= slot_id <= 18
            else:
                # fallback: deduzione dal nome (multi-lingua)
                is_res = guess_resource_type(name) is not None
            items.append(
                BuildQueueItem(
                    name=name,
                    level=parse_int(name),
                    remaining_seconds=parse_timer(duration_str),
                    is_resource_field=is_res,
                    slot_id=slot_id if isinstance(slot_id, int) else 0,
                )
            )
        return items

    async def _extract_slot_id(self, el) -> int | None:
        try:
            href = (await el.get_attribute("href")) or ""
            match = _SLOT_ID.search(href)
            if match:
                return int(match.group(1))
            css_class = (await el.get_attribute("class")) or ""
            match_class = _SLOT_CLASS.search(css_class)
            if match_class:
                return int(match_class.group(1))
        except Exception:
            pass
        return None

    async def _read_resources(self) -> Resources:
        res = Resources()
        res_selectors = {
            "wood": ["#stockBar #l1 .value", "#l1 .value", "#l1", "#stockBarResource1 .value", "#stockBarResource1", "#stockBar .wood .value", "#stockBar .resource1 .value", ".wood .value"],
            "clay": ["#stockBar #l2 .value", "#l2 .value", "#l2", "#stockBarResource2 .value", "#stockBarResource2", "#stockBar .clay .value", "#stockBar .resource2 .value", ".clay .value"],
            "iron": ["#stockBar #l3 .value", "#l3 .value", "#l3", "#stockBarResource3 .value", "#stockBarResource3", "#stockBar .iron .value", "#stockBar .resource3 .value", ".iron .value"],
            "crop": ["#stockBar #l4 .value", "#l4 .value", "#l4", "#stockBarResource4 .value", "#stockBarResource4", "#stockBar .crop .value", "#stockBar .resource4 .value", ".crop .value"],
        }
        for attr, sels in res_selectors.items():
            for sel in sels:
                try:
                    item = self.page.locator(sel).first
                    if await item.count() > 0:
                        value = (await item.inner_text()).strip()
                        num = parse_int(value)
                        if num > 0 or value == "0":
                            setattr(res, attr, num)
                            break
                except Exception:
                    continue

        warehouse = await _first_locator(self.page, selectors.WAREHOUSE_ITEM)
        if warehouse is not None:
            try:
                res.warehouse_capacity = parse_capacity(
                    (await warehouse.get_attribute("title"))
                    or (await warehouse.inner_text())
                )
            except Exception:
                pass
        granary = await _first_locator(self.page, selectors.GRANARY_ITEM)
        if granary is not None:
            try:
                res.granary_capacity = parse_capacity(
                    (await granary.get_attribute("title"))
                    or (await granary.inner_text())
                )
            except Exception:
                pass
        return res

    async def _read_village_name(self) -> str:
        for sel in selectors.ACTIVE_VILLAGE_NAME:
            locator = self.page.locator(sel).first
            try:
                if await locator.count() > 0:
                    return (await locator.inner_text()).strip()
            except Exception:  # noqa: BLE001
                continue
        return ""

    async def get_incoming_attacks(
        self, rally_slot: int = 0, village_id: int = 0
    ) -> list[IncomingAttack]:
        """Elenca i movimenti in arrivo dalla panoramica del Punto di Raduno.

        Wrapper retro-compatibile su :meth:`get_troop_movements` (una
        sola navigazione per entrambe le direzioni).
        """
        incoming, _outgoing = await self.get_troop_movements(
            rally_slot, village_id
        )
        return incoming

    async def get_troop_movements(
        self, rally_slot: int = 0, village_id: int = 0
    ) -> tuple[list[IncomingAttack], list[IncomingAttack]]:
        """Legge (movimenti in arrivo, movimenti in partenza) dalla
        panoramica del Punto di Raduno.

        Naviga a build.php?id=<rally_slot> e legge le righe 'in'/'out'
        delle tabelle movimenti: tipo (icona att/def), countdown
        all'impatto, conteggio truppe leggibile e controparte
        (attaccante per gli 'in', destinazione per gli 'out').
        Restituisce due liste vuote se il Punto di Raduno non esiste o
        il parse fallisce.
        """
        if not self.page or not rally_slot:
            return [], []
        url = f"{selectors.PATH_BUILD}?id={rally_slot}"
        if village_id:
            url += f"&newdid={village_id}"
        try:
            if f"id={rally_slot}" not in (self.page.url or ""):
                await self._goto(
                    _abs_game_url(self.page.url or "", url),
                    timeout=20000,
                )
            raw = await self.page.evaluate(
                """
                () => {
                    const parseRow = (row) => {
                        const img = row.querySelector('img');
                        const imgKey = (
                            ((img && img.getAttribute('class')) || '') +
                            ' ' + ((img && img.alt) || '')
                        ).toLowerCase();
                        let mtype = 'unknown';
                        if (/att\\d?|attack/.test(imgKey)) mtype = 'attack';
                        else if (/def\\d?|defense|reinforce/.test(imgKey)) {
                            mtype = 'reinforce';
                        }
                        const rowCls = (row.className || '').toLowerCase();
                        if (/def/.test(rowCls) && mtype === 'unknown') {
                            mtype = 'reinforce';
                        }
                        if (/att/.test(rowCls) && mtype === 'unknown') {
                            mtype = 'attack';
                        }

                        // Countdown all'impatto (.timer o pattern h:mm:ss)
                        const rowText = (row.innerText || '').trim();
                        const timerEl = row.querySelector(
                            '.timer, span.timer, .dur'
                        );
                        const ttxt = timerEl
                            ? (timerEl.textContent || '')
                            : rowText;
                        const m = ttxt.match(/(?:(\\d+)\\s*[-+]\\s*)?(?:(\\d+):)?(\\d+):(\\d{2})/);
                        let secs = 0;
                        if (m) {
                            secs =
                                parseInt(m[1] || 0, 10) * 86400 +
                                parseInt(m[2] || 0, 10) * 3600 +
                                parseInt(m[3] || 0, 10) * 60 +
                                parseInt(m[4] || 0, 10);
                        }

                        // Truppe leggibili: cifre accanto alle icone unit.
                        // In T4.x la composizione nemica e' '?' => 0 = ignoto.
                        let count = 0;
                        row.querySelectorAll('td, span').forEach(el => {
                            const t = (el.innerText || '').trim();
                            if (/^\\d+$/.test(t) && t.length < 7) {
                                count += parseInt(t, 10);
                            }
                        });

                        // Controparte: attaccante per 'in', destinazione
                        // per 'out' (link giocatore/villaggio/mappa).
                        let other = '';
                        const pLink = row.querySelector(
                            'a[href*="spieler"], a[href*="profile"], a[href*="karte"]'
                        );
                        if (pLink) {
                            other = (pLink.innerText || '').trim();
                        }

                        return {
                            type: mtype,
                            seconds: secs,
                            count: count,
                            attacker: other,
                            label: rowText.slice(0, 160),
                        };
                    };
                    const incoming = [];
                    const outgoing = [];
                    // In T4.x le righe dei movimenti portano la classe
                    // 'in' (arrivo) o 'out' (partenza). La variante
                    // '#content .troop_details tr' copre le skin senza
                    // marcatura esplicita: restano nel bucket 'in'
                    // (comportamento storico del parser).
                    document.querySelectorAll(
                        '#content table.troop_details tr.in, ' +
                        '#content tr.in, table.troop_details tr.in, ' +
                        '.troop_details tr.in, #content .troop_details tr'
                    ).forEach(row => {
                        const cls = ' ' +
                            (row.className || '').toLowerCase() + ' ';
                        if (/\\bout\\b/.test(cls)) {
                            outgoing.push(parseRow(row));
                        } else {
                            incoming.push(parseRow(row));
                        }
                    });
                    document.querySelectorAll(
                        '#content table.troop_details tr.out, ' +
                        '#content tr.out, table.troop_details tr.out, ' +
                        '.troop_details tr.out'
                    ).forEach(row => outgoing.push(parseRow(row)));
                    return { incoming: incoming, outgoing: outgoing };
                }
                """
            )
        except Exception as exc:
            logger.debug("Parse movimenti Piazza d'Armi fallito: %s", exc)
            return [], []

        now = time.time()

        def _build(items: list[dict]) -> list[IncomingAttack]:
            moves: list[IncomingAttack] = []
            for item in items or []:
                secs = safe_int(item.get("seconds") or 0)
                moves.append(
                    IncomingAttack(
                        movement_type=str(item.get("type") or "unknown"),
                        seconds_remaining=secs,
                        troop_count=safe_int(item.get("count") or 0),
                        arrival_ts=now + secs,
                        attacker=str(item.get("attacker") or ""),
                        label=str(item.get("label") or ""),
                    )
                )
            return moves

        raw = raw or {}
        incoming = _build(raw.get("incoming") or [])
        outgoing = _build(raw.get("outgoing") or [])
        if incoming:
            logger.info(
                "Piazza d'Armi: %d movimenti in arrivo (%s)",
                len(incoming),
                ", ".join(
                    f"{a.movement_type} in {a.seconds_remaining}s"
                    for a in incoming
                ),
            )
        if outgoing:
            logger.info(
                "Piazza d'Armi: %d movimenti in partenza (%s)",
                len(outgoing),
                ", ".join(
                    f"{a.movement_type} in {a.seconds_remaining}s"
                    for a in outgoing
                ),
            )
        return incoming, outgoing

    async def _read_incoming_attacks(self) -> int:
        box = await _first_locator(self.page, selectors.TROOP_MOVEMENT_BOX)
        if box is None:
            return 0
        for sel in selectors.INCOMING_ATTACK_ROWS:
            rows = box.locator(sel)
            try:
                count = await rows.count()
            except Exception:  # noqa: BLE001
                continue
            if count > 0:
                return count
        return 0

    async def has_video_bonus_available(self) -> bool:
        """Rileva se l'InfoBox o la pagina segnala la disponibilità del bonus video +15%."""
        if not self.page:
            return False
        try:
            return await self.page.evaluate("""
                () => {
                    const infoBox = document.querySelector('#sidebarBoxInfoBox, #infoBox, .infoBox');
                    if (!infoBox) return false;
                    const text = infoBox.innerText || '';
                    return (
                        text.includes('+15%') &&
                        (text.includes('video') || text.includes('negozio') || text.includes('bonus') || text.includes('attivare'))
                    );
                }
            """)
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Modulo Espansione: punti cultura, coloni, tile mappa
    # ------------------------------------------------------------------

    async def read_culture_points(
        self, residence_slot: int, village_id: int = 0
    ) -> tuple[int, int, int]:
        """Legge (CP attuali, CP richiesti, PC/giorno) da Residenza/Palazzo.

        Il riepilogo dei Punti Cultura vive su tab diverse a seconda
        della versione/skin di T4.x (panoramica di default oppure le
        tab ``&tt=N``): si scansiona prima la tab che ha funzionato
        l'ultima volta (cache ``_cp_tab``) e poi le altre.
        Ritorna (0, 0, 0) se nessuna pagina espone il dato.
        """
        if not self.page or not residence_slot:
            return (0, 0, 0)

        suffixes = (
            [self._cp_tab] if self._cp_tab is not None else []
        ) + [s for s in self._CP_TAB_SUFFIXES if s != self._cp_tab]

        # JS condiviso dalle navigazioni (scan tab + link diretto).
        cp_eval_js = """
            () => {
                const raw = (document.body
                    && document.body.innerText) || '';
                // Travian avvolge i numeri nei mark direzionali
                // U+202D/U+202C: normalizza tutto a spazi.
                const text = raw.replace(
                    /[\\u202d\\u202c\\u202f\\u00a0\\u200b]/g, ' ');
                const clean = (s) => parseInt(
                    String(s).replace(/[^0-9]/g, ''), 10) || 0;
                const num = '([0-9][0-9.,\\\\s]*)';
                const cpLabel =
                    '(?:punti\\\\s+(?:di\\\\s+)?cultura' +
                    '|punti\\\\s+culturali|culture\\\\s+points?' +
                    '|kulturpunkte|puntos?\\\\s+de\\\\s+cultura' +
                    '|points?\\\\s+de\\\\s+culture)';
                const out = {
                    have: 0, need: 0, daily: 0,
                    matched: false, specific: false,
                    has_cp: new RegExp(cpLabel, 'i').test(text),
                };
                if (!out.has_cp) return out;
                // Link diretto alla tab CP dalla navbar della
                // Residenza (la label "Punti cultura" e' presente
                // nella navbar di tutte le tab).
                const navLink = Array.from(
                    document.querySelectorAll('a[href]')
                ).find((a) => new RegExp(
                    '^' + cpLabel + '$', 'i'
                ).test((a.innerText || '').trim()));
                if (navLink) {
                    out.cp_nav_href = navLink.href || '';
                    const tm = (navLink.getAttribute('href') || '')
                        .match(/tt=(\\d+)/);
                    if (tm) out.cp_nav_tt = '&tt=' + tm[1];
                }
                // -- Campi specifici tab "Punti cultura" T4.x --
                // have: "Punti cultura prodotti finora 1517"
                let m = text.match(new RegExp(
                    cpLabel + '\\\\s+prodotti\\\\s+finora' +
                    '[^0-9]{0,15}' + num, 'i')) ||
                    text.match(
                    /(?:produced\\s+(?:so\\s+far|until\\s+now)|bisher\\s+produziert)[^0-9]{0,15}([0-9][0-9.,\\s]*)/i);
                if (m) {
                    out.have = clean(m[1]);
                    out.matched = true;
                    out.specific = true;
                }
                // need: "Il prossimo villaggio sara' disponibile
                //        a 2000"
                m = text.match(
                    /(?:prossimo\\s+villaggio\\s+sar.{0,10}disponibile\\s+a|prossimo\\s+villaggio\\s+.{0,25}richiede|next\\s+village.{0,20}(?:available|requires)|n.chstes\\s+dorf.{0,25}(?:verf.gbar|ben.tigt))[^0-9]{0,10}([0-9][0-9.,\\s]*)/i
                );
                if (m) {
                    out.need = clean(m[1]);
                    out.matched = true;
                    out.specific = true;
                }
                // daily: sezione "Punti cultura al giorno" ->
                //        ultima riga "Totale 123"
                m = text.match(new RegExp(
                    cpLabel + '\\\\s+al\\\\s+giorno' +
                    '[\\\\s\\\\S]{0,400}?(?:totale|total|gesamt)' +
                    '[^0-9]{0,10}' + num, 'i')) ||
                    text.match(
                    /(?:culture\\s+points?\\s+per\\s+day|kulturpunkte\\s+pro\\s+tag)[\\s\\S]{0,400}?(?:total|gesamt)[^0-9]{0,10}([0-9][0-9.,\\s]*)/i);
                if (m) {
                    out.daily = clean(m[1]);
                    out.matched = true;
                    out.specific = true;
                }
                if (out.specific) return out;
                // -- Fallback generico: "X / Y" o "X di Y" --
                m = text.match(new RegExp(
                    cpLabel + '[^0-9]{0,40}' + num +
                    '[^0-9]{0,10}(?:/|\\\\bdi\\\\b|\\\\bof\\\\b' +
                    '|\\\\bvon\\\\b|\\\\bde\\\\b)[^0-9]{0,10}' +
                    num, 'i'));
                if (m) {
                    out.have = clean(m[1]);
                    out.need = clean(m[2]);
                    out.matched = true;
                } else {
                    // \\b impedisce "serve" dentro "server" (l'ora del
                    // server veniva letta come CP richiesti).
                    const n = text.match(
                        /\\b(?:richiesti|necessari|servono|serve|needed|required|ben.tigt|requis|erforderlich)\\b[^0-9]{0,25}([0-9][0-9.,\\s]*)/i
                    );
                    if (n) {
                        out.need = clean(n[1]);
                        out.matched = true;
                    }
                }
                // daily generico: "250 al giorno", "250 per day"
                const d = text.match(
                    /([0-9][0-9.,\\s]*)[^0-9]{0,20}(?:al giorno|per day|a day|pro tag|par jour|al d[ií]a|daily)/i
                ) || text.match(
                    /(?:produzione|production|producci.n)[^0-9]{0,25}([0-9][0-9.,\\s]*)[^0-9]{0,15}(?:punti|points|cultur)/i
                );
                if (d) out.daily = clean(d[1]);
                return out;
            }
        """

        accepted = None   # (suffix, data) con campi specifici matchati
        generic = None    # (suffix, data) solo match generico
        nav_link = None   # (suffix_tt, href) dalla navbar
        tried = set()     # suffix gia' navigati

        for suffix in suffixes:
            url = f"{selectors.PATH_BUILD}?id={residence_slot}{suffix}"
            if village_id:
                url += f"&newdid={village_id}"
            tried.add(suffix)
            try:
                await self._goto(
                    _abs_game_url(self.page.url or "", url),
                )
            except Exception:
                continue
            try:
                data = await self.page.evaluate(cp_eval_js)
            except Exception:
                continue
            data = data or {}
            if nav_link is None and data.get("cp_nav_href"):
                nav_link = (
                    data.get("cp_nav_tt") or "",
                    data["cp_nav_href"],
                )
            if data.get("specific"):
                accepted = (suffix, data)
                break
            if generic is None and data.get("matched"):
                generic = (suffix, data)
            if nav_link:
                # Link diretto disponibile: evita lo scan cieco.
                break

        # La navbar espone il link diretto alla tab CP: se la tab
        # scansionata non aveva campi specifici si naviga li'.
        if accepted is None and nav_link:
            nav_suffix, nav_href = nav_link
            if not nav_suffix or nav_suffix not in tried:
                try:
                    await self._goto(nav_href)
                    data = await self.page.evaluate(cp_eval_js)
                    data = data or {}
                    if data.get("matched"):
                        accepted = (nav_suffix, data)
                except Exception:
                    pass

        if accepted is None:
            accepted = generic
        if accepted is not None:
            suffix, data = accepted
            self._cp_tab = suffix or ""
            have = safe_int(data.get("have", 0))
            need = safe_int(data.get("need", 0))
            daily = safe_int(data.get("daily", 0))
            # Parse parziale (label CP trovata ma have/daily a zero):
            # dump per confrontare il formato numerico reale del server.
            if not have or not daily:
                await self._dump_page_text("culture_points_partial")
            return (have, need, daily)

        await self._dump_page_text("culture_points_fail")
        logger.info(
            "Punti cultura non letti nella Residenza (slot %s): "
            "dump in debug/culture_points_fail.txt",
            residence_slot,
        )
        return (0, 0, 0)

    async def read_troops_home(self, rally_slot: int = 0, village_id: int = 0) -> dict[str, int]:
        """Legge le truppe presenti nel villaggio.

        Fonte primaria: panoramica del Punto di Raduno (build.php?id=slot).
        Fallback: box "Unita'" della panoramica villaggio (dorf1.php) —
        richiesto dal bugfix parser truppe (Potenza Militare=0): se il
        markup della pagina rally non espone i conteggi nel formato
        atteso, la lettura non si arrende al primo selettore fallito.

        Ritorna {'uN': quantita'}; dict vuoto solo se nessuna delle
        pagine espone i dati.
        """
        if not self.page:
            return {}
        if rally_slot:
            # tt=1: overview movimenti + tabella 'truppe nel villaggio'.
            url = f"{selectors.PATH_BUILD}?id={rally_slot}&tt=1"
            if village_id:
                url += f"&newdid={village_id}"
            try:
                await self._goto(
                    _abs_game_url(self.page.url or "", url),
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "read_troops_home: navigazione rally fallita: %s", exc
                )
        units = await self._read_troops_dom()
        if not units:
            # Fallback panoramica: il box 'Unita' di dorf1.php mostra i
            # contingenti del villaggio anche quando la pagina rally ha
            # un markup diverso (spec bugfix parser truppe).
            try:
                await self._goto(
                    _abs_game_url(
                        self.page.url or "",
                        selectors.PATH_DORF1
                        + (f"?newdid={village_id}" if village_id else ""),
                    ),
                    wait_until="domcontentloaded",
                )
                units = await self._read_troops_dom()
            except Exception:
                pass
        if not units:
            # Non silenzioso: il farming deve poter distinguere un
            # villaggio davvero vuoto da un sensore cieco (bug 1.1).
            try:
                logger.info(
                    "read_troops_home: nessuna truppa letta su %s "
                    "(rally tt=1 + dorf1), pagina=%s",
                    f"newdid={village_id}",
                    (self.page.url or "")[:120],
                )
            except Exception:
                pass
        return units

    async def _read_troops_dom(self) -> dict[str, int]:
        """Estrae {'uN': qty} dalla pagina corrente.

        Parser tollerante al markup (T4.4/T4.6): la quantita' viene
        cercata in catena — cella dell'icona, cella adiacente,
        genitore, prima cella solo-numerica della riga. Se non ci sono
        icone 'unit uN', fallback testuale su righe "Nome Unita' NNN"
        con mappa nomi localizzati -> uN (spec bugfix parser truppe).
        """
        data = await self.page.evaluate(
            """
            (params) => {
                const out = {};
                const labelMap = params.labels || {};
                const num = (t) => {
                    const m = String(t || '').match(/(\\d[\\d.]*)/);
                    return m ? parseInt(m[1].replace(/\\./g, ''), 10) : 0;
                };
                // 1) Icone 'unit uN': scope documento, dedup per nodo
                //    DOM (stessa icona non conta due volte; icone
                //    diverse della stessa uN si sommano = contingenti
                //    multipli a casa).
                const icons = document.querySelectorAll(
                    'img[class*="unit"], i[class*="unit"], ' +
                    'a[class*="unit"], span[class*="unit"], ' +
                    '[class*=" unit u"], [class*="unit u"]'
                );
                const seen = new Set();
                icons.forEach(icon => {
                    if (seen.has(icon)) return;
                    seen.add(icon);
                    const cls = icon.getAttribute('class') || '';
                    const m = cls.match(/(?:^|[\\s_\\-])u(\\d{1,2})(?=[\\s\\-]|$)/);
                    if (!m) return;
                    const key = 'u' + m[1];
                    let qty = 0;
                    // NOTA: '.unit' escluso da closest() — matcherebbe
                    // l'icona stessa (class 'unit uN') fermando la
                    // ricerca della quantita' sul nodo sbagliato.
                    const cell = icon.closest(
                        'td, a, li, .col, .troop, div'
                    );
                    if (cell) {
                        qty = num(cell.innerText || '');
                        if (!qty && cell.nextElementSibling)
                            qty = num(cell.nextElementSibling.innerText || '');
                        if (!qty && cell.parentElement)
                            qty = num(cell.parentElement.innerText || '');
                    }
                    if (!qty) {
                        const row = icon.closest('tr, li');
                        if (row) {
                            const cells = row.querySelectorAll('td, span, div');
                            for (const c of cells) {
                                if (c === cell || c.contains(icon)) continue;
                                const t = (c.innerText || '').trim();
                                if (/^\\d[\\d.]*$/.test(t)) { qty = num(t); break; }
                            }
                        }
                    }
                    if (qty > 0) out[key] = (out[key] || 0) + qty;
                });
                // 2) Fallback testuale: righe 'Nome Unita' 826' senza icone.
                if (!Object.keys(out).length) {
                    const rows = document.querySelectorAll(
                        'tr, li, .troop_details div, .troopDetails div'
                    );
                    rows.forEach(r => {
                        const raw = (r.innerText || '').replace(/\\s+/g, ' ').trim();
                        const low = raw.toLowerCase();
                        for (const name in labelMap) {
                            const idx = low.indexOf(name);
                            if (idx >= 0) {
                                const q = num(low.slice(idx + name.length));
                                if (q > 0) {
                                    const k = labelMap[name];
                                    out[k] = Math.max(out[k] || 0, q);
                                }
                                break;
                            }
                        }
                    });
                }
                return { units: out };
            }
            """,
            {"labels": _troop_label_map()},
        )
        units = (data or {}).get("units") or {}
        return {str(k): safe_int(v) for k, v in units.items() if safe_int(v) > 0}

    async def read_training_queue(
        self, trainer_slot: int, village_id: int = 0
    ) -> dict[str, int]:
        """Legge le unita' attualmente in addestramento in un edificio.

        Naviga a build.php?id=<trainer_slot> e raschia la tabella delle
        unita' 'in addestramento' (icone 'unit uN' + quantita'). Serve al
        modulo Smart Troop Replenishment per la formula del deficit
        (target - attuali - in coda). Ritorna {'uN': qty} o {} se la
        pagina non espone la coda.
        """
        if not self.page or not trainer_slot:
            return {}
        url = f"{selectors.PATH_BUILD}?id={trainer_slot}"
        if village_id:
            url += f"&newdid={village_id}"
        try:
            await self._goto(
                _abs_game_url(self.page.url or "", url),
            )
        except Exception:
            return {}
        try:
            data = await self.page.evaluate("""
                () => {
                    const out = {};
                    // Righe 'in addestramento' del trainer: tabella under_progress
                    // o contenitori simili con icona unit + quantita'/durata.
                    const scopes = document.querySelectorAll(
                        'table.under_progress, .under_progress, #training, ' +
                        '.training_list, .troopTraining, #build form + table'
                    );
                    scopes.forEach(scope => {
                        scope.querySelectorAll(
                            'img.unit, span.unit, i.unit, [class*="unit u"]'
                        ).forEach(icon => {
                            const cls = icon.getAttribute('class') || '';
                            const m = cls.match(/u(\\d+)/);
                            if (!m) return;
                            const key = 'u' + m[1];
                            const row = icon.closest('tr, li, .troop, div') || icon.parentElement;
                            const txt = (row && row.innerText) || '';
                            const num = txt.match(/(\\d[\\d.]*)/);
                            const qty = num
                                ? parseInt(num[1].replace(/[^0-9]/g, ''), 10) || 0
                                : 1;
                            out[key] = (out[key] || 0) + qty;
                        });
                    });
                    return out;
                }
            """)
            return {str(k): safe_int(v) for k, v in (data or {}).items() if safe_int(v) > 0}
        except Exception:
            return {}

    async def read_farm_lists(
        self, rally_slot: int, village_id: int = 0
    ) -> list[dict]:
        """Elenca le farm list del Punto di Raduno.

        Naviga a build.php?id=<slot>&tt=99 e ritorna una lista di dict:
        {name, targets, index}. Lista vuota se il tab non si apre.
        """
        if not self.page or not rally_slot:
            return []
        url = f"{selectors.PATH_BUILD}?id={rally_slot}&tt=99"
        if village_id:
            url += f"&newdid={village_id}"
        try:
            await self._goto(
                _abs_game_url(self.page.url or "", url),
            )
        except Exception:
            return []
        try:
            data = await self.page.evaluate("""
                (rowsSelectors) => {
                    const lists = [];
                    const nodes = new Set();
                    for (const sel of rowsSelectors) {
                        document.querySelectorAll(sel).forEach(n => nodes.add(n));
                    }
                    let i = 0;
                    nodes.forEach(row => {
                        const name = (
                            row.querySelector('.listTitle, .name, .listName, strong, b, .title')
                                ?.innerText || row.innerText || ''
                        ).trim().split('\\n')[0].slice(0, 60);
                        const tcount = row.querySelectorAll(
                            'input[type="checkbox"], .target, tr.slot, li.target'
                        ).length;
                        lists.push({ index: i++, name: name, targets: tcount });
                    });
                    return lists;
                }
            """, selectors.FARM_LIST_ROWS)
            return [d for d in (data or []) if isinstance(d, dict)]
        except Exception:
            return []

    async def read_map_tile(self, x: int, y: int) -> dict:
        """Legge le info del tile mappa (x, y): occupato, tipo campi grano.

        Naviga a position_details.php?x=X&y=Y (pagina dettagli tile in T4,
        con distribuzione campi e link 'Fonda'); se il pannello dettagli non
        viene trovato, fa un secondo tentativo su karte.php?x=X&y=Y.

        Ritorna: {occupied, free, crop_fields, text, settle_link}
        """
        if not self.page:
            return {"occupied": True, "crop_fields": 0, "text": "", "settle_link": False}
        data: dict | None = None
        for path in ("position_details.php", selectors.PATH_MAP):
            try:
                await self._goto(
                    _abs_game_url(self.page.url or "", f"{path}?x={x}&y={y}"),
                )
            except Exception:
                continue
            try:
                data = await self._eval_map_tile()
            except Exception:
                data = None
            # Se il pannello dettagli e' stato trovato il dato e' affidabile;
            # altrimenti riprova sulla pagina mappa (position_details assente).
            if data and data.get("panel"):
                break
        if not data:
            return {"occupied": True, "crop_fields": 0, "text": "", "settle_link": False}
        data.pop("panel", None)
        return data

    async def _eval_map_tile(self) -> dict | None:
        """Estrae occupazione/campi grano dalla pagina dettagli tile corrente."""
        return await self.page.evaluate("""
                () => {
                    const body = (document.body && document.body.innerText) || '';
                    const panel = document.querySelector(
                        '#map_details, .map_details, #tileDetails, .tileDetails, #map_infobox, .infobox'
                    );
                    const text = (panel && panel.innerText) || body;

                    // Distribuzione campi "3-3-3-9" (9c) o "1-1-1-15" (15c)
                    const dist = text.match(/(\\d)\\s*[-\\u2212\\u2010-\\u2015]\\s*(\\d)\\s*[-\\u2212\\u2010-\\u2015]\\s*(\\d)\\s*[-\\u2212\\u2010-\\u2015]\\s*(\\d+)/);
                    let cropFields = 0;
                    if (dist) {
                        cropFields = parseInt(dist[4], 10) || 0;
                    } else {
                        const cm = text.match(/(\\d+)\\s*(?:campi? grano|crop(fields?)?|getreidefelder|campos? de cereal|champs? de c[eé]r[eé]ales?)/i);
                        if (cm) cropFields = parseInt(cm[1], 10) || 0;
                        const c9 = text.match(/\\b(9|15)[ -]?[cç]\\b/i) || text.match(/\\b(9|15)[ ]?(?:cropper|croplog|grano)\\b/i);
                        if (!cropFields && c9) cropFields = parseInt(c9[1], 10) || 0;
                    }

                    const settle = !!document.querySelector(
                        'a[href*="a=settler"], a[href*="settler"]'
                    );
                    const occupied = /(?:abitato|occupied|village|villaggio|player|giocatore|inhabitant|oasis|oasi)/i.test(text)
                        && !settle;
                    const freeTile = /(?:valley|valle|unoccupied|libero|libera|free|terra libera|abandoned|disabitata|non occupat|uninhabited)/i.test(text);

                    return {
                        occupied: occupied && !freeTile,
                        free: freeTile || settle,
                        crop_fields: cropFields,
                        settle_link: settle,
                        panel: !!panel,
                        text: text.slice(0, 300),
                    };
                }
            """)

    async def read_village_coords(self, village_id: int = 0) -> tuple[int, int] | None:
        """Recupera le coordinate (x, y) di un villaggio tramite la mappa.

        Fallback per i server dove la sidebar non espone le coordinate:
        naviga ``karte.php?newdid=<id>`` (la mappa si centra sul villaggio)
        e legge gli input X/Y del form "vai a coordinate" o, in subordine,
        il tile centrale della griglia.
        """
        if not self.page or not village_id:
            return None
        try:
            await self._goto(
                _abs_game_url(
                    self.page.url or "",
                    f"{selectors.PATH_MAP}?newdid={village_id}",
                ),
                wait_until="domcontentloaded",
            )
        except Exception:
            return None
        try:
            coords = await self.page.evaluate("""
                () => {
                    const toInt = (s) => parseInt(
                        String(s)
                            .replace(/[\\u2212\\u2010-\\u2015]/g, '-')
                            .replace(/[^0-9+-]/g, ''),
                        10
                    );
                    const pick = (sels) => {
                        for (const s of sels) {
                            const el = document.querySelector(s);
                            if (!el) continue;
                            const raw = (el.value !== undefined && el.value !== '')
                                ? el.value
                                : (el.textContent || '');
                            const n = toInt(raw);
                            if (!isNaN(n)) return n;
                        }
                        return null;
                    };
                    // Form "vai a coordinate" della mappa (T4.4/T4.5/T4.6)
                    const x = pick([
                        '#xCoordInput', 'input#xCoordInput',
                        '#mapCoordX input', '#mapCoordX',
                        'input[name="x"]', '.coordinateX input',
                    ]);
                    const y = pick([
                        '#yCoordInput', 'input#yCoordInput',
                        '#mapCoordY input', '#mapCoordY',
                        'input[name="y"]', '.coordinateY input',
                    ]);
                    if (x !== null && y !== null) return {x: x, y: y};
                    // Fallback: tile centrale della griglia -> link dettagli
                    const center = document.querySelector(
                        '#mapContainer .tile.center a, ' +
                        '#mapContainer a.center, ' +
                        '#mapContainer a[class*="own"]'
                    );
                    if (center) {
                        const m = (center.href || '').match(/x=([+\\u2212\\u2010-\\u2015\\-]?\\d+).*?y=([+\\u2212\\u2010-\\u2015\\-]?\\d+)/)
                            || (center.title || '').match(
                                /\\(\\s*([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\s*[|/]\\s*([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\s*\\)/
                            );
                        if (m) return {x: toInt(m[1]), y: toInt(m[2])};
                    }
                    return null;
                }
            """)
            if (
                isinstance(coords, dict)
                and isinstance(coords.get("x"), int)
                and isinstance(coords.get("y"), int)
            ):
                return int(coords["x"]), int(coords["y"])
        except Exception:
            logger.debug(
                "read_village_coords: estrazione fallita per %s",
                village_id, exc_info=True,
            )
        return None

    async def scan_map_area(self, center_x: int, center_y: int, radius: int = 5) -> list[dict]:
        """Scansiona la mappa attorno al villaggio attivo (karte.php).

        Estrae i villaggi grigi (inattivi/abbandonati), oasi e insediamenti vicini.
        """
        if not self.page:
            return []
        try:
            await self._goto(
                _abs_game_url(self.page.url or "", f"{selectors.PATH_MAP}?x={center_x}&y={center_y}"),
            )
            # T4.5/T4.6: la griglia mappa e' renderizzata via JS dopo il
            # DOMContentLoaded — attendiamo la comparsa di almeno un tile
            # (timeout breve, non fatale: il dump diagnostico copre i casi ko).
            try:
                await self.page.wait_for_selector(
                    "a[href*='position_details.php'], a[href*='karte.php?x='], "
                    "map area, #mapContainer a, [id*='tile' i], [data-x][data-y]",
                    timeout=6000,
                )
            except Exception:
                pass
        except Exception:
            return []

        try:
            tiles: list = await self.page.evaluate("""
                (params) => {
                    const { centerX, centerY, rad } = params;
                    const results = [];
                    const seen = new Set();

                    // Normalizza il segno meno Unicode (U+2212 '−' e trattini
                    // U+2010-U+2015) usato da Travian nelle coordinate
                    const toInt = (s) => parseInt(String(s).replace(/[\\u2212\\u2010-\\u2015]/g, '-'), 10);
                    const coordRe = /\\(([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\s*[|/]\\s*([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\)/;

                    // T4.4/T4.5/T4.6: estrazione dai blocchi tile o link con coordinate
                    const mapLinks = document.querySelectorAll(
                        '#mapContainer a, #map_details a, #mapContainer area, #mapCoordX, .mapBlock, a[href*="position_details.php"], a[href*="karte.php?x="]'
                    );

                    mapLinks.forEach(el => {
                        const href = el.getAttribute('href') || el.getAttribute('onclick') || '';
                        const title = el.getAttribute('title') || el.innerText || '';
                        const xMatch = href.match(/[?&]x=([-\\d]+)/) || title.match(coordRe);
                        const yMatch = href.match(/[?&]y=([-\\d]+)/);

                        let x = null, y = null;
                        if (xMatch && yMatch) {
                            x = toInt(xMatch[1]);
                            y = toInt(yMatch[1]);
                        } else if (title) {
                            const cMatch = title.match(coordRe);
                            if (cMatch) {
                                x = toInt(cMatch[1]);
                                y = toInt(cMatch[2]);
                            }
                        }

                        if (x === null || y === null) {
                            // Attributi data-x/data-y (tile div T4.5+)
                            const dx = el.getAttribute('data-x') || el.getAttribute('data-tile-x');
                            const dy = el.getAttribute('data-y') || el.getAttribute('data-tile-y');
                            if (dx !== null && dy !== null) {
                                x = toInt(dx); y = toInt(dy);
                            }
                        }
                        if (x === null || y === null) {
                            // id="tile_X_Y" / "tile-X-Y"
                            const idm = (el.id || '').match(/tile[_-]?(-?\\d+)[_-](-?\\d+)/i);
                            if (idm) { x = toInt(idm[1]); y = toInt(idm[2]); }
                        }
                        if (x === null || y === null) {
                            // Handler inline con coordinate (x: .., y: ..)
                            const oc = (el.getAttribute('onclick') || '') + ' ' +
                                (el.getAttribute('onmouseover') || '');
                            const om = oc.match(/x\\s*[:=]\\s*(-?\\d+)\\D{0,20}?y\\s*[:=]\\s*(-?\\d+)/);
                            if (om) { x = toInt(om[1]); y = toInt(om[2]); }
                        }
                        if (x === null || y === null) {
                            // Link figlio con coordinate dentro il contenitore tile
                            const inner = el.querySelector && el.querySelector(
                                'a[href*="x="][href*="y="], area[href*="x="][href*="y="]'
                            );
                            if (inner) {
                                const ih = inner.getAttribute('href') || '';
                                const ix = ih.match(/[?&]x=([-\\d]+)/);
                                const iy = ih.match(/[?&]y=([-\\d]+)/);
                                if (ix && iy) { x = toInt(ix[1]); y = toInt(iy[1]); }
                            }
                        }

                        if (x === null || y === null || isNaN(x) || isNaN(y)) return;
                        const key = `${x}_${y}`;
                        if (seen.has(key)) return;
                        seen.add(key);

                        // Calcola distanza dal centro
                        const dist = Math.hypot(x - centerX, y - centerY);
                        if (dist > rad * 1.5) return;

                        // Estrazione popolazione e nome se presenti nel tooltip
                        const popMatch = title.match(/(?:popolazione|population|einwohner|habitants|habitantes)[:\\s]*(\\d+)/i)
                            || title.match(/\\b(\\d+)\\s*(?:abitanti|inhabitants|pop)\\b/i);
                        const pop = popMatch ? parseInt(popMatch[1], 10) : 0;

                        const isOasis = /(?:oasis|oasi)/i.test(title) || /(?:oasis|oasi)/i.test(href);
                        const name = title.split('\\n')[0].replace(/\\([\\d\\s|\\-+\\u2212\\u2010-\\u2015]+\\)/g, '').trim() || (isOasis ? 'Oasi' : `Tile (${x}|${y})`);

                        results.push({
                            x: x,
                            y: y,
                            village_name: name,
                            population: pop,
                            is_oasis: isOasis,
                            distance: dist,
                        });
                    });

                    return results;
                }
            """, {"centerX": center_x, "centerY": center_y, "rad": radius})
            out = [t for t in (tiles or []) if isinstance(t, dict)]
            if not out:
                # Dump diagnostico: rivela il markup reale della pagina mappa
                # (container presenti, iframe, tile alternativi) al prossimo run.
                try:
                    dbg = await self.page.evaluate("""
                        () => ({
                            url: location.href,
                            links: document.querySelectorAll('a').length,
                            areas: document.querySelectorAll('area').length,
                            tileIds: document.querySelectorAll('[id*="tile" i]').length,
                            mapContainer: !!document.getElementById('mapContainer'),
                            map: !!document.getElementById('map'),
                            mapBlocks: document.querySelectorAll(
                                '.mapBlock, .mapWrapper, #map_details, .tile'
                            ).length,
                            iframes: document.querySelectorAll('iframe').length,
                            text: ((document.body && document.body.innerText) || '')
                                .slice(0, 200),
                        })
                    """)
                    logger.debug(
                        "scan_map_area (%s|%s): 0 tile estratti, dump=%s",
                        center_x, center_y, dbg,
                    )
                except Exception:
                    pass
            return out
        except Exception:
            return []

    async def read_combat_reports(self, limit: int = 15) -> list[dict]:
        """Legge gli ultimi report di saccheggio/combattimento (berichte.php).

        Estrae le coordinate del target, il ratio di bottino e le perdite subite.
        """
        if not self.page:
            return []
        try:
            await self._goto(
                _abs_game_url(self.page.url or "", "berichte.php?t=1"),
            )
        except Exception:
            return []

        try:
            reports: list = await self.page.evaluate("""
                (maxCount) => {
                    const results = [];
                    // Normalizza il segno meno Unicode (U+2212 '−' e trattini
                    // U+2010-U+2015) usato da Travian nelle coordinate
                    const toInt = (s) => parseInt(String(s).replace(/[\\u2212\\u2010-\\u2015]/g, '-'), 10);
                    const rows = document.querySelectorAll(
                        '#overview tbody tr, table.reports tbody tr, #reports tbody tr, tr:has(.subject)'
                    );

                    let count = 0;
                    rows.forEach(row => {
                        if (count >= maxCount) return;
                        const subjectEl = row.querySelector('.sub, .subject, a[href*="berichte.php?id="]');
                        if (!subjectEl) return;

                        const text = (subjectEl.innerText || '').trim();
                        const title = subjectEl.getAttribute('title') || row.innerText || '';

                        // Estrae coordinate se presenti nel testo del report: es. "raid su Villaggio (12|-34)"
                        const coordMatch = (text + ' ' + title).match(/\\(([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\s*[|/]\\s*([+\\u2212\\u2010-\\u2015\\-]?\\d+)\\)/);
                        if (!coordMatch) return;

                        const x = toInt(coordMatch[1]);
                        const y = toInt(coordMatch[2]);

                        // Icona esito saccheggio / perdite:
                        // classe 'iReport1' = bottino pieno/verde, 'iReport2' = parziale, 'iReport3' = vuoto
                        // 'iReport4' / 'iReport5' / 'iReport6' = perdite o sconfitta
                        const icon = row.querySelector('img[class*="iReport"], i[class*="iReport"], .reportStatus');
                        const iconClass = (icon && icon.getAttribute('class')) || '';
                        const iconAlt = (icon && icon.getAttribute('alt')) || '';

                        let lootRatio = 1.0;
                        let losses = 0;
                        let defenders = 0;

                        if (/iReport1\\b|full|pieno|100%/i.test(iconClass + ' ' + iconAlt)) {
                            lootRatio = 1.0;
                        } else if (/iReport2\\b|half|parziale|yellow/i.test(iconClass + ' ' + iconAlt)) {
                            lootRatio = 0.5;
                        } else if (/iReport3\\b|empty|vuoto|grey/i.test(iconClass + ' ' + iconAlt)) {
                            lootRatio = 0.0;
                        } else if (/iReport[45678]\\b|loss|perdita|red|defeat|rosso/i.test(iconClass + ' ' + iconAlt)) {
                            losses = 1;
                            defenders = 5;
                            lootRatio = 0.0;
                        }

                        results.push({
                            x: x,
                            y: y,
                            target_name: text.split('(')[0].trim() || `Target (${x}|${y})`,
                            loot_ratio: lootRatio,
                            losses: losses,
                            defenders: defenders,
                        });
                        count++;
                    });

                    return results;
                }
            """, limit)
            return [r for r in (reports or []) if isinstance(r, dict)]
        except Exception:
            return []


# Alias per retrocompatibilità e interoperabilità
GameObserver = TravianParser
