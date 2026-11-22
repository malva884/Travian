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
from .building_prereqs import FIELD_GID_TYPES, GID_ACADEMY
from .server_profile import calibrate_routing, profile_for_url
from .telemetry_capture import capture_anomaly
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
# Tooltip del tasto di annullamento lavori — non e' il nome edificio.
_CANCEL_WORD = re.compile(
    r"^(annulla|cancel|abbrechen|annuler|iptal|avbryt)\b", re.IGNORECASE
)

# parole chiave (multi-lingua) per mappare un campo al suo tipo di risorsa
_RESOURCE_KEYWORDS: dict[ResourceType, tuple[str, ...]] = {
    ResourceType.WOOD: (
        "wood",
        "legno",
        "holz",
        "bois",
        "madera",
        "boschivo",
        "bosco",
        "taglialegna",
        "woodcutter",
        "holzfäller",
        "holzfaeller",
    ),
    ResourceType.CLAY: (
        "clay",
        "argilla",
        "lehm",
        "argile",
        "barro",
        "cava",
        "clay pit",
        "lehmgrube",
    ),
    ResourceType.IRON: (
        "iron",
        "ferro",
        "eisen",
        "fer",
        "hierro",
        "miniera",
        "iron mine",
        "eisenmine",
    ),
    ResourceType.CROP: (
        "crop",
        "grano",
        "getreide",
        "cereale",
        "wheat",
        "ble",
        "trigo",
        "campo",
        "cropland",
        "getreidefeld",
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


# Parole che identificano il controllo "Ricerca" nella riga unita'
# dell'Accademia (IT/EN/DE/FR/ES).
_ACADEMY_RESEARCH_WORDS = re.compile(
    r"ricerca|ricerc|research|erforsch|recherche|estudia|estudi", re.I
)
# Priorita' di merge quando lo stesso uN compare in piu' nodi (es. icona
# nella coda + riga tabella): lo stato piu' informativo vince.
_ACADEMY_STATUS_PRIORITY = {
    "researched": 0,
    "unknown": 1,
    "blocked": 2,
    "researchable": 3,
    "in_progress": 4,
}


def _academy_row_status(rec: dict) -> str:
    """Classifica una riga unita' del dump Accademia.

    - ``in_progress``: timer nella riga (ricerca in corso);
    - ``researchable``: almeno un controllo abilitato il cui testo/href
      riconduce a 'Ricerca' (pulsante pronto);
    - ``blocked``: controlli presenti ma tutti disabilitati (risorse
      insufficienti o prerequisiti mancanti);
    - ``unknown``: controlli abilitati non riconducibili a ricerca;
    - ``researched``: riga senza controlli (unita' gia' sbloccata).
    """
    if rec.get("timer"):
        return "in_progress"
    controls = list(rec.get("controls") or [])
    enabled = [c for c in controls if not c.get("disabled")]
    for ctl in enabled:
        text = str(ctl.get("text") or "")
        href = str(ctl.get("href") or "")
        if _ACADEMY_RESEARCH_WORDS.search(text) or re.search(
            r"[?&](?:a|t)=\d+", href
        ):
            return "researchable"
    if controls and not enabled:
        return "blocked"
    if enabled:
        return "unknown"
    return "researched"


def normalize_academy_state(raw: dict | None) -> dict:
    """Normalizza il dump JS ``_JS_ACADEMY_SNAPSHOT`` in uno stato utile.

    Ritorna ``{"research_active": bool, "researching": [tN],
    "units": {tN: {status,label,costs}}}`` oppure {} se il markup non
    espone righe unita' riconoscibili.
    """
    if not isinstance(raw, dict):
        return {}
    try:
        from .models import STATIC_TROOP_LABELS  # import ritardato: no cicli
    except Exception:  # noqa: BLE001
        STATIC_TROOP_LABELS = {}
    units: dict[str, dict] = {}
    for rec in raw.get("rows") or []:
        if not isinstance(rec, dict):
            continue
        try:
            un = int(rec.get("u") or 0)
        except (TypeError, ValueError):
            continue
        if not 1 <= un <= 30:
            continue
        key = f"t{un}"
        status = _academy_row_status(rec)
        costs = {
            str(k): safe_int(v)
            for k, v in (rec.get("costs") or {}).items()
            if safe_int(v) > 0
        }
        entry = {
            "status": status,
            "label": STATIC_TROOP_LABELS.get(key, key),
            "costs": costs,
        }
        prev = units.get(key)
        if prev is None or _ACADEMY_STATUS_PRIORITY.get(
            status, 0
        ) > _ACADEMY_STATUS_PRIORITY.get(prev.get("status", ""), 0):
            units[key] = entry
    researching = sorted(
        (k for k, u in units.items() if u.get("status") == "in_progress"),
        key=lambda k: int(k[1:]),
    )
    active = bool(raw.get("active")) or bool(researching)
    if not units and not active:
        return {}
    return {
        "research_active": active,
        "researching": researching,
        "units": units,
    }


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

    # JS condiviso dalle pagine CP (scan tab Residenza + link diretto):
    # cerca label "Punti cultura" multi-lingua e ne estrae i numeri
    # prodotti/richiesti/giornalieri normalizzando i mark direzionali.
    _CP_EVAL_JS = """
        () => {
            const raw = (document.body
                && document.body.innerText) || '';
            // Travian avvolge i numeri nei mark direzionali
            // (U+202D/U+202C, isolate U+2066-U+2069, U+061C):
            // normalizza tutto a spazi.
            const text = raw.replace(
                /[\\u202d\\u202c\\u202f\\u00a0\\u200b\\u061c\\u2066-\\u2069]/g, ' ');
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
            //        riga finale "Totale/Somma 123". Tollera
            //        parole extra tra label e 'al giorno' (es.
            //        "Punti cultura prodotti al giorno") e le
            //        varianti 'quotidiani/giornalieri'.
            //        NB: regex LITERAL — [\\s\\S] passato come stringa
            //        a new RegExp collasserebbe a [sS] (escape non
            //        valido in stringa): daily resterebbe sempre 0.
            m = text.match(
                /(?:punti\\s+(?:di\\s+)?cultura|punti\\s+culturali|culture\\s+points?|kulturpunkte|puntos?\\s+de\\s+cultura|points?\\s+de\\s+culture)[^0-9]{0,40}?(?:al\\s+giorno|quotidian[oi]|giornalier[oi])[\\s\\S]{0,400}?(?:totale|total|gesamt|somma)[^0-9]{0,10}([0-9][0-9.,\\s]*)/i
            ) || text.match(
                /(?:culture\\s+points?\\s+per\\s+day|kulturpunkte\\s+pro\\s+tag)[\\s\\S]{0,400}?(?:total|gesamt|sum|totale|somma)[^0-9]{0,10}([0-9][0-9.,\\s]*)/i);
            if (m) {
                out.daily = clean(m[1]);
                out.matched = true;
                out.specific = true;
            }
            // Nuova UI: tab "Punti cultura" di /village/statistics —
            // tabella "Villaggio PC/Giorno Party Truppe Slots" con riga
            // "Somma 57". Non mostra i totali assoluti dello slot.
            m = text.match(
                /(?:PC|CP)\\s*\\/\\s*(?:giorno|day|tag|jour|d[ií]a|dia)[\\s\\S]{0,900}?(?:somma|totale|total|gesamt)[^0-9]{0,10}([0-9][0-9.,\\s]*)/i
            );
            if (m && !out.daily) {
                out.daily = clean(m[1]);
                out.matched = true;
                out.specific = true;
            }
            // Stessa pagina nuova UI: riga "Somma 57 - 0   U / T"
            // (slot insediamento usati/totali) e frase "prossimo slot
            // insediamento il DD.MM.YY, HH:MM".
            m = text.match(
                /(?:somma|totale|total|gesamt)\\s+[0-9][0-9.,\\s]*[\\s\\S]{0,80}?(\\d+)\\s*\\/\\s*(\\d+)/i
            );
            if (m) {
                out.slots_used = clean(m[1]);
                out.slots_total = clean(m[2]);
            }
            m = text.match(
                /slot[^0-9]{0,60}?(\\d{1,2}[.\\/-]\\d{1,2}[.\\/-]\\d{2,4}[^|±(\\n\\r]{0,15})/i
            );
            if (m) {
                out.next_slot = String(m[1]).trim();
            }
            // daily generico: "250 al giorno", "250 per day".
            // Provato anche quando i campi specifici hanno gia'
            // matchato ma il daily e' rimasto a zero (wording
            // diverso dalla sezione attesa o render asincrono).
            if (!out.daily) {
                const d = text.match(
                    /([0-9][0-9.,\\s]*)[^0-9]{0,20}(?:al giorno|per day|a day|pro tag|par jour|al d[ií]a|daily)/i
                ) || text.match(
                    /(?:produzione|production|producci.n)[^0-9]{0,25}([0-9][0-9.,\\s]*)[^0-9]{0,15}(?:punti|points|cultur)/i
                );
                if (d) out.daily = clean(d[1]);
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
                // \b impedisce "serve" dentro "server" (l'ora del
                // server veniva letta come CP richiesti).
                const n = text.match(
                    /\\b(?:richiesti|necessari|servono|serve|needed|required|ben.tigt|requis|erforderlich)\\b[^0-9]{0,25}([0-9][0-9.,\\s]*)/i
                );
                if (n) {
                    out.need = clean(n[1]);
                    out.matched = true;
                }
            }
            return out;
        }
    """

    def __init__(self, page: Page) -> None:
        self.page = page
        # Tab Residenza che l'ultima volta conteneva il riepilogo CP:
        # evita di riprovare tutte le tab a ogni ciclo.
        self._cp_tab: str | None = None
        # Ordine dei path dettaglio tile appreso dal server: su Legends
        # la scheda esce da /map?x=&y=, su T4.x da position_details.php.
        self._tile_detail_order: tuple[str, ...] | None = None
        # Route edificio che l'ultima volta ha aperto la pagina CP:
        # 'slot?id=' (nuova UI) oppure 'build.php?id=' (UI classica).
        self._cp_base: str | None = None
        # Extra dell'ultima pagina CP letta (nuova UI: slot
        # insediamento usati/totali e data stimata prossimo slot).
        self.cp_extra: dict = {}
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

    def _building_url_path(self, slot_id: int) -> str:
        """Path ``?id=<slot>`` coerente col routing rilevato del server.

        I server moderni (Travian Legends nuova UI) servono gli edifici
        su ``/building?id=N`` o ``/slot?id=N``; quelli legacy su
        ``build.php?id=N``. La scelta segue ``routing_mode`` del profilo
        calibrato; in dubbio si usa il path legacy che i server moderni
        reindirizzano comunque.
        """
        try:
            prof = profile_for_url(getattr(self.page, "url", "") or "")
            if prof.routing_mode == "modern":
                return f"building?id={int(slot_id)}"
        except Exception:  # noqa: BLE001
            pass
        return f"{selectors.PATH_BUILD}?id={int(slot_id)}"

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
                        // Nome dal div .name/.building: l'alt dell'icona
                        // 'annulla' (tasto cancel) NON e' il nome edificio.
                        const nameEl = el.querySelector('.name') || el.querySelector('.building');
                        let name = nameEl?.innerText?.trim() || '';
                        if (!name) {
                            // Fallback img[alt] escludendo le icone di annullamento.
                            const img = el.querySelector(
                                'img[alt]:not(.del):not([class*="cancel"]):not([onclick*="cancel"])'
                            );
                            name = img?.getAttribute('alt')?.trim() || '';
                        }
                        // Scarta i tooltip di annullamento letti per errore.
                        if (/^(annulla|cancel|abbrechen|annuler|iptal|avbryt)\b/i.test(name)) name = '';
                        // Livello reale da .lvl/.level (fallback: nel nome).
                        const lvlEl = el.querySelector('.lvl, .level');
                        let level = null;
                        if (lvlEl) {
                            const lm = (lvlEl.innerText || '').match(/(\\d+)/);
                            if (lm) level = parseInt(lm[1], 10);
                        }
                        const durationEl = el.querySelector('.buildDuration, .timer, .dur, .clocks');
                        let duration = durationEl?.innerText?.trim() || '';
                        // Slot di origine dal link build.php?id=N (se la riga lo contiene)
                        const link = el.querySelector('a[href*="build.php?id="]') || el.closest('a[href*="build.php?id="]');
                        let slotId = null;
                        if (link) {
                            const m = (link.getAttribute('href') || '').match(/build\\.php[?&]id=(\\d+)/);
                            if (m) slotId = parseInt(m[1], 10);
                        }
                        let gid = null;
                        const classStr = (el.className || '') + ' ' + Array.from(el.querySelectorAll('*')).map(c => c.className || '').join(' ');
                        const gm = classStr.match(/\\bgid(\\d+)\\b/i);
                        if (gm) gid = parseInt(gm[1], 10);
                        if (!gid && link) {
                            const gmLink = (link.getAttribute('href') || '').match(/[?&]gid=(\\d+)/);
                            if (gmLink) gid = parseInt(gmLink[1], 10);
                        }
                        if (name) {
                            items.push({ building: name, level: level, time_left: duration, slot_id: slotId, gid: gid });
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
                    # Nome dal div .name/.building: l'alt dell'icona
                    # 'annulla' (tasto cancel) NON e' il nome edificio.
                    name = ""
                    name_el = row.locator(".name, .building").first
                    if await name_el.count() > 0:
                        name = (await name_el.inner_text()).strip()
                    if not name:
                        img_el = row.locator(
                            "img[alt]:not(.del):not([class*='cancel'])"
                        ).first
                        if await img_el.count() > 0:
                            name = (
                                (await img_el.get_attribute("alt")) or ""
                            ).strip()
                    if _CANCEL_WORD.match(name or ""):
                        name = ""
                    level = 0
                    lvl_el = row.locator(".lvl, .level").first
                    if await lvl_el.count() > 0:
                        level = parse_int((await lvl_el.inner_text()).strip())
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
                            {
                                "building": name,
                                "level": level or None,
                                "time_left": duration,
                                "slot_id": slot_id,
                            }
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
        # Auto-calibrazione routing: il primo dorf1 caricato classifica
        # il server come legacy/modern nel profilo persistente.
        try:
            await calibrate_routing(self.page)
        except Exception:  # noqa: BLE001
            pass
        try:
            raw_fields: list = await self.page.evaluate("""
                () => {
                    const seen = {};
                    const slots = document.querySelectorAll(
                        '#resourceFieldContainer .buildingSlot, ' +
                        '#resourceFieldContainer a.resourceField, ' +
                        '#resourceFieldContainer [data-aid], ' +
                        '#village_map .buildingSlot, #villageMap .buildingSlot, ' +
                        '.buildingSlot, a[href*="build.php?id="], ' +
                        'a[href*="/slot?id="], area'
                    );
                    slots.forEach(slot => {
                        const href = slot.getAttribute('href') || '';
                        const cls = slot.getAttribute('class') || '';
                        let slotId = null;
                        // Vecchia UI: build.php?id=N o classe 'aN'.
                        // Nuova UI (/resources): href /slot?id=N, data-aid=N
                        // o classe 'buildingSlotN'.
                        const matchId = href.match(/build\\.php[?&]id=(\\d+)/) ||
                                        href.match(/\\/slot\\?[^#\\s]*?\\bid=(\\d+)/);
                        if (matchId) {
                            slotId = parseInt(matchId[1], 10);
                        } else {
                            const aid = slot.getAttribute('data-aid');
                            if (aid && /^\\d+$/.test(aid)) {
                                slotId = parseInt(aid, 10);
                            } else {
                                const matchCls = cls.match(/\\ba(\\d+)\\b/) ||
                                                 cls.match(/\\bbuildingSlot(\\d+)\\b/);
                                if (matchCls) slotId = parseInt(matchCls[1], 10);
                            }
                        }
                        // Solo i 18 campi risorsa del dorf1: link verso slot
                        // dorf2 (19-40) non devono inquinare la lista.
                        if (slotId === null || slotId < 1 || slotId > 18) return;

                        // gid risorsa: data-gid (nuova UI) o classe Travian
                        // gidN/gN — legno=1, argilla=2, ferro=3, grano=4.
                        let gid = -1;
                        const dataGid = slot.getAttribute('data-gid');
                        if (dataGid && /^\\d+$/.test(dataGid)) {
                            gid = parseInt(dataGid, 10);
                        } else {
                            const gidMatch = cls.match(/\\bg(?:id)?(\\d+)\\b/);
                            if (gidMatch) gid = parseInt(gidMatch[1], 10);
                        }

                        const title = (slot.getAttribute('title') || slot.getAttribute('alt') || slot.innerText || '').trim();

                        // Livello: badge numerico dello slot (.level vecchia
                        // UI, .labelLayer nuova UI) o classe 'levelN'.
                        let level = -1;
                        const lvlEl = slot.querySelector('.level, .lvl, .buildingLevel, .labelLayer, [class*="level"]');
                        const lvlSrc = lvlEl ? lvlEl.innerText : '';
                        const lvlMatch = (lvlSrc || '').match(/\\d+/);
                        if (lvlMatch) level = parseInt(lvlMatch[0], 10);
                        if (level < 0) {
                            const lvlCls = cls.match(/\\blevel(\\d+)\\b/);
                            if (lvlCls) level = parseInt(lvlCls[1], 10);
                        }

                        // Dedup per slot: tieni la versione piu' ricca.
                        const prev = seen[slotId];
                        if (!prev || (prev.gid < 0 && gid >= 0) || (!prev.title && title) || (prev.level < 0 && level >= 0)) {
                            seen[slotId] = {
                                slot_id: slotId,
                                gid: gid >= 0 ? gid : (prev ? prev.gid : -1),
                                title: title || (prev ? prev.title : ''),
                                level: level >= 0 ? level : (prev ? prev.level : -1),
                            };
                        }
                    });
                    return Object.values(seen);
                }
            """)
            if raw_fields and isinstance(raw_fields, list):
                for item in raw_fields:
                    s_id = item.get("slot_id")
                    title = item.get("title", "")
                    gid = safe_int(item.get("gid"), -1)
                    if s_id is None or not (1 <= int(s_id) <= 18):
                        continue
                    # Livello: title localizzato prima (es. "Bosco livello 3"),
                    # fallback sul badge numerico letto dal DOM.
                    lvl = parse_int(title)
                    if lvl <= 0:
                        lvl = max(0, safe_int(item.get("level"), 0))
                    rtype = (
                        FIELD_GID_TYPES.get(gid)
                        or guess_resource_type(title)
                    )
                    # Nuova UI: il title e' assente o coincide col numero di
                    # livello (innerText = ".labelLayer") — in quel caso il
                    # nome deriva dal tipo risorsa, non dal badge.
                    if not title or title.strip().isdigit():
                        title = rtype.value if rtype else title
                    fields.append(
                        Field(
                            slot_id=int(s_id),
                            name=title,
                            level=lvl,
                            # Il gid del DOM e' piu' affidabile del titolo
                            # localizzato; il guess resta come fallback.
                            resource_type=rtype,
                            gid=gid,
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
        if not fields:
            # Diagnostica drift markup: se entrambi i percorsi (JS + DOM)
            # tornano vuoti, logga URL e conteggi dei selettori chiave
            # cosi' il prossimo aggiornamento selettori parte dai dati.
            try:
                probe = await self.page.evaluate("""
                    () => {
                        const rfc = document.querySelector('#resourceFieldContainer');
                        const kids = rfc
                            ? Array.from(rfc.querySelectorAll('*'))
                                .slice(0, 40)
                                .map(el => el.tagName.toLowerCase() + '|' +
                                     (el.getAttribute('class') || '').slice(0, 60) + '|' +
                                     (el.getAttribute('href') || el.getAttribute('data-aid') ||
                                      el.getAttribute('data-slot') || ''))
                            : [];
                        return {
                            url: location.href,
                            buildingSlot: document.querySelectorAll('.buildingSlot').length,
                            buildLinks: document.querySelectorAll('a[href*="build.php?id="]').length,
                            areas: document.querySelectorAll('area').length,
                            rfcKids: kids,
                            rfcHtml: rfc ? rfc.innerHTML.slice(0, 3000) : null,
                        };
                    }
                """)
            except Exception as exc:  # noqa: BLE001
                probe = {"error": str(exc)}
            logger.warning(
                "read_fields: 0 campi dorf1 letti. Probe DOM: %s", probe
            )
            try:
                await capture_anomaly(
                    self.page,
                    "FIELDS_EMPTY",
                    village_id=self._current_newdid(),
                    extra={"probe": probe},
                )
            except Exception:  # noqa: BLE001
                pass
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

        # Auto-calibrazione routing anche dal dorf2 (href /slot|/building
        # degli interni -> 'modern', build.php?id=N -> 'legacy').
        try:
            await calibrate_routing(page)
        except Exception:  # noqa: BLE001
            pass

        try:
            interior_data = await page.evaluate("""
                () => {
                    const buildings = {};
                    // Scope alla mappa del villaggio: [data-aid] fuori
                    // dalla mappa (menu costruzione, tooltip) puo'
                    // veicolare il building-aid, non lo slot.
                    const mapScope = document.querySelector(
                        '#village_map, #villageMap, .villageMap, ' +
                        'map[name*="village" i], [id*="villagemap" i]'
                    ) || document;
                    const nodes = mapScope.querySelectorAll(
                        '.buildingSlot, a[href*="build.php"], ' +
                        'a[href*="dorf2.php"], a[href*="/building"], ' +
                        'area[href], [data-aid], [data-gid], ' +
                        '[data-slot], [data-did]'
                    );
                    const attr = (e, k) =>
                        (e && e.getAttribute) ? e.getAttribute(k) : null;
                    nodes.forEach(el => {
                        const cls = el.getAttribute('class') || '';
                        const inner = el.querySelector('a') || el;
                        const href =
                            (inner.getAttribute &&
                             inner.getAttribute('href')) ||
                            attr(el, 'href') || '';

                        // ---- slot_id ----------------------------------
                        // Fonti ordinate: href ?id=N > data-slot/data-did
                        // > classe aN (posizione sulla mappa) > data-aid
                        // (usato come slot solo in assenza di fonti
                        // affidabili — su UI moderne puo' essere il
                        // building-aid, cioe' il gid).
                        const idMatch = href.match(/[?&]id=(\\d+)/);
                        const dSlot =
                            attr(el, 'data-slot') ||
                            attr(el, 'data-slotid') ||
                            attr(el, 'data-did') ||
                            attr(inner, 'data-slot') ||
                            attr(inner, 'data-did');
                        const aCls = cls.match(/\\ba(\\d+)\\b/);
                        const aid = attr(el, 'data-aid') ||
                                    attr(inner, 'data-aid');
                        let slotId = null;
                        if (idMatch) slotId = parseInt(idMatch[1], 10);
                        else if (dSlot && /^\\d+$/.test(dSlot))
                            slotId = parseInt(dSlot, 10);
                        else if (aCls) slotId = parseInt(aCls[1], 10);
                        else if (aid && /^\\d+$/.test(aid))
                            slotId = parseInt(aid, 10);
                        // Solo edifici interni del dorf2
                        if (slotId === null || slotId < 19 || slotId > 40)
                            return;

                        // ---- gid --------------------------------------
                        // Fonti ordinate: data-gid > data-aid diverso
                        // dallo slotId (building-aid UI moderne) >
                        // classe gN/gidN del markup classico.
                        const dGid = attr(el, 'data-gid') ||
                                     attr(inner, 'data-gid');
                        const gCls = cls.match(/\\bg(\\d+)\\b/) ||
                                     cls.match(/\\bgid(\\d+)\\b/);
                        let gid = -1;
                        if (dGid && /^\\d+$/.test(dGid))
                            gid = parseInt(dGid, 10);
                        else if (
                            aid && /^\\d+$/.test(aid) &&
                            parseInt(aid, 10) !== slotId
                        )
                            gid = parseInt(aid, 10);
                        else if (gCls) gid = parseInt(gCls[1], 10);

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

                        // Merge per-campo: una versione piu' povera non
                        // puo' sovrascrivere gid/nome/livello gia' letti.
                        const prev = buildings[slotId];
                        if (!prev) {
                            buildings[slotId] = {
                                slot_id: slotId, gid: gid,
                                name: name, level: level
                            };
                        } else {
                            if (prev.gid < 0 && gid >= 0) prev.gid = gid;
                            if (!prev.name && name) prev.name = name;
                            if (prev.level === null && level !== null)
                                prev.level = level;
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
        # Dedup: la stessa costruzione puo' essere renderizzata da due
        # contenitori diversi (box coda + overview sidebar). Contarla due
        # volte saturerebbe fintamente max_build_queue e bloccherebbe la
        # coda parallela romana (dorf1+dorf2).
        seen: set = set()
        for q in raw_queue:
            name = str(q.get("building", "") or "").strip()
            # Rete di sicurezza: il tooltip del tasto annulla non e' un
            # edificio (il fix dei selettori lo esclude gia' a monte).
            if _CANCEL_WORD.match(name):
                continue
            duration_str = q.get("time_left", "")
            slot_id = q.get("slot_id")
            if isinstance(slot_id, int) and slot_id > 0:
                key = ("slot", slot_id)
            else:
                key = ("name", name.lower())
            if key in seen:
                continue
            seen.add(key)
            q_gid = q.get("gid")
            if isinstance(slot_id, int) and 1 <= slot_id <= 40:
                is_res = 1 <= slot_id <= 18
            elif isinstance(q_gid, int) and q_gid in (1, 2, 3, 4):
                is_res = True
            elif isinstance(q_gid, int) and q_gid >= 5:
                is_res = False
            else:
                is_res = guess_resource_type(name) is not None
            # Livello esplicito da .lvl/.level; fallback sul testo nome.
            q_level = q.get("level")
            level = (
                int(q_level)
                if isinstance(q_level, int) and q_level > 0
                else parse_int(name)
            )
            items.append(
                BuildQueueItem(
                    name=name,
                    level=level,
                    remaining_seconds=parse_timer(duration_str),
                    is_resource_field=is_res,
                    slot_id=slot_id if isinstance(slot_id, int) else 0,
                    gid=q_gid if isinstance(q_gid, int) and q_gid > 0 else 0,
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

                        // Coordinate controparte: href karte.php?x=..&y=..
                        // (o /karte?x=) poi pattern '(x|y)' nella riga.
                        let cx = null, cy = null;
                        const mapLink = row.querySelector(
                            'a[href*="karte"], a[href*="/map?x="]'
                        );
                        if (mapLink) {
                            const href = mapLink.getAttribute('href') || '';
                            const hm = href.match(
                                /[?&]x=(-?\\d+)[^0-9-]*(?:[?&]|\\&)y=(-?\\d+)|x=(-?\\d+).*?y=(-?\\d+)/
                            );
                            if (hm) {
                                cx = parseInt(hm[1] || hm[3], 10);
                                cy = parseInt(hm[2] || hm[4], 10);
                            }
                        }
                        if (cx === null || isNaN(cx)) {
                            const cm = rowText.match(
                                /\\((-?\\d{1,3})\\s*\\|\\s*(-?\\d{1,3})\\)/
                            );
                            if (cm) {
                                cx = parseInt(cm[1], 10);
                                cy = parseInt(cm[2], 10);
                            }
                        }
                        if (isNaN(cx)) cx = null;
                        if (isNaN(cy)) cy = null;

                        return {
                            type: mtype,
                            seconds: secs,
                            count: count,
                            attacker: other,
                            label: rowText.slice(0, 160),
                            cx: cx,
                            cy: cy,
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
                cx = item.get("cx")
                cy = item.get("cy")
                moves.append(
                    IncomingAttack(
                        movement_type=str(item.get("type") or "unknown"),
                        seconds_remaining=secs,
                        troop_count=safe_int(item.get("count") or 0),
                        arrival_ts=now + secs,
                        attacker=str(item.get("attacker") or ""),
                        label=str(item.get("label") or ""),
                        counterpart_x=(
                            safe_int(cx) if cx is not None else None
                        ),
                        counterpart_y=(
                            safe_int(cy) if cy is not None else None
                        ),
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
        cp_eval_js = self._CP_EVAL_JS

        # Route edificio: nuova UI '/slot?id=N' oppure classica
        # 'build.php?id=N' — la prima che valida l'URL viene usata
        # per le tab successive (cache _cp_base).
        bases = (
            [self._cp_base] if self._cp_base else []
        ) + [b for b in (
                "slot?id=", "building?id=", f"{selectors.PATH_BUILD}?id="
            ) if b != self._cp_base]
        slot_check = re.compile(
            rf"(?:(?:/slot|/building|build\.php)\?[^#]*"
            rf"\bid={int(residence_slot)}\b"
            rf"|/build/{int(residence_slot)}\b)"
        )

        accepted = None   # (suffix, data) con campi specifici matchati
        generic = None    # (suffix, data) solo match generico
        nav_link = None   # (suffix_tt, href) dalla navbar
        tried = set()     # suffix gia' navigati

        for base in bases:
            # Le tab &tt=N esistono solo su build.php: le UI nuove
            # (/slot, /building) espongono il link CP nella navbar.
            tab_list = (
                suffixes if base.startswith(selectors.PATH_BUILD) else [""]
            )
            for suffix in tab_list:
                url = f"{base}{residence_slot}{suffix}"
                if village_id:
                    url += f"&newdid={village_id}"
                tried.add(suffix)
                try:
                    await self._goto(
                        _abs_game_url(self.page.url or "", url),
                    )
                except Exception:
                    continue
                if not slot_check.search(self.page.url or ""):
                    # Route non valida su questo server (es. /slot su
                    # vecchia UI): passa alla route alternativa.
                    break
                self._cp_base = base
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
            if accepted is not None or nav_link is not None:
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
            if not daily and (have or need):
                # Safety net: su altre UI la tabella "Punti cultura al
                # giorno" puo' arrivare via XHR dopo il primo render.
                try:
                    await self.page.wait_for_timeout(1000)
                    retry = await self.page.evaluate(cp_eval_js) or {}
                    rd = safe_int(retry.get("daily", 0))
                    if rd:
                        daily = rd
                        data = retry
                except Exception:  # noqa: BLE001
                    pass
            self.cp_extra = {
                k: data[k] for k in
                ("slots_used", "slots_total", "next_slot")
                if data.get(k) is not None
            }
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

    async def read_culture_points_overview(self) -> tuple[int, int, int]:
        """CP senza Residenza/Palazzo: pagina corrente, profilo, panoramica.

        Alcune versioni espongono i Punti Cultura anche fuori dalla
        Residenza (profilo giocatore spieler.php, panoramica villaggi
        dorf3.php, box account/pagine /profile delle nuove UI): probe
        best-effort con le stesse regex di ``read_culture_points``.
        Ritorna (0, 0, 0) se nessuna pagina espone il dato.
        """
        if not self.page:
            return (0, 0, 0)

        async def _eval_cp() -> dict:
            try:
                data = await self.page.evaluate(self._CP_EVAL_JS)
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        async def _scan(url: str | None) -> dict:
            """Eval CP su ``url`` (o pagina corrente), seguendo il link
            diretto alla sotto-pagina CP se la nav lo espone (es. tab
            'Punti cultura' di /village/statistics nella nuova UI)."""
            if url:
                try:
                    await self._goto(url)
                except Exception:
                    return {}
            data = await _eval_cp()
            if data.get("matched"):
                return data
            nav = data.get("cp_nav_href")
            if nav:
                try:
                    await self._goto(nav)
                except Exception:
                    return data
                data = await _eval_cp()
            return data

        async def _finish(data: dict) -> tuple[int, int, int]:
            """Salva gli extra (slots/ETA nuova UI) e ritorna la tupla.

            Se il daily e' a zero ma have/need sono stati letti, la
            tabella 'Punti cultura al giorno' era probabilmente non
            ancora renderizzata (XHR post-domcontentloaded): un
            secondo eval a breve distanza la cattura.
            """
            have = safe_int(data.get("have", 0))
            need = safe_int(data.get("need", 0))
            daily = safe_int(data.get("daily", 0))
            if not daily and (have or need):
                try:
                    await self.page.wait_for_timeout(1000)
                    retry = await _eval_cp()
                    rd = safe_int(retry.get("daily", 0))
                    if rd:
                        daily = rd
                        data = retry
                except Exception:  # noqa: BLE001
                    pass
            self.cp_extra = {
                k: data[k] for k in
                ("slots_used", "slots_total", "next_slot")
                if data.get(k) is not None
            }
            return (have, need, daily)

        # 1. La pagina corrente potrebbe gia' esporre i CP (es. box
        #    account delle nuove UI): nessuna navigazione extra.
        data = await _scan(None)
        if data.get("matched"):
            return _finish(data)

        # 2. Candidati: link reali della navbar verso profilo/
        #    panoramica + rotte classiche spieler.php e dorf3.php
        #    (dorf3.php redirige a /village/statistics sulla nuova UI).
        try:
            hrefs = await self.page.evaluate(
                """() => Array.from(document.querySelectorAll('a[href]'))
                    .map(a => a.href || '')
                    .filter(h => /spieler\\.php|dorf3\\.php|\\/profile|profil|village\\/statistics/i.test(h))"""
            )
        except Exception:
            hrefs = []
        candidates: list[str] = []
        for h in hrefs or []:
            if h and h not in candidates:
                candidates.append(h)
        for path in ("dorf3.php", "spieler.php", "village/statistics"):
            url = _abs_game_url(self.page.url or "", path)
            if url not in candidates:
                candidates.append(url)

        for url in candidates[:4]:
            data = await _scan(url)
            if data.get("matched"):
                return await _finish(data)

        await self._dump_page_text("culture_points_fail")
        logger.info(
            "Punti cultura non letti (nessuna Residenza e nessuna "
            "pagina profilo/panoramica con CP): "
            "dump in debug/culture_points_fail.txt"
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

    # Snapshot JS dell'Accademia (gid 22): per ogni riga con icona
    # 'unit uN' raccoglie testo, timer, controlli (bottone/link Ricerca
    # con stato enabled) e costi risorse; segnala inoltre se esiste un
    # timer di ricerca attivo nel contenuto principale.
    _JS_ACADEMY_SNAPSHOT = """
        () => {
            const num = (t) => {
                const m = String(t || '').match(/(\\d[\\d.,\\s]*)/);
                return m ? parseInt(m[1].replace(/[^\\d]/g, ''), 10) : 0;
            };
            const out = {rows: [], active: false};
            const scope = document.querySelector(
                '#content, #build, .buildingDetails, .content, main'
            ) || document.body;
            out.active = !!scope.querySelector(
                '.timer, .dur, .clocks, [class*="timer" i]'
            );
            const seen = new Set();
            // Selettori moderni: qualsiasi tag (img/i/span/div/svg) con
            // classe contenente u1..u30 — le UI nuove non usano piu'
            // 'unit uN' ma solo 'uN'. Il regex \\bu(\\d{1,2})\\b filtra
            // i falsi positivi ('menu1', 'sub2'…).
            const uSels = [];
            for (let n = 1; n <= 30; n++) {
                uSels.push('[class*="u' + n + '"]');
            }
            uSels.push(
                '[class*="unit u"]', '[class*="unit_u"]',
                'img.unit', 'i.unit', 'span.unit', 'a.unit'
            );
            const iconSel = uSels.join(',');
            // Scope multipli: .research/.information (layout moderni)
            // oltre ai contenitori classici; la barra menu superiore
            // viene esclusa icona per icona.
            const scopes = document.querySelectorAll(
                '.research, .information, .buildingDetails, #build, ' +
                '#content, .content, main'
            );
            const roots = scopes.length ? Array.from(scopes) : [document.body];
            const iconSeen = new Set();
            const icons = [];
            for (const s of roots) {
                for (const icon of s.querySelectorAll(iconSel)) {
                    if (iconSeen.has(icon)) continue;
                    iconSeen.add(icon);
                    if (icon.closest(
                        '.headerBar, #headerBar, .topBar, #topBar, ' +
                        '#topnav, header, nav, .navigation'
                    )) continue;
                    icons.push(icon);
                }
            }
            for (const icon of icons) {
                const m = (icon.getAttribute('class') || '')
                    .match(/\\bu(\\d{1,2})\\b/);
                if (!m) continue;
                const uN = parseInt(m[1], 10);
                if (uN < 1 || uN > 30) continue;
                const row = icon.closest(
                    'tr, li, .research, .troop, .unitRow, ' +
                    '.details, .item, .researchRow'
                ) || icon.closest('div') || icon.parentElement;
                if (!row || seen.has(row)) continue;
                seen.add(row);
                const rec = {
                    u: uN,
                    text: (row.innerText || '')
                        .replace(/\\s+/g, ' ').trim().slice(0, 500),
                    timer: !!row.querySelector(
                        '.timer, .dur, .clocks, [class*="timer" i]'
                    ),
                    controls: [],
                    costs: {},
                };
                for (const c of row.querySelectorAll(
                    'button, a[href], input[type="submit"], ' +
                    'input[type="button"], [role="button"]'
                )) {
                    const dis = !!(
                        c.disabled ||
                        c.getAttribute('disabled') !== null ||
                        /\\b(disabled|grey|inactive)\\b/i
                            .test(c.className || '') ||
                        c.getAttribute('aria-disabled') === 'true'
                    );
                    rec.controls.push({
                        text: String(
                            c.innerText || c.value || ''
                        ).trim().slice(0, 80),
                        href: c.getAttribute
                            ? (c.getAttribute('href') || '') : '',
                        disabled: dis,
                    });
                }
                // Costi ricerca: icone/elementi risorsa nella riga
                // (classi r1-r4 o keyword localizzate). Il numero si
                // legge nel nodo, nel fratello o nel genitore (se
                // corto, per non pescare cifre della riga intera).
                const groups = [
                    ['r1', 'lumber', 'wood', 'holz'],
                    ['r2', 'clay', 'brick', 'lehm'],
                    ['r3', 'iron', 'eisen'],
                    ['r4', 'crop', 'wheat', 'getreide'],
                ];
                const resKeys = ['wood', 'clay', 'iron', 'crop'];
                groups.forEach((names, i) => {
                    const sel = names
                        .map(n => `[class*="${n}" i]`)
                        .join(',');
                    for (const el of row.querySelectorAll(sel)) {
                        let v = num(el.innerText || '');
                        if (!v && el.nextElementSibling) {
                            v = num(el.nextElementSibling.innerText || '');
                        }
                        if (!v && el.parentElement) {
                            const pt = el.parentElement.innerText || '';
                            if (pt.length <= 40) v = num(pt);
                        }
                        if (v > 0) { rec.costs[resKeys[i]] = v; return; }
                    }
                });
                out.rows.push(rec);
            }
            return out;
        }
    """

    async def _goto_building_slot(
        self, slot_id: int, village_id: int = 0
    ) -> bool:
        """Naviga a ``?id=<slot>`` senza redirect inutili.

        Appende ``&newdid`` solo quando il villaggio attivo differisce da
        ``village_id`` (evita il round-trip di cambio contesto). Ritorna
        False se la navigazione fallisce o se l'URL finale non e' una
        pagina edificio.
        """
        if not self.page or not slot_id:
            return False
        url = self._building_url_path(slot_id)
        if village_id and self._current_newdid() != int(village_id):
            url += f"&newdid={village_id}"
        try:
            await self._goto(_abs_game_url(self.page.url or "", url))
        except Exception:  # noqa: BLE001
            return False
        try:
            from .actions import _is_building_url  # no cicli

            if not _is_building_url(self.page.url or ""):
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    async def _detect_building_gid(self) -> int | None:
        """gid reale della pagina edificio aperta (``div#build.gidN``).

        Fonte di verita' sul tipo di edificio effettivamente caricato:
        serve a intercettare mapping slot->gid errati della mappa
        villaggio (es. UI moderne con data-aid ambiguo). ``None`` se la
        classe gid non e' leggibile.
        """
        if not self.page:
            return None
        try:
            gid = await self.page.evaluate("""
                () => {
                    const b = document.querySelector(
                        '#build, .buildingDetails, .build'
                    );
                    const pick = (el) => {
                        if (!el) return null;
                        const cls = el.getAttribute('class') || '';
                        const m = cls.match(/\\bgid?(\\d{1,2})\\b/);
                        return m ? parseInt(m[1], 10) : null;
                    };
                    let g = pick(b);
                    if (g === null) {
                        g = pick(document.querySelector(
                            '#content [class*="gid"], #build [class*="gid"]'
                        ));
                    }
                    return g;
                }
            """)
            return int(gid) if gid is not None else None
        except Exception:  # noqa: BLE001
            return None

    async def _locate_gid_slot(
        self,
        target_gid: int,
        village_id: int,
        slot_corrections: dict[int, int],
    ) -> int | None:
        """Rilegge la mappa dorf2 e cerca lo slot con ``gid=target_gid``.

        Effetto collaterale: ``slot_corrections`` viene popolato con i
        gid reali rilevati (rettifiche da applicare a ``state.fields``).
        Ritorna lo slot_id del target o ``None``.
        """
        try:
            interiors = await self.read_interior_fields()
        except Exception:  # noqa: BLE001
            return None
        if not interiors:
            return None
        found: int | None = None
        for f in interiors:
            gid = int(getattr(f, "gid", -1) or -1)
            sid = int(getattr(f, "slot_id", 0) or 0)
            if sid < 19 or sid > 40 or gid < 0:
                continue
            slot_corrections.setdefault(sid, gid)
            if gid == target_gid and found is None:
                found = sid
        return found

    async def read_academy_state(
        self, academy_slot: int, village_id: int = 0
    ) -> dict:
        """Stato dell'Accademia (gid 22) del villaggio corrente.

        Naviga allo slot con URL coerente col routing rilevato
        (``build.php?id=N`` legacy, ``building?id=N`` modern) e legge:

        - ``research_active``: True se una ricerca e' gia' in corso
          (timer nel contenuto o riga con timer) — il planner non deve
          sovrapporre azioni;
        - ``units``: ``{"tN": {"status", "label", "costs"}}`` con
          status in researched|researchable|blocked|in_progress —
          'researchable' = pulsante Ricerca abilitato, 'blocked' =
          risorse insufficienti/prerequisiti mancanti;
        - ``researching``: chiavi tN in corso di ricerca.

        Ritorna {} se la pagina non espone dati riconoscibili. Quando il
        gid rilevato diverge da 22 (mappa villaggio errata) la chiave
        ``slot_corrections`` riporta le rettifiche slot->gid da applicare
        a ``state.fields`` e ``resolved_slot`` lo slot reale dell'Accademia.
        """
        if not self.page or not academy_slot:
            return {}
        if not await self._goto_building_slot(academy_slot, village_id):
            logger.debug(
                "read_academy_state: navigazione slot %s fallita o "
                "URL non edificio (%s)",
                academy_slot, (self.page.url or "")[:120],
            )
            return {}

        # Coerenza gid: la pagina aperta deve essere davvero l'Accademia
        # (gid 22). Su UI moderne la mappa puo' aver associato slot->gid
        # sbagliati (data-aid ambiguo): si corregge la mappa e si cerca
        # lo slot reale.
        resolved_slot = academy_slot
        slot_corrections: dict[int, int] = {}
        detected = await self._detect_building_gid()
        if detected is not None and detected != GID_ACADEMY:
            logger.info(
                "read_academy_state: slot %s non e' Accademia "
                "(gid=%s rilevato, atteso 22) — correzione mappa",
                academy_slot, detected,
            )
            slot_corrections[academy_slot] = detected
            try:
                await capture_anomaly(
                    self.page,
                    "ACADEMY_WRONG_GID",
                    slot_id=academy_slot,
                    expected_gid=GID_ACADEMY,
                    detected_gid=detected,
                    village_id=village_id,
                )
            except Exception:  # noqa: BLE001
                pass
            # Recovery 1: rilettura dorf2 (JS gia' corretto per
            # data-aid/data-gid) per localizzare lo slot con gid 22.
            new_slot = await self._locate_gid_slot(
                GID_ACADEMY, village_id, slot_corrections
            )
            if new_slot is None:
                # Recovery 2: scansione selettiva degli slot 19-40 fino a
                # trovare div#build.gid22. Solo slot non marcati vuoti
                # dalla mappa corretta — i g0 sono aree libere.
                empty = {
                    sid for sid, g in slot_corrections.items() if g == 0
                }
                for sid in range(19, 41):
                    if sid in empty:
                        continue
                    if not await self._goto_building_slot(sid, village_id):
                        continue
                    g = await self._detect_building_gid()
                    if g is not None and g != GID_ACADEMY:
                        slot_corrections.setdefault(sid, g)
                    if g == GID_ACADEMY:
                        new_slot = sid
                        break
            if new_slot is None:
                logger.debug(
                    "read_academy_state: nessun slot gid22 trovato "
                    "(correzioni: %s)",
                    slot_corrections,
                )
                return {"slot_corrections": slot_corrections}
            resolved_slot = new_slot
            # Naviga allo slot reale e verifica il gid ancora una volta
            # (il locate gia' l'ha aperto nello scan; nel path dorf2
            # serve la navigazione).
            if not await self._goto_building_slot(
                resolved_slot, village_id
            ):
                return {"slot_corrections": slot_corrections}
            final_gid = await self._detect_building_gid()
            if final_gid is not None and final_gid != GID_ACADEMY:
                return {"slot_corrections": slot_corrections}
        try:
            raw = await self.page.evaluate(self._JS_ACADEMY_SNAPSHOT)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "read_academy_state: eval fallita su %s: %s",
                (self.page.url or "")[:120], exc,
            )
            return {}
        state = normalize_academy_state(raw)
        if not state:
            logger.debug(
                "read_academy_state: markup non riconosciuto su %s",
                (self.page.url or "")[:120],
            )
            # Cattura l'HTML reale dell'Accademia per blindarlo nei
            # test parametrici (fixture replay harness).
            try:
                await capture_anomaly(
                    self.page,
                    "ACADEMY_UNREADABLE",
                    slot_id=academy_slot,
                    expected_gid=22,
                    village_id=village_id,
                )
            except Exception:  # noqa: BLE001
                pass
            # La rettifica della mappa non si perde col fallimento del
            # parse: l'orchestratore la applica a state.fields.
            if slot_corrections:
                return {"slot_corrections": dict(slot_corrections)}
        else:
            # Metadati di recovery per il chiamante: lo slot reale
            # risolto e le rettifiche slot->gid da applicare a
            # state.fields (mappa villaggio).
            state["resolved_slot"] = resolved_slot
            if slot_corrections:
                state["slot_corrections"] = dict(slot_corrections)
        return state

    async def read_trainable_units(
        self, trainer_slot: int, village_id: int = 0
    ) -> set[str]:
        """Chiavi 'tN' delle unita' sbloccate (addestrabili) nel trainer.

        La form di addestramento (Caserma/Scuderia/Officina o
        Residenza/Palazzo) espone gli input ``tN`` solo per le unita'
        gia' ricercate in Accademia: e' la verita' operativa su
        'sbloccata' usata dal planner quando il DOM dell'Accademia
        omette l'unita' (es. UI legacy che nasconde le ricercate).
        """
        if not self.page or not trainer_slot:
            return set()
        url = self._building_url_path(trainer_slot)
        if village_id:
            url += f"&newdid={village_id}"
        try:
            await self._goto(_abs_game_url(self.page.url or "", url))
        except Exception:  # noqa: BLE001
            return set()
        try:
            keys = await self.page.evaluate("""
                () => {
                    const out = new Set();
                    for (const inp of document.querySelectorAll(
                        'form input[name^="t"], #trainUnits input, ' +
                        '.trainUnits input, .innerTrainUnitBox input'
                    )) {
                        const m = String(inp.name || '')
                            .match(/^t(\\d{1,2})$/);
                        if (m) out.add('t' + m[1]);
                    }
                    if (!out.size) {
                        const scope = document.querySelector(
                            '#trainUnits, .trainUnits, ' +
                            '.innerTrainUnitBox, form'
                        );
                        if (scope) {
                            for (const icon of scope.querySelectorAll(
                                '[class*="unit u"], img.unit, ' +
                                'i.unit, span.unit'
                            )) {
                                const m = (icon.getAttribute('class') || '')
                                    .match(/\\bu(\\d{1,2})\\b/);
                                if (m) out.add('t' + m[1]);
                            }
                        }
                    }
                    return Array.from(out);
                }
            """)
            return {
                str(k) for k in (keys or [])
                if str(k).startswith("t") and str(k)[1:].isdigit()
            }
        except Exception:  # noqa: BLE001
            return set()

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

        Su Legends (es. ts30) la scheda tile esce dal pannello della pagina
        mappa (``karte.php?x=X&y=Y`` / ``/map``); su T4.x da
        ``position_details.php?x=X&y=Y``. I path vengono provati in
        ordine — quello che funziona e' ricordato per il resto della
        sessione — e prima di estrarre si attende che la scheda tile sia
        davvero renderizzata: leggere il DOM appena dopo il goto produceva
        falsi "occupato" (parole come "villaggio" nella sidebar).

        Ritorna: {occupied, free, crop_fields, text, settle_link, ok}
        """
        if not self.page:
            return {"occupied": True, "crop_fields": 0, "text": "", "settle_link": False}
        paths = self._tile_detail_order or (
            selectors.PATH_MAP, "position_details.php"
        )
        data: dict | None = None
        for path in paths:
            try:
                await self._goto(
                    _abs_game_url(self.page.url or "", f"{path}?x={x}&y={y}"),
                )
            except Exception:
                continue
            # La scheda tile si monta via JS dopo il DOMContentLoaded:
            # attende pannello dettagli, link 'Fonda' o marcatori tipici
            # della scheda. Se non compaiono, il path non serve (su
            # Legends position_details.php non esiste).
            try:
                await self.page.wait_for_function(
                    """() => {
                        const body = (document.body && document.body.innerText) || '';
                        return !!document.querySelector(
                            '#map_details, .map_details, #tileDetails, .tileDetails, ' +
                            '#map_infobox, .infobox, #positionDetails, .position-details, ' +
                            '.tile-details, #map_popup, #popupMap, .popupContent, ' +
                            '.detailInfo, #tile_details, .mappanel'
                        ) || !!document.querySelector('a[href*="settler"]')
                        || /(?:campi?\\s*(?:di\\s+)?grano|crop\\s*fields?|getreidefelder|abitanti|inhabitants|popolazione|population|distribuzione\\s+del|land\\s+distribution|valle\\s+abbandonata|abandoned\\s+valley|non\\s+occupat|unoccupied)/i.test(body);
                    }""",
                    timeout=4000,
                )
            except Exception:
                continue
            try:
                data = await self._eval_map_tile()
            except Exception:
                data = None
            # Dato affidabile solo se la scheda e' stata isolata/letta.
            if data and data.get("ok"):
                # Il path che ha funzionato passa in testa per le letture
                # successive (T4 -> position_details, Legends -> mappa).
                self._tile_detail_order = (
                    (path,) + tuple(p for p in paths if p != path)
                )
                break
        if not data or not data.get("ok"):
            return {"occupied": True, "crop_fields": 0, "text": "", "settle_link": False}
        data.pop("panel", None)
        return data

    async def _eval_map_tile(self) -> dict | None:
        """Estrae occupazione/campi grano dalla scheda tile corrente.

        Il testo viene letto dal pannello dettagli o, se non esiste un
        contenitore noto (Legends), dal piu' piccolo elemento con i
        marcatori della scheda risalendo ai genitori finche' resta di
        dimensione "scheda": mai il body intero, dove le parole della
        sidebar farebbero scattare falsi "occupato".
        """
        return await self.page.evaluate("""
                () => {
                    const MARK = /(?:campi?\\s*(?:di\\s+)?grano|crop\\s*fields?|getreidefelder|abitanti|inhabitants|popolazione|population|distribuzione\\s+del|land\\s+distribution|valle\\s+abbandonata|abandoned\\s+valley)/i;
                    const panel = document.querySelector(
                        '#map_details, .map_details, #tileDetails, .tileDetails, ' +
                        '#map_infobox, .infobox, #positionDetails, .position-details, ' +
                        '.tile-details, #map_popup, #popupMap, .popupContent, ' +
                        '.detailInfo, #tile_details, .mappanel'
                    );
                    let text = (panel && panel.innerText) || '';
                    if (!text) {
                        // Piu' piccolo contenitore con marcatori scheda,
                        // poi si risale ai genitori finche' il testo resta
                        // sotto i 3000 caratteri (scheda intera, non la
                        // pagina con sidebar e navbar).
                        let best = null;
                        const cand = document.querySelectorAll(
                            'div, section, article, aside, table, td, li, p'
                        );
                        for (const el of cand) {
                            const t = el.innerText || '';
                            if (t && t.length <= 3000 && MARK.test(t)
                                && (!best || t.length < (best.innerText || '').length)) {
                                best = el;
                            }
                        }
                        let node = best;
                        while (node && node.parentElement
                               && node.parentElement !== document.body
                               && (node.parentElement.innerText || '').length <= 3000) {
                            node = node.parentElement;
                        }
                        text = (node && node.innerText) || '';
                    }
                    const ok = !!text && (!!panel || MARK.test(text));

                    // Distribuzione campi "3-3-3-9" (9c) o "1-1-1-15" (15c).
                    // Su Legends i separatori possono essere celle/whitespace,
                    // slash, punti o virgole — non solo trattini.
                    const dist = text.match(/(\\d)\\s*[-\\u2212\\u2010-\\u2015\\/.,:\\s]\\s*(\\d)\\s*[-\\u2212\\u2010-\\u2015\\/.,:\\s]\\s*(\\d)\\s*[-\\u2212\\u2010-\\u2015\\/.,:\\s]\\s*(\\d+)/);
                    let cropFields = 0;
                    if (dist) {
                        cropFields = parseInt(dist[4], 10) || 0;
                    } else {
                        // ts30: "6\\tCampi di grano" — 'di' opzionale.
                        const cm = text.match(/(\\d+)\\s*(?:campi?\\s*(?:di\\s+)?grano|crop\\s*fields?|getreidefelder|campos?\\s+de\\s+cereal|champs?\\s+de\\s+c[eé]r[eé]ales?)/i);
                        if (cm) cropFields = parseInt(cm[1], 10) || 0;
                        const c9 = text.match(/\\b(9|15)[ -]?[cç]\\b/i) || text.match(/\\b(9|15)[ ]?(?:cropper|croplog|grano)\\b/i);
                        if (!cropFields && c9) cropFields = parseInt(c9[1], 10) || 0;
                    }

                    const settle = !!document.querySelector(
                        'a[href*="a=settler"], a[href*="settler"]'
                    );
                    // "villaggio"/"village" esclusi dai marcatori occupato:
                    // compaiono nella sidebar di ogni pagina.
                    const occupiedMark = /(?:abitanti|inhabitants|popolazione|population|giocatore|player|proprietario|owner|alleanza|alliance|trib[uù]|tribe|oasi\\s+occupat|occupied\\s+oasis|natura|natar)/i.test(text);
                    const freeTile = /(?:valley|valle|unoccupied|libero|libera|free|terra libera|abandoned|disabitata|non occupat|uninhabited|distribuzione del territorio|land distribution|campi?\\s*(?:di\\s+)?grano|crop\\s*fields?|getreidefelder)/i.test(text);

                    return {
                        occupied: occupiedMark && !freeTile && !settle,
                        free: freeTile || settle,
                        crop_fields: cropFields,
                        settle_link: settle,
                        panel: !!panel,
                        ok: ok,
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
        Canali di estrazione, in ordine: DOM della griglia (T4.4/T4.5,
        incluse iframe), poi risposte JSON XHR/fetch captate durante il
        caricamento (Travian Legends: api/v1/map/position e simili).
        """
        if not self.page:
            return []

        # Travian Legends renderizza la mappa via XHR JSON: le risposte
        # vengono captate e parsate quando il DOM non espone i tile.
        captured: list = []

        def _on_response(resp) -> None:
            try:
                rt = resp.request.resource_type
                if rt and rt not in ("xhr", "fetch"):
                    return
            except Exception:
                pass
            captured.append(resp)

        try:
            self.page.on("response", _on_response)
        except Exception:
            pass

        try:
            try:
                await self._goto(
                    _abs_game_url(self.page.url or "", f"{selectors.PATH_MAP}?x={center_x}&y={center_y}"),
                )
                # T4.5/T4.6/Legends: la griglia mappa e' renderizzata via
                # JS/XHR dopo il DOMContentLoaded — attendiamo la comparsa
                # di almeno un tile o di una risposta mappa (timeout
                # breve, non fatale: il dump diagnostico copre i casi ko).
                try:
                    await self.page.wait_for_selector(
                        "a[href*='position_details.php'], a[href*='karte.php?x='], "
                        "map area, #mapContainer a, [id*='tile' i], [data-x][data-y], "
                        "[data-cell-id], .map .cell, #map .tile",
                        timeout=4500,
                    )
                except Exception:
                    pass
                # Attesa breve extra per XHR mappa ancora in volo.
                if not captured:
                    try:
                        await self.page.wait_for_timeout(1200)
                    except Exception:
                        pass
            except Exception:
                return []

            # 1) Estrazione DOM (tutti i frame: la mappa puo' vivere in
            #    un iframe).
            tiles: list = []
            for frame in list(getattr(self.page, "frames", [self.page])):
                try:
                    part = await frame.evaluate("""
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
                    if isinstance(part, list):
                        tiles.extend(part)
                except Exception:
                    continue

            out = [t for t in tiles if isinstance(t, dict)]

            # 1.5) Scheda singolo tile: su Legends /map?x=&y= mostra il
            #      dettaglio della casella richiesta (es. "Valle
            #      abbandonata (138|38) ... 6 Campi di grano") direttamente
            #      nel body — si emette come tile a distanza 0.
            if not out:
                try:
                    card = await self._eval_map_tile()
                except Exception:
                    card = None
                if card and card.get("text") and (
                    card.get("crop_fields")
                    or card.get("free")
                    or card.get("occupied")
                ):
                    otext = str(card.get("text") or "")
                    is_oasis = bool(re.search(r"oas[ie]", otext, re.I))
                    occ = bool(card.get("occupied")) and not is_oasis
                    out = [{
                        "x": int(center_x),
                        "y": int(center_y),
                        "village_name": "Oasi" if is_oasis else "",
                        "population": 1 if occ else 0,
                        "occupied": occ,
                        "is_oasis": is_oasis,
                        "crop_fields": int(card.get("crop_fields") or 0),
                        "distance": 0.0,
                    }]

            # 2) Fallback Legends: tile dalle risposte JSON mappa
            #    captate durante il caricamento (api/v1/map/position).
            if not out and captured:
                try:
                    out = await self._map_xhr_tiles(
                        captured, center_x, center_y, radius
                    )
                except Exception:
                    logger.debug(
                        "scan_map_area (%s|%s): parsing XHR mappa fallito",
                        center_x, center_y, exc_info=True,
                    )
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
                    noisy = (
                        "/js/", "/css/", "/img", "/font", "/static/",
                        "/assets/", "i18n", ".woff", ".png", ".svg",
                    )
                    dbg["xhrUrls"] = [
                        u for u in
                        (getattr(r, "url", "") for r in captured)
                        if u and not any(k in u.lower() for k in noisy)
                    ][:16]
                    logger.debug(
                        "scan_map_area (%s|%s): 0 tile estratti, dump=%s",
                        center_x, center_y, dbg,
                    )
                except Exception:
                    pass
            return out
        except Exception:
            return []
        finally:
            try:
                self.page.remove_listener("response", _on_response)
            except Exception:
                pass

    async def _map_xhr_tiles(
        self,
        responses: list,
        center_x: int,
        center_y: int,
        radius: int,
    ) -> list[dict]:
        """Estrae i tile mappa dalle risposte JSON captate (Travian
        Legends popola la griglia via api/v1/map/position e simili).

        Cerca ricorsivamente dizionari con chiavi x/y nel payload. Prima
        passata solo su URL "da mappa" (map/karte/position/tile); se non
        produce nulla, seconda passata su tutte le XHR JSON.
        """
        found: dict[tuple[int, int], dict] = {}

        def _walk(node, depth: int = 0) -> None:
            if depth > 6:
                return
            if isinstance(node, dict):
                if "x" in node and "y" in node:
                    t = self._tile_from_map_json(node, center_x, center_y)
                    if t is not None:
                        found.setdefault((t["x"], t["y"]), t)
                for v in node.values():
                    _walk(v, depth + 1)
            elif isinstance(node, list):
                for v in node:
                    _walk(v, depth + 1)

        for want_mapish in (True, False):
            for resp in responses:
                url = (getattr(resp, "url", "") or "").lower()
                mapish = any(
                    k in url for k in ("map", "karte", "position", "tile")
                )
                if want_mapish != mapish:
                    continue
                try:
                    payload = await resp.json()
                except Exception:
                    continue
                _walk(payload)
            if found:
                break

        return [
            t for t in found.values() if t["distance"] <= radius * 1.5
        ]

    @staticmethod
    def _tile_from_map_json(
        d: dict, center_x: int, center_y: int
    ) -> dict | None:
        """Normalizza un tile dal payload JSON della mappa Legends."""
        try:
            x = int(d.get("x"))
            y = int(d.get("y"))
        except (TypeError, ValueError):
            return None

        def _num(*vals) -> int:
            for v in vals:
                if v is None:
                    continue
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    continue
            return 0

        village = d.get("village") if isinstance(d.get("village"), dict) else {}
        oasis = d.get("oasis") if isinstance(d.get("oasis"), dict) else {}
        ttype = str(d.get("type") or d.get("tileType") or "").lower()
        is_oasis = bool(oasis) or bool(d.get("isOasis")) or "oasis" in ttype
        pop = _num(
            d.get("population"), d.get("pop"), village.get("population")
        )
        owner = (
            d.get("playerId") or d.get("player") or d.get("owner")
            or d.get("uid") or village.get("playerId")
        )
        name = str(
            d.get("title") or d.get("name") or village.get("name") or ""
        )
        if not name:
            name = "Oasi" if is_oasis else f"Tile ({x}|{y})"

        # Distribuzione campi: "3-3-3-9" / "1 1 1 15" / "3339" / "11115"
        # oppure chiave cropFields nei valori del tile (profondita' <= 2).
        crop = _num(d.get("cropFields"), d.get("crop_fields"))
        if not crop:
            strs: list[str] = []

            def _collect(n, depth: int) -> None:
                if depth > 2:
                    return
                if isinstance(n, str):
                    strs.append(n)
                elif isinstance(n, dict):
                    for v in n.values():
                        _collect(v, depth + 1)
                elif isinstance(n, list):
                    for v in n:
                        _collect(v, depth + 1)

            _collect(d, 0)
            dist_re = re.compile(
                r"(\d)\s*[-\u2212\u2010-\u2015/.,:\s]\s*(\d)"
                r"\s*[-\u2212\u2010-\u2015/.,:\s]\s*(\d)"
                r"\s*[-\u2212\u2010-\u2015/.,:\s]\s*(\d+)"
            )
            compact_re = re.compile(r"^\s*(\d)(\d)(\d)(\d{1,2})\s*$")
            for s in strs:
                m = dist_re.search(s) or compact_re.search(s)
                if m:
                    crop = int(m.group(4))
                    break

        dist = ((x - center_x) ** 2 + (y - center_y) ** 2) ** 0.5
        return {
            "x": x,
            "y": y,
            "village_name": name,
            "population": pop,
            "occupied": (not is_oasis) and (bool(owner) or pop > 0),
            "is_oasis": is_oasis,
            "oasis_type": str(
                oasis.get("type") or oasis.get("resType")
                or d.get("oasisType") or ("crop" if is_oasis else "")
            ),
            "crop_fields": crop,
            "distance": dist,
        }

    async def read_tile_details(self, x: int, y: int) -> dict:
        """Scheda tile completa per la Smart Pre-Raid Intelligence.

        Estende ``read_map_tile`` (riuso della stessa navigazione/path
        caching) aggiungendo la lettura delle bestie della Natura
        (oasi), dello stato di annessione e della protezione
        principianti.

        Ritorna::

            {"x", "y", "ok", "type": free_oasis|occupied_oasis|village|
             free_tile|unknown, "is_oasis", "occupied", "oasis_annexed",
             "nature_troops": {"u31": n, ...}, "protection": bool,
             "text"}
        """
        base = await self.read_map_tile(x, y)
        out: dict = {
            "x": int(x),
            "y": int(y),
            "ok": bool(base.get("ok")),
            "type": "unknown",
            "is_oasis": False,
            "occupied": bool(base.get("occupied")),
            "oasis_annexed": False,
            "nature_troops": {},
            "protection": False,
            "text": str(base.get("text") or ""),
        }
        if not out["ok"]:
            return out
        text = out["text"].lower()
        out["is_oasis"] = bool(re.search(r"oas[ie]", text))
        try:
            extra = await self._eval_tile_details()
        except Exception:  # noqa: BLE001
            extra = None
        if isinstance(extra, dict):
            nature = extra.get("nature") or {}
            out["nature_troops"] = {
                str(k): safe_int(v)
                for k, v in nature.items()
                if safe_int(v) > 0
            }
            out["oasis_annexed"] = bool(extra.get("annexed"))
            out["protection"] = bool(extra.get("protection"))
            if extra.get("is_oasis"):
                out["is_oasis"] = True
            if extra.get("village_link"):
                out["oasis_annexed"] = True

        # Classificazione tipo tile (spec): un'oasi annessa a un
        # villaggio giocatore va trattata come villaggio.
        if out["is_oasis"]:
            out["type"] = (
                "occupied_oasis" if out["oasis_annexed"] else "free_oasis"
            )
        elif out["occupied"]:
            out["type"] = "village"
        else:
            out["type"] = "free_tile"
        return out

    async def _eval_tile_details(self) -> dict | None:
        """Estrae bestie Natura / annessione / protezione dalla scheda tile.

        Stesso scope del pannello di ``_eval_map_tile``: le icone bestie
        sono 'unit u31..u40'; fallback testuale sui nomi localizzati
        (mappa alias da ``combat_calc.NATURE_ALIASES``).
        """
        from .combat_calc import NATURE_ALIASES  # import ritardato

        names = {
            n: u for n, u in NATURE_ALIASES.items()
            if not n.startswith("u") and not n.startswith("t")
        }
        return await self.page.evaluate(
            """
            (names) => {
                const PANEL_SEL =
                    '#map_details, .map_details, #tileDetails, .tileDetails, ' +
                    '#map_infobox, .infobox, #positionDetails, .position-details, ' +
                    '.tile-details, #map_popup, #popupMap, .popupContent, ' +
                    '.detailInfo, #tile_details, .mappanel';
                const panel = document.querySelector(PANEL_SEL);
                const scope = panel || document.body;
                const text = (scope && scope.innerText) || '';
                const num = (t) => {
                    const m = String(t || '').match(/(\\d[\\d.]*)/);
                    return m ? parseInt(m[1].replace(/\\./g, ''), 10) : 0;
                };
                const nature = {};

                // 1) Icone 'unit u31..u40' (bestie Natura): qty nella
                //    cella adiacente, come il parser truppe rally.
                scope.querySelectorAll(
                    'img[class*="unit"], i[class*="unit"], ' +
                    'span[class*="unit"], [class*=" unit u"], ' +
                    '[class*="unit u"]'
                ).forEach(icon => {
                    const cls = icon.getAttribute('class') || '';
                    const m = cls.match(
                        /(?:^|[\\s_\\-])u(3\\d|40)(?=[\\s\\-]|$)/
                    );
                    if (!m) return;
                    const key = 'u' + m[1];
                    let qty = 0;
                    const cell = icon.closest('td, a, li, .col, div');
                    if (cell) {
                        qty = num(cell.innerText || '');
                        if (!qty && cell.nextElementSibling)
                            qty = num(cell.nextElementSibling.innerText || '');
                        if (!qty && cell.parentElement)
                            qty = num(cell.parentElement.innerText || '');
                    }
                    if (qty > 0) nature[key] = (nature[key] || 0) + qty;
                });

                // 2) Fallback testuale: '19 Ragni' / '19 spiders'.
                if (!Object.keys(nature).length) {
                    const low = text.toLowerCase();
                    for (const name in names) {
                        const idx = low.indexOf(name);
                        if (idx < 0) continue;
                        // numero prima o dopo il nome (es. 'Rat: 25')
                        const before = low.slice(0, idx).match(
                            /(\\d[\\d.]*)\\s*$/
                        );
                        const after = low.slice(idx + name.length).match(
                            /^\\s*[:\\-]?\\s*(\\d[\\d.]*)/
                        );
                        const q = before ? num(before[1])
                                         : (after ? num(after[1]) : 0);
                        if (q > 0) {
                            const k = names[name];
                            nature[k] = Math.max(nature[k] || 0, q);
                        }
                    }
                }

                // Annessione oasi: link a un villaggio/proprietario nel
                // pannello o marcatori testuali multi-lingua.
                const lowAll = text.toLowerCase();
                const annexed = /(?:oasi\\s+occupat|occupied\\s+oasis|' +
                    'besetzte\\s+oase|oasis\\s+occupied|di\\s+propriet|owner|' +
                    'proprietario|trib[uù]|tribe|alleanza|alliance|' +
                    'anness)/i.test(text);
                const village_link = !!scope.querySelector(
                    'a[href*="spieler.php"], a[href*="profile"], ' +
                    'a[href*="allianz.php"], a[href*="karte.php?d="]'
                );
                const protection = /(?:beginner|protezione|' +
                    'protez\\w*\\s+principiant|anf[aä]ngerschutz|' +
                    'novice\\s+protection|beginners?\\s+protection)/i
                    .test(text);
                const is_oasis = /oas[ie]/i.test(text);
                return {
                    nature: nature,
                    annexed: annexed,
                    village_link: village_link,
                    protection: protection,
                    is_oasis: is_oasis,
                };
            }
            """,
            names,
        )

    async def parse_scout_report(self, x: int, y: int) -> dict | None:
        """Legge l'ultimo report di spionaggio verso (x|y) da berichte.php.

        Apre il report piu' recente la cui riga contiene le coordinate
        e marcatori da ricognizione (icona 'reportScout'/'iReport' o
        testo spia). Dalla pagina dettaglio estrae risorse, truppe del
        difensore, livello mura ed eventuale perdita degli esploratori.

        Ritorna::

            {"x", "y", "ts", "resources": {wood, clay, iron, crop},
             "troops": {"uN": n}, "wall_level": int,
             "scouts_lost": bool}

        o ``None`` se non esiste un report di spionaggio per il tile.
        """
        if not self.page:
            return None
        try:
            await self._goto(
                _abs_game_url(self.page.url or "", "berichte.php"),
            )
        except Exception:  # noqa: BLE001
            return None

        # 1) Riga report per le coordinate + link dettaglio.
        try:
            href = await self.page.evaluate(
                """
                (coords) => {
                    const toInt = (s) => parseInt(
                        String(s).replace(/[\\u2212\\u2010-\\u2015]/g, '-'), 10
                    );
                    const coordRe = new RegExp(
                        '\\\\(\\\\s*([+\\\\u2212\\\\u2010-\\\\u2015\\\\-]?\\\\d+)\\\\s*[|/]\\\\s*' +
                        '([+\\\\u2212\\\\u2010-\\\\u2015\\\\-]?\\\\d+)\\\\s*\\\\)'
                    );
                    const rows = document.querySelectorAll(
                        '#overview tbody tr, table.reports tbody tr, ' +
                        '#reports tbody tr, tr:has(.subject), tr:has(.sub)'
                    );
                    for (const row of rows) {
                        const txt = (row.innerText || '');
                        const m = txt.match(coordRe);
                        if (!m) continue;
                        if (toInt(m[1]) !== coords.x || toInt(m[2]) !== coords.y)
                            continue;
                        const scoutish = /(?:scout|spia|spi|esplor|ricogn|spy|' +
                            'recon|erkund|natura|nature)/i.test(txt) ||
                            !!row.querySelector(
                                'img[src*="scout"], i[class*="scout" i], ' +
                                '[class*="reportScout" i]'
                            );
                        const link = row.querySelector(
                            'a[href*="berichte.php?id="], a[href*="report?id="]'
                        );
                        if (!link) continue;
                        return {
                            href: link.href || link.getAttribute('href'),
                            scoutish: scoutish,
                        };
                    }
                    return null;
                }
                """,
                {"x": int(x), "y": int(y)},
            )
        except Exception:  # noqa: BLE001
            return None
        if not href or not href.get("href"):
            return None

        # 2) Dettaglio report: risorse / truppe / mura / esploratori persi.
        try:
            await self._goto(_abs_game_url(self.page.url or "", href["href"]))
        except Exception:  # noqa: BLE001
            return None
        try:
            data = await self.page.evaluate(
                """
                () => {
                    const num = (t) => {
                        const m = String(t || '').match(/(\\d[\\d.]*)/);
                        return m
                            ? parseInt(m[1].replace(/\\./g, ''), 10) : 0;
                    };
                    const body = (document.body && document.body.innerText) || '';
                    const out = {
                        resources: {}, troops: {},
                        wall_level: 0, scouts_lost: false,
                        is_scout: false,
                    };
                    out.is_scout = /(?:scout|spia|esplor|ricogn|spy|' +
                        'recon|erkund)/i.test(body);

                    // Sezione difensore: unita' 'uN' + quantita'.
                    const def = document.querySelector(
                        '#defender, .defender, [class*="defender" i], ' +
                        'div.role.defender'
                    ) || document.body;
                    def.querySelectorAll(
                        'img[class*="unit"], i[class*="unit"], ' +
                        'span[class*="unit"], [class*=" unit u"], ' +
                        '[class*="unit u"]'
                    ).forEach(icon => {
                        const cls = icon.getAttribute('class') || '';
                        const m = cls.match(
                            /(?:^|[\\s_\\-])u(\\d{1,2})(?=[\\s\\-]|$)/
                        );
                        if (!m) return;
                        const key = 'u' + m[1];
                        let qty = 0;
                        const cell = icon.closest('td, a, li, .col, div');
                        if (cell) {
                            qty = num(cell.innerText || '');
                            if (!qty && cell.nextElementSibling)
                                qty = num(cell.nextElementSibling.innerText || '');
                        }
                        if (qty > 0) out.troops[key] = (out.troops[key] || 0) + qty;
                    });

                    // Risorse spiate: icone r1..r4 + quantita'.
                    const rmap = {
                        'r1': 'wood', 'r2': 'clay',
                        'r3': 'iron', 'r4': 'crop',
                    };
                    document.querySelectorAll(
                        'img[class*="r1"], img[class*="r2"], ' +
                        'img[class*="r3"], img[class*="r4"], ' +
                        'i[class*="r1"], i[class*="r2"], ' +
                        'i[class*="r3"], i[class*="r4"], ' +
                        'span[class*="res"]'
                    ).forEach(icon => {
                        const cls = icon.getAttribute('class') || '';
                        const m = cls.match(/\\br([1-4])\\b/);
                        if (!m) return;
                        const key = rmap['r' + m[1]];
                        const cell = icon.closest('td, a, li, .col, div');
                        let qty = 0;
                        if (cell) {
                            qty = num(cell.innerText || '');
                            if (!qty && cell.nextElementSibling)
                                qty = num(cell.nextElementSibling.innerText || '');
                        }
                        if (qty > 0)
                            out.resources[key] = Math.max(
                                out.resources[key] || 0, qty
                            );
                    });

                    // Mura / reggia: riga con nome edificio + livello.
                    const wallNames = new RegExp(
                        '(mura|wall|mauer|palisade|palizzata|terrazzo|' +
                        'earth\\\\s*wall|city\\\\s*wall|reggia|residenza|' +
                        'residence|palace|palazzo|command\\\\s*center|' +
                        'military\\\\s*base)', 'i'
                    );
                    document.querySelectorAll('tr, li, div').forEach(el => {
                        const t = (el.innerText || '').trim();
                        if (t && t.length < 200 && wallNames.test(t)) {
                            const lv = t.match(
                                /(?:livello|level|stufe|lvl)[^\\d]*(\\d+)/i
                            ) || t.match(/\\b(\\d{1,2})\\s*$/);
                            if (lv) {
                                out.wall_level = Math.max(
                                    out.wall_level, parseInt(lv[1], 10) || 0
                                );
                            }
                        }
                    });

                    // Esploratori intercettati: sezione perdite attaccante
                    // con numero > 0 o marcatore testuale di sconfitta.
                    const att = document.querySelector(
                        '#attacker, .attacker, [class*="attacker" i], ' +
                        'div.role.attacker'
                    );
                    if (att) {
                        att.querySelectorAll(
                            '[class*="loss" i], .casualty, td:last-child'
                        ).forEach(el => {
                            if (num(el.innerText || '') > 0)
                                out.scouts_lost = true;
                        });
                    }
                    if (/(?:perdute|persi|killed|uccisi|lost|verloren|' +
                        'died|intercett)/i.test(body)) {
                        out.scouts_lost = true;
                    }
                    return out;
                }
                """
            )
        except Exception:  # noqa: BLE001
            return None
        if not isinstance(data, dict):
            return None
        res = data.get("resources") or {}
        return {
            "x": int(x),
            "y": int(y),
            "ts": time.time(),
            "is_scout": bool(data.get("is_scout")),
            "resources": {
                "wood": safe_int(res.get("wood")),
                "clay": safe_int(res.get("clay")),
                "iron": safe_int(res.get("iron")),
                "crop": safe_int(res.get("crop")),
            },
            "troops": {
                str(k): safe_int(v)
                for k, v in (data.get("troops") or {}).items()
                if safe_int(v) > 0
            },
            "wall_level": safe_int(data.get("wall_level")),
            "scouts_lost": bool(data.get("scouts_lost")),
        }

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


# ---------------------------------------------------------------------------
# DOM & Sensor Health Check (spec debug pipeline)
# ---------------------------------------------------------------------------
# Dedup: stessa combinazione di anomalie -> un solo alert per ciclo
# (altrimenti il JSONL si riempirebbe a ogni tick).
_last_health_signature: dict[tuple[str, str], tuple[str, ...]] = {}


def check_state_health(state: GameState, account_id: str = "") -> list[str]:
    """Rileva letture incoerenti del GameState -> alert SENSOR_ANOMALY.

    Chiamata dall'orchestrator dopo l'assemblaggio dello stato. Copre i
    casi tipici di regressione dei selettori/layout Travian:

    - 0 campi dorf1 o 0 edifici dorf2 rilevati;
    - ``gid == -1`` su slot dorf2 occupati (edificio esiste ma il gid
      non e' stato letto dal DOM);
    - risorse tutte a 0 e capacita' magazzini a 0 (stockbar non parsata);
    - coda costruzioni con item senza nome (parser coda degradato).

    Ritorna la lista delle anomalie correnti. L'alert (logger WARNING +
    evento trace JSONL) viene emesso solo quando la firma cambia, per
    non spammare a ogni ciclo.
    """
    anomalies: list[str] = []
    try:
        res_fields = state.resource_fields()
        interiors = state.interior_fields()
        if not res_fields:
            anomalies.append("dorf1: 0 campi risorsa rilevati")
        if not interiors:
            anomalies.append("dorf2: 0 edifici interni rilevati")
        unread = [
            f.slot_id
            for f in interiors
            if f.gid is not None and int(f.gid) < 0
            and (f.level or 0) > 0
        ]
        if unread:
            anomalies.append(
                f"dorf2: gid non letto su slot occupati {sorted(unread)}"
            )
        res = state.resources
        if (
            not any([res.wood, res.clay, res.iron, res.crop])
            and not res.warehouse_capacity
            and not res.granary_capacity
        ):
            anomalies.append("stockbar: risorse e capacita' tutte a 0")
        unnamed = sum(
            1 for q in state.build_queue if not (q.name or "").strip()
        )
        if unnamed:
            anomalies.append(
                f"coda costruzioni: {unnamed} item senza nome"
            )
    except Exception:  # noqa: BLE001
        logger.debug("check_state_health fallito", exc_info=True)
        return []

    key = (str(account_id or ""), str(state.village_name or ""))
    signature = tuple(sorted(anomalies))
    if _last_health_signature.get(key) != signature:
        prev = _last_health_signature.get(key)
        _last_health_signature[key] = signature
        if anomalies:
            try:
                from . import debug_trace

                debug_trace.sensor_anomaly(
                    account_id,
                    state.village_name,
                    anomalies,
                    context={
                        "fields": len(state.fields),
                        "queue": len(state.build_queue),
                        "prev_signature": list(prev or ()),
                    },
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "SENSOR_ANOMALY [%s][%s] %s",
                    account_id or "?",
                    state.village_name or "?",
                    "; ".join(anomalies),
                )
    return anomalies
