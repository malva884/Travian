"""Modulo Eroe autonomo: salute, unguenti, avventure e punti attributo.

Design:
- un manager per account con snapshot persistente in
  state/hero_<account>.json (letto dalla dashboard Streamlit);
- eseguito una volta per ciclo account (l'eroe e' legato all'account,
  non al singolo villaggio) con un cooldown tra un controllo e l'altro;
- flusso: leggi stato da hero.php -> cura con unguenti se HP bassi ->
  assegna eventuali punti attributo -> invia in avventura se disponibile;
- ogni fase e' best-effort: selettori multipli (ITA/EN) e log debug
  quando il DOM non matcha — mai crashare il ciclo principale.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .browser import sleep_human
from .config import BASE_DIR, Settings

if TYPE_CHECKING:
    from .browser import TravianBrowser

logger = logging.getLogger(__name__)

STATE_DIR = BASE_DIR / "state"

# Intervallo minimo tra un controllo dell'eroe e il successivo.
CHECK_MIN_SECONDS = 240.0
CHECK_MAX_SECONDS = 420.0

# Tab dell'eroe aggiornate con i nuovi percorsi di gioco
_TABS_MAIN = ("/hero/attributes", "hero.php?t=attributes", "hero.php?t=1", "hero.php")
_TABS_INVENTORY = ("/hero/inventory", "hero.php?t=inventory", "hero.php?t=2")

# AGGIUNTO IL PERCORSO PULITO COME PRIORITÀ ASSOLUTA
_TABS_ADVENTURES = ("/hero/adventures", "hero.php?t=adventures", "hero.php?t=adventure", "hero.php?t=3")

# Emergency Unpack (baule eroe): gli item risorsa sono consumabili che
# trasferiscono legno/argilla/ferro/grano nel villaggio dove si trova
# l'eroe. Limite di sicurezza sui consumi per singola emergenza.
_UNPACK_MAX_USES = 8

# Selettore union degli slot inventario (stesso ordine di querySelectorAll,
# usato sia dal sensore JS sia dal click per indice).
_INVENTORY_ITEM_SELECTOR = (
    ".inventory .item, .items .item, #items .item, .heroInventory .item, "
    ".inventoryItem, .itemSlot .item, [class*='inventory'] [class*='item']"
)

# Delta risorse leggibile sulla pagina contratto aperta (build.php /
# residenza): costo richiesto (classi r1..r4) meno stock della barra
# (#l1..#l4). Restituisce solo i deficit positivi.
_JS_PAGE_DEFICIT = """
    () => {
        const toInt = (s) => {
            const m = String(s || '')
                .replace(/[\\s.'\\u00a0\\u202f]/g, '')
                .match(/-?\\d+/);
            return m ? parseInt(m[0], 10) : 0;
        };
        const costs = { wood: 0, clay: 0, iron: 0, crop: 0 };
        const keyOf = { r1: 'wood', r2: 'clay', r3: 'iron', r4: 'crop' };
        const scopes = document.querySelectorAll(
            '.showCosts, #contract, .contractLink, table#build_value, ' +
            '.costs, #build, #content'
        );
        scopes.forEach(scope => {
            scope.querySelectorAll(
                '.r1, .r2, .r3, .r4, [class*=" r1"], [class*=" r2"], ' +
                '[class*=" r3"], [class*=" r4"], [class^="r1"], [class^="r2"], ' +
                '[class^="r3"], [class^="r4"]'
            ).forEach(el => {
                const cls = (el.getAttribute('class') || '').match(/r([1-4])/);
                if (!cls) return;
                const holder = el.parentElement || el;
                const n = toInt(holder.innerText || el.getAttribute('title') || '');
                if (n > 0) {
                    const key = keyOf['r' + cls[1]];
                    costs[key] = Math.max(costs[key], n);
                }
            });
        });
        const stock = {
            wood: toInt((document.querySelector('#l1 .value, #l1') || {}).innerText),
            clay: toInt((document.querySelector('#l2 .value, #l2') || {}).innerText),
            iron: toInt((document.querySelector('#l3 .value, #l3') || {}).innerText),
            crop: toInt((document.querySelector('#l4 .value, #l4') || {}).innerText),
        };
        const deficit = {};
        for (const r of ['wood', 'clay', 'iron', 'crop']) {
            const d = costs[r] - stock[r];
            if (costs[r] > 0 && d > 0) deficit[r] = d;
        }
        return { deficit: deficit, costs: costs, stock: stock };
    }
"""

# Sensore inventario: per ogni slot item raccoglie testo/tooltip/alt e
# rileva i consumabili di risorsa (pacchi/sacchi). 'res' = risorsa singola
# oppure 'any' per pacchi che concedono tutte e quattro le risorse.
_JS_INVENTORY_RESOURCES = (
    """
    () => {
        const out = [];
        const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
        const parseAmount = (txt) => {
            const m = (txt || '').match(/(\\d[\\d.\\s']{1,9})/);
            if (!m) return 0;
            return parseInt(m[1].replace(/[^0-9]/g, ''), 10) || 0;
        };
        const resOf = (txt) => {
            const t = ' ' + (txt || '').toLowerCase() + ' ';
            const kw = {
                wood: ['legno', 'wood', 'lumber', 'holz', 'bois', 'madera'],
                clay: ['argilla', 'clay', 'lehm', 'argile', 'barro', 'arcilla'],
                iron: ['ferro', 'iron', 'eisen', 'hierro'],
                crop: ['grano', 'crop', 'wheat', 'grain', 'cereal', 'getreide',
                       'frumento', 'trigo'],
            };
            for (const res of ['wood', 'clay', 'iron', 'crop']) {
                if (kw[res].some(w => t.includes(w))) return res;
            }
            return /risorse|resources|ressourcen|recursos/.test(t) ? 'any' : '';
        };
        const packLike = (txt) =>
            /pacco|sacco|pack|bag|sack|crate|cassa|chest|baule|paquet|paquete|kiste/i
                .test(txt || '');
        const items = document.querySelectorAll(
            '"""
    + _INVENTORY_ITEM_SELECTOR
    + """'
        );
        let idx = 0;
        items.forEach(item => {
            const img = item.querySelector('img');
            const blob = [
                item.getAttribute('title') || '',
                item.getAttribute('data-title') || '',
                item.getAttribute('data-tooltip') || '',
                item.getAttribute('data-item-name') || '',
                item.getAttribute('aria-label') || '',
                img ? ((img.getAttribute('alt') || '') + ' ' +
                       (img.getAttribute('title') || '')) : '',
                item.innerText || '',
                ...Array.from(item.querySelectorAll(
                    '[class*="tooltip" i], .description, .name'
                )).map(e => e.innerText || ''),
            ].join(' | ');
            const res = resOf(blob);
            const amount = parseAmount(blob);
            let count = 1;
            const cEl = item.querySelector(
                '.count, .amount, .number, [class*="count" i]'
            );
            if (cEl) {
                const n = parseAmount(cEl.innerText);
                if (n > 0) count = n;
            }
            if (res && amount > 0 && packLike(blob)) {
                out.push({
                    idx: idx, res: res, amount: amount, count: count,
                    label: norm(blob).slice(0, 120),
                });
            }
            idx++;
        });
        return out;
    }
"""
)

# Focus attributi: etichette ITA/EN usate per trovare la riga e il suo "+".
_ATTRIBUTE_LABELS: dict[str, tuple[str, ...]] = {
    "strength": ("forza", "strength", "power", "macht", "force"),
    "off": ("bonus d'attacco", "bonus di attacco", "off bonus", "offence", "offense", "attacco"),
    "def": ("bonus di difesa", "def bonus", "defence", "defense", "difesa"),
    "resources": ("risorse", "resources", "produzione", "production", "ressourcen"),
}

_JS_HERO_STATE = """
    () => {
        const parseNum = (str) => {
            const m = (str || '').match(/\\d+/);
            return m ? parseInt(m[0], 10) : 0;
        };
        const text = (document.body.innerText || '').toLowerCase();

        // --- Salute: barra nel box eroe o nella pagina ---
        let health = 0;
        const healthEl = document.querySelector(
            '#sidebarBoxHero .healthBar, .heroHealthBar, #heroHealth, .health, .bar.health'
        );
        if (healthEl) {
            const title = healthEl.getAttribute('title') || healthEl.innerText || '';
            const frac = title.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
            const pct = title.match(/(\\d+)\\s*%/);
            if (frac) health = Math.round(100 * parseInt(frac[1], 10) / Math.max(1, parseInt(frac[2], 10)));
            else if (pct) health = parseInt(pct[1], 10);
            else health = parseNum(title);
        }
        if (!health) {
            const fill = document.querySelector(
                '#sidebarBoxHero .healthBar .bar, #sidebarBoxHero .bar .fill, .healthBar .fill'
            );
            if (fill) {
                const w = (fill.style.width || '').match(/(\\d+(?:\\.\\d+)?)/);
                if (w) health = Math.round(parseFloat(w[1]));
            }
        }
        if (!health) {
            const m = text.match(/(\\d+)\\s*\\/\\s*(\\d+)\\s*(?:hp|salute|health)/) ||
                      text.match(/(?:salute|health|hp)[^\\d]*(\\d+)\\s*%/);
            if (m) health = parseInt(m[1], 10);
            else health = 100;
        }

        // --- Stato: a casa / in marcia / in avventura / morto ---
        let status = 'home';
        const statusEl = document.querySelector('#sidebarBoxHero .status, .heroStatus, .statusMessage');
        const stText = ((statusEl ? statusEl.innerText : '') || text).toLowerCase();
        if (/morto|dead|regener|rinasc/.test(stText)) status = 'dead';
        else if (/avventur|adventure/.test(stText)) status = 'adventure';
        else if (/marcia|travell|viagg|underway|in viaggio|stationar|stazion/.test(stText)) status = 'moving';
        else if (/fuori villaggio|other village|altro villaggio/.test(stText)) status = 'away';

        // --- Avventure disponibili: badge nel box eroe / top bar ---
        let adventures = 0;
        const advElements = document.querySelectorAll(
            'a[href*="adventure"], a[href*="t=3"], .adventure, #sidebarBoxHero .adventure, .favorKey'
        );
        for (const el of advElements) {
            const inner = el.innerText || '';
            const titleAttr = el.getAttribute('title') || '';
            const combined = (inner + ' ' + titleAttr).toLowerCase();
            const num = parseNum(inner);
            if (num > 0 && (combined.includes('avventur') || combined.includes('adventure') || el.href.includes('adventure') || el.href.includes('t=3'))) {
                adventures = num;
                break;
            }
        }
        if (adventures === 0) {
            const advBoxLink = document.querySelector('a[href*="adventure"], a[href*="t=3"]');
            if (advBoxLink && advBoxLink.offsetParent !== null) {
                const textToCheck = (advBoxLink.innerText || advBoxLink.getAttribute('title') || '').toLowerCase();
                if (textToCheck.includes('avventur') || textToCheck.includes('adventure') || advBoxLink.querySelector('.count, .badge, span')) {
                    const badgeNum = parseNum(advBoxLink.querySelector('.count, .badge, span')?.innerText);
                    adventures = badgeNum > 0 ? badgeNum : 1;
                }
            }
        }
        if (adventures === 0) {
            // Sulla tab avventure il conteggio affidabile sono le righe
            // della lista con un bottone/link d'avvio
            const rows = document.querySelectorAll(
                '#adventures tr, .adventureList tr, table.adventures tr, ' +
                '.adventureList .adventure, tr.adventureRow, ' +
                'table tr:has(button), table tr:has(a[href*="adventure"])'
            );
            rows.forEach(r => {
                if (r.querySelector('button, a.button, a[href*="adventure"], a[href*="start"]')) {
                    adventures++;
                }
            });
        }

        // --- Punti attributo da distribuire ---
        let freePoints = 0;
        const ptsEl = document.querySelector(
            '.heroAttributes .points, .attrPoints, [class*="freepoint" i], [class*="skillpoint" i]'
        );
        if (ptsEl) freePoints = parseNum(ptsEl.innerText);
        if (!freePoints) {
            const m = text.match(/(\\d+)\\s*(?:punti|points)\\s*(?:da distribuire|remaining|available|liberi)/);
            if (m) freePoints = parseInt(m[1], 10);
        }

        // --- Livello eroe ---
        let level = 0;
        const lvlEl = document.querySelector('.heroLevel, #sidebarBoxHero .level, .level');
        if (lvlEl) level = parseNum(lvlEl.innerText);
        if (!level) {
            // Fallback: 'Livello 15' / 'Level 15' nel testo della pagina
            const m = text.match(/(?:livello|level|lvl\\.?)\\s*:?\\s*(\\d+)/);
            if (m) level = parseInt(m[1], 10);
        }

        return {
            health: Math.min(100, Math.max(0, health)),
            status: status,
            adventures: adventures,
            free_points: freePoints,
            level: level,
        };
    }
"""


@dataclass
class HeroManager:
    """Gestione autonoma dell'eroe per un account."""

    account_id: str
    state_path: Path | None = None
    next_check: float = 0.0
    snapshot: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.state_path is None:
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.account_id)
            self.state_path = STATE_DIR / f"hero_{safe}.json"

    # ------------------------------------------------------------------
    # Persistenza snapshot (letto dalla dashboard)
    # ------------------------------------------------------------------
    def _save(self) -> None:
        try:
            assert self.state_path is not None
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(self.snapshot, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Salvataggio snapshot eroe fallito: %s", exc)

    async def _dump_html(self, page, settings: Settings) -> None:
        """Salva l'HTML di hero.php in debug/<account>/hero_state.html."""
        try:
            assert settings.debug_dir is not None
            settings.debug_dir.mkdir(parents=True, exist_ok=True)
            (settings.debug_dir / "hero_state.html").write_text(
                await page.content(), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Dump HTML eroe fallito: %s", exc)

    # ------------------------------------------------------------------
    # Entry point: chiamato una volta per ciclo account
    # ------------------------------------------------------------------
    async def run(
        self,
        browser: "TravianBrowser",
        settings: Settings,
        prefix: str = "",
    ) -> bool:
        """Esegue il check dell'eroe rispettando il cooldown."""
        now = time.time()
        if now < self.next_check:
            return True
        self.next_check = now + random.uniform(CHECK_MIN_SECONDS, CHECK_MAX_SECONDS)

        page = browser.page
        if page is None:
            return False

        cfg = settings.hero
        try:
            # 1. Pagina principale dell'eroe (attributi/stato)
            await self._goto_hero_tab(browser, _TABS_MAIN)
            await sleep_human(1.0, 1.8)

            state = await self._sense(page)
            logger.info(
                "%s Eroe: salute %s%%, stato=%s, avventure=%s, punti=%s, livello=%s",
                prefix, state["health"], state["status"], state["adventures"],
                state["free_points"], state["level"],
            )
            if state["level"] == 0:
                # Livello 0 e' impossibile in gioco: i selettori non hanno
                # trovato il DOM dell'eroe -> i valori potrebbero essere i
                # fallback (salute 100, avventure 0). Non fidarci: dump HTML.
                logger.warning(
                    "%s Lettura stato eroe non confermata (livello=0 impossibile): "
                    "i selettori potrebbero non matchare questo server. "
                    "Dump HTML in debug/%s/hero_state.html",
                    prefix, settings.account_id,
                )
                await self._dump_html(page, settings)
            self.snapshot = {
                **state,
                "is_home": state["status"] == "home",
                "last_action": "",
                "updated_at": now,
            }

            # 2. Auto-cura con unguenti se HP sotto soglia (eroe fermo)
            if state["health"] < cfg.heal_threshold and state["status"] in (
                "home", "away", "dead",
            ):
                healed = await self._apply_healing(browser, prefix)
                self.snapshot["last_action"] = (
                    "unguento applicato" if healed else "cura fallita (nessun unguento?)"
                )
                if healed:
                    await sleep_human(1.0, 1.5)
                    state = await self._sense(page)
                    self.snapshot.update(state)

            # 3. Assegnazione punti attributo disponibili
            if state["free_points"] > 0:
                assigned = await self._assign_attribute_points(
                    browser, settings, state["free_points"], prefix
                )
                if assigned:
                    self.snapshot["last_action"] = (
                        f"{self.snapshot['last_action']} + punti attributo assegnati"
                    ).strip(" +")

            # 4. Spedizione in avventura: navigazione esplicita sulla tab
            #    avventure — il badge sulla pagina attributi puo' mancare,
            #    il conteggio affidabile sono le righe della lista stessa.
            if cfg.auto_adventures and state["status"] == "home":
                await self._goto_hero_tab(browser, _TABS_ADVENTURES)
                await sleep_human(1.0, 1.5)
                state_adv = await self._sense(page)
                if state_adv["adventures"] > state["adventures"]:
                    state["adventures"] = state_adv["adventures"]
                    self.snapshot["adventures"] = state["adventures"]
                    logger.info(
                        "%s Avventure rilevate sulla tab: %s",
                        prefix, state["adventures"],
                    )

            can_send = (
                cfg.auto_adventures
                and state["adventures"] > 0
                and state["status"] == "home"
                and state["health"] >= cfg.min_health_to_adventure
            )
            if can_send:
                sent = await self._send_to_adventure(browser, prefix)
                self.snapshot["last_action"] = (
                    "spedito in avventura" if sent else "spedizione avventura fallita"
                )
            elif cfg.auto_adventures and state["adventures"] > 0:
                logger.debug(
                    "%s Avventure disponibili ma eroe non idoneo (stato=%s, hp=%s)",
                    prefix, state["status"], state["health"],
                )

            # 5. Inventario risorse (spec 5): legge i pacchi/sacchi
            #    accumulati da avventure/aste e ne salva i totali nello
            #    snapshot — il planner li considera riserve immuni al
            #    saccheggio (Emergency Unpack li consuma all'occorrenza).
            inv = await self._sense_inventory_resources(browser, prefix)
            if inv is not None:
                self.snapshot["resource_items"] = inv["items"]
                self.snapshot["resource_totals"] = inv["totals"]
                if inv["items"]:
                    logger.info(
                        "%s Eroe: %d pacchi risorsa in inventario (%s)",
                        prefix, inv["count"], inv["totals"],
                    )

            self._save()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s Errore modulo eroe: %s", prefix, exc)
            self.snapshot.setdefault("last_action", f"errore: {exc}")
            self.snapshot["updated_at"] = now
            self._save()
            return False

    # ------------------------------------------------------------------
    # Sensori
    # ------------------------------------------------------------------
    async def _sense(self, page) -> dict[str, Any]:
        """Legge salute/stato/avventure/punti dell'eroe dal DOM."""
        try:
            state = await page.evaluate(_JS_HERO_STATE)
            if isinstance(state, dict):
                state.setdefault("health", 100)
                state.setdefault("status", "home")
                state.setdefault("adventures", 0)
                state.setdefault("free_points", 0)
                state.setdefault("level", 0)
                return state
        except Exception as exc:  # noqa: BLE001
            logger.debug("Lettura stato eroe fallita: %s", exc)
        return {
            "health": 100, "status": "home", "adventures": 0,
            "free_points": 0, "level": 0,
        }

    async def _sense_inventory_resources(
        self, browser: "TravianBrowser", prefix: str = ""
    ) -> dict[str, Any] | None:
        """Legge i consumabili-risorsa nel baule dell'eroe (tab inventario).

        Ritorna {"items": [...], "totals": {wood,clay,iron,crop}, "count": n}
        o None se la pagina inventario non e' raggiungibile/leggibile.
        """
        page = browser.page
        if page is None:
            return None
        try:
            await self._goto_hero_tab(browser, _TABS_INVENTORY)
            await sleep_human(0.8, 1.4)
            items = await page.evaluate(_JS_INVENTORY_RESOURCES)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "%s Lettura inventario eroe fallita: %s", prefix, exc,
            )
            return None
        if not isinstance(items, list):
            return None
        totals: dict[str, int] = {
            "wood": 0, "clay": 0, "iron": 0, "crop": 0,
        }
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                amount = int(it.get("amount") or 0) * max(
                    1, int(it.get("count") or 1)
                )
            except (TypeError, ValueError):
                continue
            res = str(it.get("res") or "")
            if res == "any":
                for k in totals:
                    totals[k] += amount
            elif res in totals:
                totals[res] += amount
        return {
            "items": items,
            "totals": totals,
            "count": len(items),
        }

    async def _goto_hero_tab(self, browser: "TravianBrowser", candidates: tuple[str, ...]) -> None:
        """Prova i path candidati del tab (nomi o numeri a seconda del server)."""
        for path in candidates:
            try:
                await browser.goto(path)
                return
            except Exception:  # noqa: BLE001
                continue

    # ------------------------------------------------------------------
    # Auto-cura: inventario -> unguento -> usa
    # ------------------------------------------------------------------
    async def _apply_healing(self, browser: "TravianBrowser", prefix: str) -> bool:
        """Apre l'inventario e usa un unguento (o pozione minore)."""
        page = browser.page
        assert page is not None
        try:
            await self._goto_hero_tab(browser, _TABS_INVENTORY)
            await sleep_human(1.0, 1.8)

            # Item curativo: classe/icona o tooltip che menziona unguento/pozione
            item = page.locator(
                ".inventory .item[class*='ointment' i], .items .item[class*='ointment' i], "
                "[class*='item'][title*='Unguent' i], [class*='item'][title*='Ointment' i], "
                "img[alt*='Unguent' i], img[alt*='Ointment' i], "
                ".item[class*='heal' i], [title*='Pozione' i], [title*='Potion' i]"
            ).first
            if await item.count() == 0:
                logger.debug("%s Nessun unguento/pozione in inventario", prefix)
                return False
            await item.click()
            await sleep_human(0.8, 1.4)

            # Dialog d'uso: bottone 'Usa' / quantita' -> conferma
            used = await page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll(
                        'button, a.button, input[type="submit"], input[type="button"]'
                    ));
                    const use = btns.find(b =>
                        /usa|use|apply|applica|consume/i.test(
                            (b.innerText || b.value || '')
                        ) && !b.disabled && b.offsetParent !== null
                    );
                    if (use) { use.click(); return true; }
                    // alcuni server: click diretto sull'item lo usa gia'
                    return false;
                }
            """)
            await sleep_human(0.8, 1.4)
            logger.info("%s Unguento eroe applicato (conferma=%s)", prefix, used)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s Uso unguento fallito: %s", prefix, exc)
            return False

    # ------------------------------------------------------------------
    # Punti attributo
    # ------------------------------------------------------------------
    def _attribute_focus(self, settings: Settings) -> str:
        """Sceglie l'attributo target: config esplicita o profilo account."""
        focus = (settings.hero.attribute_focus or "auto").strip().lower()
        if focus in _ATTRIBUTE_LABELS:
            return focus
        # auto: hammer -> forza, altrimenti risorse (fase economica)
        profile = (settings.strategy_profile or "").lower()
        if "hammer" in profile or "aggressive" in profile:
            return "strength"
        return "resources"

    async def _assign_attribute_points(
        self,
        browser: "TravianBrowser",
        settings: Settings,
        points: int,
        prefix: str,
    ) -> bool:
        """Assegna i punti liberi all'attributo scelto (fino a esaurimento)."""
        page = browser.page
        assert page is not None
        focus = self._attribute_focus(settings)
        labels = "|".join(_ATTRIBUTE_LABELS[focus])
        try:
            assigned = 0
            for _ in range(min(points, 20)):
                clicked = await page.evaluate(
                    """
                    (labels) => {
                        const rows = Array.from(document.querySelectorAll(
                            '.heroAttributes tr, .heroAttributes .attribute, ' +
                            '#attributes tr, .attributes .attr, .skillpoints .row'
                        ));
                        const re = new RegExp(labels, 'i');
                        for (const row of rows) {
                            if (!re.test(row.innerText || '')) continue;
                            const btn = row.querySelector(
                                'a[href*="addPoint"], button[class*="add" i], ' +
                                'a[class*="add" i], .plus, a:has-text("+"), button:has-text("+")'
                            );
                            if (btn && !btn.disabled && btn.offsetParent !== null) {
                                btn.click();
                                return true;
                            }
                        }
                        // Fallback globale: primo '+' cliccabile vicino alla label
                        const all = Array.from(document.querySelectorAll('a, button'));
                        const plus = all.find(b =>
                            (b.innerText || '').trim() === '+' &&
                            !b.disabled && b.offsetParent !== null
                        );
                        if (plus) { plus.click(); return true; }
                        return false;
                    }
                    """,
                    labels,
                )
                if not clicked:
                    break
                assigned += 1
                await sleep_human(0.5, 1.0)

            if assigned:
                logger.info(
                    "%s Eroe: assegnati %d punti attributo a '%s'",
                    prefix, assigned, focus,
                )
            else:
                logger.debug(
                    "%s Punti attributo presenti ma bottone '+' non trovato (focus=%s)",
                    prefix, focus,
                )
            return assigned > 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Assegnazione punti attributo fallita: %s", prefix, exc)
            return False

    # ------------------------------------------------------------------
    # Spedizione avventura
    # ------------------------------------------------------------------
    async def _send_to_adventure(self, browser: "TravianBrowser", prefix: str) -> bool:
        """Tab avventure -> prima avventura disponibile -> conferma invio."""
        page = browser.page
        assert page is not None
        try:
            # 1. Naviga esplicitamente alla pagina pulita delle avventure
            await self._goto_hero_tab(browser, _TABS_ADVENTURES)
            await sleep_human(1.5, 2.2)

            # 2. Cerca e clicca sul pulsante 'Esplora' / 'Inizia' nella tabella delle avventure
            clicked = await page.evaluate("""
                                          () => {
                                              // Cerca tutti i pulsanti o link all'interno della tabella delle avventure
                                              const elements = Array.from(document.querySelectorAll(
                                                  'button, a.button, a.textButtonV1, input[type="submit"], input[type="button"], .adventureList button, tr.adventureRow button'
                                              ));

                                              // Aggiunte le parole chiave ufficiali in italiano e inglese ("esplora", "explore")
                                              const btnRe = /esplora|explore|inizia|start|vai|go|avvia|adventure/i;
                                              for (const el of elements) {
                                                  const text = (el.innerText || el.value || '').trim();
                                                  const href = el.getAttribute('href') || '';

                                                  if (!el.disabled && el.offsetParent !== null) {
                                                      if (btnRe.test(text) || btnRe.test(href) || el.classList.contains('textButtonV1')) {
                                                          el.click();
                                                          return true;
                                                      }
                                                  }
                                              }

                                              // Fallback ulteriore: cerca qualsiasi elemento verde o bottone nella tabella
                                              const greenBtn = document.querySelector('button.green, .green button, button:has-text("Esplora")');
                                              if (greenBtn && !greenBtn.disabled && greenBtn.offsetParent !== null) {
                                                  greenBtn.click();
                                                  return true;
                                              }

                                              return false;
                                          }
                                          """)

            if not clicked:
                logger.debug("%s Nessun pulsante 'Esplora' trovato nella lista avventure", prefix)
                return False

            await sleep_human(1.2, 2.0)

            # 3. Eventuale schermata di conferma pop-up (se presente)
            await page.evaluate("""
                                () => {
                                    const confirmBtns = Array.from(document.querySelectorAll(
                                        'button, a.button, input[type="submit"], input[type="button"]'
                                    ));
                                    const b = confirmBtns.find(x =>
                                        /conferm|confirm|esplora|explore|inizia|start|vai|ok/i.test(x.innerText || x.value || '') &&
                                        !x.disabled && x.offsetParent !== null
                                    );
                                    if (b) {
                                        b.click();
                                        return true;
                                    }
                                    return false;
                                }
                                """)
            await sleep_human(1.0, 1.8)
            logger.info("%s Eroe spedito in avventura con successo!", prefix)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s Spedizione avventura fallita: %s", prefix, exc)
            return False

    # ------------------------------------------------------------------
    # Emergency Unpack: risorse dal baule dell'eroe (spec 3.2)
    # ------------------------------------------------------------------
    async def emergency_unpack(
        self,
        browser: "TravianBrowser",
        *,
        village_id: int = 0,
        prefix: str = "",
    ) -> dict[str, int]:
        """Consuma pacchi risorsa dal baule dell'eroe per coprire il deficit.

        Chiamato a valle di un'azione di spesa fallita per "risorse
        insufficienti": la pagina contratto e' ancora aperta, quindi il
        delta (costo - stock) si legge direttamente dal DOM
        (``_JS_PAGE_DEFICIT``). Poi apre l'inventario eroe e consuma il
        MINIMO di pacchi che copre il deficit, risorsa per risorsa
        (i pacchi 'any' coprono tutte e quattro le risorse insieme).

        Le risorse unpack finiscono nel villaggio dove si trova l'eroe:
        se lo snapshot segnala l'eroe lontano/morto/in avventura il
        consumo viene saltato per non accreditare il villaggio sbagliato.

        Ritorna {wood, clay, iron, crop} delle quantita' accreditate
        ({ } se nessun consumo o nessun deficit leggibile).
        """
        page = browser.page
        if page is None:
            return {}
        status = str((self.snapshot or {}).get("status") or "")
        if status and status != "home":
            logger.debug(
                "%s Emergency Unpack saltato: eroe '%s', i pacchi "
                "accrediterebbero il villaggio sbagliato.",
                prefix, status,
            )
            return {}

        # 1. Delta risorse dal contratto aperto (build.php o residenza):
        #    niente costo leggibile = niente unpack (evita sprechi).
        try:
            data = await page.evaluate(_JS_PAGE_DEFICIT)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Lettura deficit contratto fallita: %s", prefix, exc)
            return {}
        deficit = {
            str(k): int(v)
            for k, v in ((data or {}).get("deficit") or {}).items()
            if int(v or 0) > 0
        }
        if not deficit:
            logger.debug(
                "%s Emergency Unpack: nessun deficit leggibile sulla pagina "
                "(costi %s, stock %s).",
                prefix, (data or {}).get("costs"), (data or {}).get("stock"),
            )
            return {}
        logger.info(
            "%s Emergency Unpack: deficit %s — apro il baule dell'eroe.",
            prefix, deficit,
        )

        # 2. Inventario eroe -> consumo minimo dei pacchi risorsa
        try:
            await self._goto_hero_tab(browser, _TABS_INVENTORY)
            await sleep_human(1.0, 1.8)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s Apertura inventario eroe fallita: %s", prefix, exc)
            return {}

        gained: dict[str, int] = {"wood": 0, "clay": 0, "iron": 0, "crop": 0}
        uses = 0
        while uses < _UNPACK_MAX_USES and any(v > 0 for v in deficit.values()):
            try:
                items = await page.evaluate(_JS_INVENTORY_RESOURCES)
            except Exception as exc:  # noqa: BLE001
                logger.debug("%s Scansione inventario fallita: %s", prefix, exc)
                break
            items = [i for i in (items or []) if isinstance(i, dict)]
            pick = _pick_resource_pack(items, deficit)
            if pick is None:
                logger.debug(
                    "%s Nessun pacco utile in baule per il deficit %s "
                    "(item rilevati: %s).",
                    prefix, deficit,
                    [f"{i.get('res')}x{i.get('amount')}" for i in items],
                )
                break
            used = await self._use_inventory_item(page, int(pick["idx"]))
            if not used:
                break
            uses += 1
            amount = int(pick.get("amount") or 0)
            if pick.get("res") == "any":
                for res in ("wood", "clay", "iron", "crop"):
                    covered = min(amount, deficit.get(res, 0))
                    gained[res] += covered
                    deficit[res] = max(0, deficit.get(res, 0) - amount)
            else:
                res = str(pick.get("res"))
                covered = min(amount, deficit.get(res, 0))
                gained[res] = gained.get(res, 0) + covered
                deficit[res] = max(0, deficit.get(res, 0) - amount)
            logger.info(
                "%s Pacco consumato (%s, %s) — deficit residuo %s.",
                prefix, pick.get("label") or pick.get("res"), amount, deficit,
            )
            await sleep_human(1.0, 1.6)

        gained = {k: v for k, v in gained.items() if v > 0}
        if gained:
            logger.info(
                "%s Emergency Unpack: accreditate %s nel villaggio "
                "(newdid=%s) da %d pacchi.",
                prefix, gained, village_id or "?", uses,
            )
        return gained

    async def _use_inventory_item(self, page, idx: int) -> bool:
        """Clicca l'item inventario in posizione `idx` e conferma 'Usa'."""
        try:
            item = page.locator(_INVENTORY_ITEM_SELECTOR).nth(idx)
            if await item.count() == 0:
                return False
            await item.click()
            await sleep_human(0.8, 1.4)
            confirmed = await page.evaluate("""
                () => {
                    const btns = Array.from(document.querySelectorAll(
                        'button, a.button, input[type="submit"], input[type="button"]'
                    ));
                    const use = btns.find(b =>
                        /usa|use|apply|applica|consume|consuma/i.test(
                            b.innerText || b.value || ''
                        ) && !b.disabled && b.offsetParent !== null
                    );
                    if (use) { use.click(); return true; }
                    // Alcuni server usano il pacco al primo click sull'item.
                    return false;
                }
            """)
            await sleep_human(0.8, 1.4)
            logger.debug(
                "Item inventario idx=%d consumato (conferma dialog=%s)",
                idx, confirmed,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("Uso item inventario idx=%d fallito: %s", idx, exc)
            return False


def _pick_resource_pack(
    items: list[dict], deficit: dict[str, int]
) -> dict | None:
    """Sceglie il pacco inventario che copre piu' deficit residuo.

    Per i pacchi 'any' il gain e' la somma sui quattro tipi; a parita'
    di copertura vince il pacco piu' piccolo (meno spreco). Ritorna il
    dict item scelto o None se nessun pacco copre qualcosa.
    """
    best: dict | None = None
    best_gain = 0
    for it in items:
        amount = int(it.get("amount") or 0)
        if amount <= 0 or int(it.get("count") or 0) <= 0:
            continue
        res = str(it.get("res") or "")
        if res == "any":
            gain = sum(min(amount, int(deficit.get(r, 0))) for r in deficit)
        elif res in deficit:
            gain = min(amount, int(deficit.get(res, 0)))
        else:
            gain = 0
        if gain > best_gain or (
            gain == best_gain > 0 and amount < int((best or {}).get("amount") or 0)
        ):
            best, best_gain = it, gain
    return best if best_gain > 0 else None
