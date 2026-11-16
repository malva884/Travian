"""Driver del browser: Playwright, login ibrido, sessione persistente.

Strategia anti-bot consigliata:
1. Primo avvio: `main.py --login` apre una finestra visibile, l'utente
   fa il login a mano (captcha inclusi) e la sessione viene salvata.
2. Avvii successivi: i cookie vengono ricaricati automaticamente e il
   login viene saltato. Se la sessione scade, si tenta il login via
   form; se fallisce in modalita' visibile si ricade sul login manuale.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from pathlib import Path

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

import re
from urllib.parse import urlparse

from . import selectors
from .config import Settings

logger = logging.getLogger(__name__)


class ConcurrentSessionError(RuntimeError):
    """Sessione invalidata a meta' run: probabile login manuale altrove.

    Sollevata da ``ensure_logged_in`` quando una sessione che risultava
    attiva in precedenza scompare senza un logout esplicito del bot.
    In Travian un secondo login invalida la prima sessione: ritentare
    subito il login significherebbe contendere l'account all'utente
    (e due sessioni che si rimpallano sono un pattern da bot).
    """

# Nota: la patch asyncio per WinError 10054 (_ProactorBasePipeTransport)
# vive in travian_agent/__init__.py: si applica a ogni import del package.


def human_delay(min_seconds: float = 0.8, max_seconds: float = 2.5) -> float:
    """Restituisce un delay casuale 'umano' (distribuzione quasi gaussiana)."""
    return random.uniform(min_seconds, max_seconds) * random.choice(
        [1.0, 1.0, 1.0, 1.15]
    )


async def sleep_human(min_seconds: float = 0.8, max_seconds: float = 2.5) -> None:
    await asyncio.sleep(human_delay(min_seconds, max_seconds))


# --- Navigazione 'umana' via click DOM -----------------------------------

_RE_NEWDID = re.compile(r"newdid=(\d+)")
_RE_BUILD_ID = re.compile(r"build\.php\?[^#\s]*?\bid=(\d+)")


async def _click_first(page: Page, selectors_list: list[str]) -> bool:
    """Click sul primo elemento visibile fra le alternative di selettore."""
    for sel in selectors_list:
        locator = page.locator(sel).first
        try:
            if await locator.count() == 0 or not await locator.is_visible():
                continue
            await locator.click(timeout=5000)
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            return True
        except Exception:
            continue
    return False


async def navigate_via_dom(page: Page, path: str) -> bool:
    """Tenta la navigazione via click DOM; True se riuscita.

    Usabile da qualunque componente con una ``Page`` (browser, sensors):
    copre i casi d'uso frequenti e torna False in modo che il chiamante
    possa ripiegare su ``page.goto``.

    - ``?newdid=N``: click sulla voce villaggio nella sidebar
    - ``build.php?id=N``: prima assicura il dorf giusto (campi 1-18 su
      dorf1, interni 19-40 su dorf2) poi click sullo slot della mappa
    - ``dorf1.php``/``dorf2.php``/``karte.php``: click sull'icona navbar

    I selettori matchano ``id=N&``/``id=N$`` per non confondere lo slot 3
    con lo slot 30 (stessa cosa per newdid).
    """
    # URL assoluti -> path relativo (regex e confronti lavorano sul path)
    _parsed = urlparse(path)
    if _parsed.netloc:
        path = _parsed.path.lstrip("/") + (
            f"?{_parsed.query}" if _parsed.query else ""
        )

    # 1. Cambio villaggio: click sulla sidebar invece di ?newdid=
    m = _RE_NEWDID.search(path)
    if m:
        did = m.group(1)
        cur = _RE_NEWDID.search(page.url or "")
        if not cur or cur.group(1) != did:
            if not await _click_first(
                page,
                [
                    f'a[href*="newdid={did}&"]',
                    f'a[href$="newdid={did}"]',
                    f'a[href*="newdid={did}"]',
                ],
            ):
                return False
            await sleep_human(0.4, 1.0)
        base = path.split("?", 1)[0]
        # Il link sidebar punta a dorf1: se il target e' solo dorf1 basta.
        if base == selectors.PATH_DORF1 or path == "":
            return True

    # 2a. Nuova costruzione su slot vuoto: dorf2.php?a=GID&id=SLOT
    m_new = re.search(r"dorf2\.php\?[^#\s]*?\ba=(\d+)[^#\s]*?\bid=(\d+)", path)
    if m_new:
        gid, slot = int(m_new.group(1)), int(m_new.group(2))
        if selectors.PATH_DORF2 not in (page.url or ""):
            if not await _click_first(page, [f'a[href*="{selectors.PATH_DORF2}"]']):
                return False
            await sleep_human(0.5, 1.2)
        if not await _click_first(
            page,
            [
                f'area[href$="id={slot}"]',
                f'area[href*="id={slot}&"]',
                f'.buildingSlot.a{slot} a',
                f'.buildingSlot.a{slot}',
            ],
        ):
            return False
        await sleep_human(0.5, 1.2)
        # Catalogo costruzioni: click sulla scheda dell'edificio scelto
        return await _click_first(
            page,
            [
                f'a[href*="a={gid}&"]',
                f'a[href*="?a={gid}&"]',
                f'a[href$="a={gid}"]',
            ],
        )

    # 2. build.php?id=N: click sullo slot della mappa villaggio
    b = _RE_BUILD_ID.search(path)
    if b:
        slot = int(b.group(1))
        cur_url = page.url or ""
        if "build.php" in cur_url and f"id={slot}" in cur_url:
            pass  # gia' sullo slot: gestisce solo i sub-param sotto
        else:
            dorf = selectors.PATH_DORF1 if slot <= 18 else selectors.PATH_DORF2
            if dorf not in cur_url:
                if not await _click_first(page, [f'a[href*="{dorf}"]']):
                    return False
                await sleep_human(0.5, 1.2)
            slot_ok = False
            for sel in (
                f'a[href*="build.php?id={slot}&"]',
                f'a[href$="build.php?id={slot}"]',
                f'area[href*="build.php?id={slot}&"]',
                f'area[href$="build.php?id={slot}"]',
                f'a[href*="id={slot}&"][href*="build.php"]',
                f'a[href$="id={slot}"][href*="build.php"]',
                f'.buildingSlot.a{slot} a',
                f'.buildingSlot.a{slot}',
                f'[data-aid="{slot}"] a',
                f'[data-aid="{slot}"]',
            ):
                try:
                    loc = page.locator(sel).first
                    if await loc.count() == 0 or not await loc.is_visible():
                        continue
                    await loc.click(timeout=5000)
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=15000
                    )
                    slot_ok = True
                    break
                except Exception:
                    continue
            if not slot_ok:
                return False
        # Sub-tab (es. rally point tt=99) e nuova costruzione (a=GID):
        # la pagina e' gia' sullo slot, resta da cliccare la tab/scheda.
        tt = re.search(r"[?&]tt=(\d+)", path)
        if tt:
            await sleep_human(0.4, 1.0)
            return await _click_first(
                page, [f'a[href*="tt={tt.group(1)}"]']
            )
        a_gid = re.search(r"[?&]a=(\d+)", path)
        if a_gid:
            await sleep_human(0.4, 1.0)
            return await _click_first(
                page,
                [
                    f'a[href*="a={a_gid.group(1)}&"]',
                    f'a[href$="a={a_gid.group(1)}"]',
                ],
            )
        return True

    # 3. Pagine navbar (dorf1/dorf2/karte): click sull'icona.
    # Se la query ha parametri oltre newdid (es. x/y, tt, a) il click
    # navbar li perderebbe -> False, il chiamante ripiega su page.goto.
    for nav in (
        selectors.PATH_DORF1,
        selectors.PATH_DORF2,
        selectors.PATH_MAP,
    ):
        if path.split("?", 1)[0] == nav or path == nav:
            _q = path.split("?", 1)[1] if "?" in path else ""
            _q = re.sub(r"newdid=\d+&?", "", _q).strip("&")
            if _q:
                return False
            if nav in (page.url or ""):
                return True
            return await _click_first(page, [f'a[href*="{nav}"]'])
    return False


class TravianBrowser:
    """Gestisce il ciclo di vita del browser e l'autenticazione."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        # True dopo che is_logged_in() ha confermato una sessione attiva
        # in questo processo: serve a distinguere 'mai autenticati' da
        # 'sessione caduta a meta' run' (login manuale altrove).
        self._had_session: bool = False

    def reset_session_tracking(self) -> None:
        """Resetta il tracciamento sessione dopo una pausa per conflitto."""
        self._had_session = False

    async def __aenter__(self) -> "TravianBrowser":
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.settings.headless
        )

        context_kwargs: dict = {
            "locale": self.settings.browser_locale,
            "viewport": {"width": 1920, "height": 1080},
        }
        if self.settings.state_file.exists():
            context_kwargs["storage_state"] = str(self.settings.state_file)
            logger.info("Sessione precedente caricata da %s", self.settings.state_file)

        self._context = await self._browser.new_context(**context_kwargs)
        self._context.set_default_navigation_timeout(
            self.settings.navigation_timeout_ms
        )
        self.page = await self._context.new_page()

    async def save_state(self) -> None:
        """Persiste cookie e localStorage su disco (state/session.json)."""
        if self._context is None:
            return
        self.settings.state_file.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self.settings.state_file))
        logger.info("Sessione salvata in %s", self.settings.state_file)

    async def stop(self) -> None:
        if self._context is not None:
            try:
                await self.save_state()
            except Exception:  # noqa: BLE001 - best effort in chiusura
                logger.exception("Impossibile salvare lo stato della sessione")
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = self.page = None

    def is_alive(self) -> bool:
        """Verifica se il browser e la pagina sono ancora aperti e reattivi."""
        if (
            self._playwright is None
            or self._browser is None
            or self._context is None
            or self.page is None
        ):
            return False
        try:
            return not self.page.is_closed()
        except Exception:
            return False

    async def reload(self) -> None:
        """Ricarica la pagina corrente in modo sicuro con delay."""
        if not self.is_alive():
            await self.recover()
            return
        assert self.page is not None
        logger.info("Ricaricamento della pagina in corso...")
        await self.page.reload(wait_until="domcontentloaded")
        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )

    async def recover(self) -> None:
        """Ripristina il contesto del browser in caso di crash o disconnessione."""
        logger.warning(
            "Ripristino del contesto browser per account '%s'...",
            self.settings.account_id,
        )
        try:
            await self.stop()
        except Exception:
            logger.debug("Errore durante la chiusura per ripristino", exc_info=True)
        await self.start()

    def url(self, path: str) -> str:
        return f"{self.settings.server_url}/{path.lstrip('/')}"

    async def goto(self, path: str) -> None:
        """Naviga verso ``path`` preferendo i click DOM ai GET diretti.

        Se ``settings.dom_navigation`` e' attivo, prova prima a cliccare
        il link corrispondente nella pagina corrente (sidebar villaggi
        per newdid=, navbar per dorf1/dorf2, slot della mappa per
        build.php?id=N). In assenza di elementi cliccabili ripiega su
        ``page.goto``: la resilienza viene prima del realismo.
        """
        assert self.page is not None
        logger.debug("Navigazione verso %s", path)
        if getattr(self.settings, "dom_navigation", False):
            try:
                if await navigate_via_dom(self.page, path):
                    await sleep_human(
                        self.settings.action_delay_min,
                        self.settings.action_delay_max,
                    )
                    return
            except Exception as exc:  # noqa: BLE001 — fallback a goto diretto
                logger.debug("Navigazione DOM fallita per %s: %s", path, exc)
        await self.page.goto(self.url(path), wait_until="domcontentloaded")
        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )


    async def _first(self, selector_list: list[str], timeout: float = 3000):
        """Primo locator che esiste fra le alternative, altrimenti None."""
        assert self.page is not None
        for sel in selector_list:
            locator = self.page.locator(sel).first
            try:
                if await locator.count() > 0:
                    return locator
            except PlaywrightTimeoutError:
                continue
        return None

    async def is_logged_in(self) -> bool:
        if await self._first(selectors.LOGIN_CONTAINERS) is not None:
            return False
        return await self._first(selectors.LOGGED_IN_MARKER) is not None

    async def ensure_logged_in(self) -> None:
        """Garantisce una sessione attiva con strategia ibrida.

        Ordine:
        1. sessione salvata (storage_state) -> gia' autenticati
        2. login automatico via form
        3. se fallisce: in modalita' visibile attesa del login manuale
           (l'utente puo' risolvere captcha/lobby); in headless si arrende
        """
        assert self.page is not None

        if not self.page.url.startswith(self.settings.server_url):
            await self.goto(selectors.PATH_INDEX)

        if await self.is_logged_in():
            self._had_session = True
            logger.debug("Sessione gia' attiva")
            return

        # La sessione era attiva in questo run e ora non lo e' piu':
        # in Travian un secondo login invalida la precedente — niente
        # re-login aggressivo, l'orchestrator decidera' la pausa.
        if self._had_session:
            raise ConcurrentSessionError(
                "Sessione invalidata a meta' run: probabile login manuale "
                "da un altro browser. Attendo prima di ritentare."
            )

        try:
            await self._automated_login()
            await self.save_state()
        except Exception:
            if self.settings.headless:
                await self.dump_debug("login_failed")
                raise RuntimeError(
                    "Login automatico fallito in headless. Esegui "
                    "'main.py --login' per autenticarti a mano e "
                    "salvare la sessione."
                )
            logger.warning(
                "Login automatico fallito: passo al login manuale "
                "(risolvi eventuali captcha nella finestra)"
            )
            await self.wait_for_manual_login()

    async def _automated_login(self) -> None:
        """Login via form usando le credenziali di .env."""
        assert self.page is not None
        if not self.settings.username or not self.settings.password:
            raise ValueError(
                f"Credenziali mancanti per l'account '{self.settings.account_id}'. "
                "Configura username e password oppure esegui il login manuale con 'main.py --login'."
            )
        logger.info("Sessione assente/scaduta: login automatico")
        await self.goto(selectors.PATH_LOGIN)

        username_input = await self._first(selectors.LOGIN_USERNAME_INPUT)
        password_input = await self._first(selectors.LOGIN_PASSWORD_INPUT)
        if username_input is None or password_input is None:
            raise RuntimeError(
                "Form di login non trovato: aggiorna selectors.LOGIN_* "
                "(o il server richiede login via lobby Travian Games)"
            )

        await username_input.click()
        await username_input.type(
            self.settings.username,
            delay=random.uniform(60, 140),  # battitura non istantanea
        )
        await password_input.click()
        await password_input.type(
            self.settings.password, delay=random.uniform(60, 140)
        )
        await sleep_human(
            self.settings.action_delay_min, self.settings.action_delay_max
        )

        submit = await self._first(selectors.LOGIN_SUBMIT_BUTTON)
        if submit is None:
            await password_input.press("Enter")
        else:
            await submit.click()

        await self.page.wait_for_load_state("domcontentloaded")
        await sleep_human(1.5, 3.0)

        if not await self.is_logged_in():
            raise RuntimeError(
                "Login automatico fallito (credenziali, captcha o "
                "selettori obsoleti)"
            )
        logger.info("Login automatico riuscito")
        self._had_session = True

    async def wait_for_manual_login(self, timeout: float = 300.0) -> None:
        """Attende che l'utente completi il login nel browser.

        Controlla periodicamente il marker di sessione: copre captcha,
        passaggio per la lobby Travian Games e qualsiasi flusso manuale.
        Al termine salva lo stato su disco.
        """
        assert self.page is not None
        if not self.page.url.startswith(self.settings.server_url):
            await self.goto(selectors.PATH_LOGIN)

        logger.info(
            "Completa il login nella finestra del browser "
            "(captcha/lobby inclusi). Timeout: %.0fs",
            timeout,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self.is_logged_in():
                logger.info("Login manuale rilevato")
                await self.save_state()
                self._had_session = True
                return
            await asyncio.sleep(2.0)
        raise TimeoutError(
            f"Login manuale non completato entro {timeout:.0f}s"
        )

    async def dump_debug(self, tag: str) -> Path:
        """Salva screenshot + HTML in debug/ per diagnosticare i selettori."""
        assert self.page is not None
        self.settings.debug_dir.mkdir(parents=True, exist_ok=True)
        shot = self.settings.debug_dir / f"{tag}.png"
        html = self.settings.debug_dir / f"{tag}.html"
        try:
            await self.page.screenshot(path=str(shot), full_page=True)
            html.write_text(await self.page.content(), encoding="utf-8")
        except Exception:  # noqa: BLE001
            logger.exception("Dump debug fallito")
        return shot
