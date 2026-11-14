"""Loop temporale: Sense -> Decide -> Act, con delay umani.

Uso:
    python main.py --login        # login manuale una tantum (salva la sessione)
    python main.py                # loop continuo
    python main.py --once         # una sola iterazione (utile per i test)
    python main.py --dump         # salva screenshot+HTML della pagina in debug/
    python main.py --headed       # browser visibile
    python main.py --brain llm    # cervello LLM (stub -> fallback a regole)
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import logging
import os
import random
import sys

from travian_agent import selectors
from travian_agent.browser import TravianBrowser
from travian_agent.config import BASE_DIR, Settings, load_accounts, load_settings
from travian_agent.orchestrator import run_all_accounts, run_single_account_loop

logger = logging.getLogger("travian_agent")

PID_DIR = BASE_DIR / "state"


def _pid_path(account_id: str) -> "os.PathLike":
    """File che registra il PID dell'agente in esecuzione per l'account."""
    return PID_DIR / f"agent_{account_id}.pid"


def _pid_alive(pid: int) -> bool:
    """True se il processo con quel PID esiste (portabile, niente segnali)."""
    if sys.platform == "win32":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _acquire_agent_lock(accounts: list[Settings]) -> list | None:
    """Scrive i PID file degli account da avviare; None se uno e' gia' attivo.

    Impedisce istanze duplicate del bot (il problema dei processi orfani):
    se un agent_<id>.pid punta a un processo vivo, l'avvio viene rifiutato.
    """
    for acc in accounts:
        path = _pid_path(acc.account_id)
        if path.exists():
            try:
                old_pid = int(path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                old_pid = 0
            if old_pid and _pid_alive(old_pid):
                logger.error(
                    "Agente per '%s' gia' in esecuzione (PID %s). "
                    "Ferma quello attivo prima di riavviare.",
                    acc.account_id,
                    old_pid,
                )
                return None
            # PID stantio (processo morto senza cleanup): file rimosso sotto
    PID_DIR.mkdir(parents=True, exist_ok=True)
    for acc in accounts:
        _pid_path(acc.account_id).write_text(str(os.getpid()), encoding="utf-8")
        # Resetta un eventuale stop-flag non consumato da una fermata
        # precedente: senza pulizia l'agente uscirebbe al primo ciclo.
        (PID_DIR / f"stop_{acc.account_id}.flag").unlink(missing_ok=True)
    return [_pid_path(acc.account_id) for acc in accounts]


def _release_agent_lock(paths: list | None) -> None:
    """Rimuove i PID file scritti da questo processo."""
    for path in paths or []:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _asyncio_exception_handler(loop, context) -> None:
    """Silenzia l'errore cosmetico di chiusura pipe su Windows.

    Con ProactorEventLoop + subprocess Playwright, la chiusura forzata del
    pipe verso Chromium solleva ConnectionResetError (WinError 10054) in
    _call_connection_lost: e' rumore di shutdown, non un errore applicativo.
    """
    exc = context.get("exception")
    handle = context.get("handle")
    if (
        isinstance(exc, ConnectionResetError)
        and handle is not None
        and "_call_connection_lost" in repr(handle)
    ):
        return
    loop.default_exception_handler(context)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Agente per Travian Legends (Multi-Account Support)")
    parser.add_argument("--account", help="ID dell'account specifico da eseguire (se omesso e ci sono più account, li esegue tutti)")
    parser.add_argument("--once", action="store_true", help="una sola iterazione")
    parser.add_argument(
        "--dump",
        action="store_true",
        help="salva screenshot e HTML in debug/ dopo il login",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="apre il browser per il login manuale e salva la sessione, poi esce",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="mostra la finestra del browser (sovrascrive HEADLESS)",
    )
    parser.add_argument(
        "--brain",
        choices=["rules", "llm"],
        default="rules",
        help="tipo di cervello decisionale",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


async def main() -> int:
    asyncio.get_running_loop().set_exception_handler(_asyncio_exception_handler)
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    # DB centrale: schema + migrazione una-tantum dei JSON legacy in state/
    db.init_db()

    accounts = load_accounts()
    if not accounts:
        try:
            accounts = [load_settings(require_credentials=not args.login)]
        except Exception as exc:
            logger.error("Nessun account valido trovato: %s", exc)
            return 2

    # Filtro account se specificato
    if args.account:
        selected = [acc for acc in accounts if acc.account_id == args.account]
        if not selected:
            logger.error("Account '%s' non trovato tra quelli configurati.", args.account)
            return 1
        accounts = selected

    # Se login manuale: esegue solo per il primo account selezionato
    if args.login:
        target = accounts[0]
        target.headless = False
        logger.info("Avvio login manuale per account '%s' (%s)...", target.account_id, target.server_url)
        async with TravianBrowser(target) as browser:
            await browser.wait_for_manual_login()
        logger.info("Sessione salvata in %s", target.state_file)
        return 0

    if args.headed:
        for acc in accounts:
            acc.headless = False

    # Guardia anti-duplicati: rifiuta se un agente dello stesso account e' vivo
    pid_paths = _acquire_agent_lock(accounts)
    if pid_paths is None:
        return 3

    try:
        if len(accounts) == 1:
            await run_single_account_loop(
                accounts[0],
                brain_type=args.brain,
                once=args.once,
                dump=args.dump,
            )
        else:
            await run_all_accounts(
                accounts,
                brain_type=args.brain,
                once=args.once,
                dump=args.dump,
            )
    finally:
        _release_agent_lock(pid_paths)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nInterrotto dall'utente.")
