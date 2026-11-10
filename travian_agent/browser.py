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
import sys
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

from . import selectors
from .config import Settings

logger = logging.getLogger(__name__)

# Workaround Windows (WinError 10054): quando il processo Chromium chiude
# bruscamente la pipe, ProactorEventLoop esegue socket.shutdown() su una
# connessione gia' morta e asyncio logga "Exception in callback
# _call_connection_lost" con ConnectionResetError. E' rumore non bloccante
# (la connessione e' gia' chiusa): lo sopprimiamo.
if sys.platform.startswith("win"):
    try:
        from asyncio.proactor_events import _ProactorBasePipeTransport

        _orig_call_connection_lost = (
            _ProactorBasePipeTransport._call_connection_lost
        )

        def _safe_call_connection_lost(self, exc):  # type: ignore[no-untyped-def]
            try:
                _orig_call_connection_lost(self, exc)
            except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                pass

        _ProactorBasePipeTransport._call_connection_lost = (
            _safe_call_connection_lost
        )
    except Exception:  # noqa: BLE001 - patch opzionale, best effort
        pass


def human_delay(min_seconds: float = 0.8, max_seconds: float = 2.5) -> float:
    """Restituisce un delay casuale 'umano' (distribuzione quasi gaussiana)."""
    return random.uniform(min_seconds, max_seconds) * random.choice(
        [1.0, 1.0, 1.0, 1.15]
    )


async def sleep_human(min_seconds: float = 0.8, max_seconds: float = 2.5) -> None:
    await asyncio.sleep(human_delay(min_seconds, max_seconds))


class TravianBrowser:
    """Gestisce il ciclo di vita del browser e l'autenticazione."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None

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
        assert self.page is not None
        logger.debug("Navigazione verso %s", path)
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
            logger.debug("Sessione gia' attiva")
            return

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
