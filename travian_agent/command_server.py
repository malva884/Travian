"""Mini server HTTP per il Command Center live.

Espone su una porta dedicata (default 8610):

- ``GET  /api/dashboard-state?account=<id>`` — ultimo snapshot di
  telemetria scritto dall'orchestratore (``state/dashboard_state_*.json``);
- ``POST /api/command`` — accoda un comando rapido per l'agente
  (``{"action","village_name","params","account_id"}``);
- ``GET  /command-center[/...]`` — serve i file statici di
  ``web/command_center`` (index.html).

Viene avviato in un thread daemon da ``app.py`` (dashboard Streamlit)
e opzionalmente da ``main.py``: l'eventuale bind fallito su porta gia'
occupata viene ignorato — vale il primo server attivo.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import BASE_DIR
from .telemetry import (
    SUPPORTED_COMMANDS,
    enqueue_command,
    latest_telemetry_path,
    telemetry_path,
)

logger = logging.getLogger(__name__)

WEB_DIR = BASE_DIR / "web" / "command_center"
COMMAND_PORT = 8610

_server_started = False
_start_lock = threading.Lock()


def _read_snapshot(account_id: str = "") -> tuple[dict | None, int]:
    """Snapshot JSON per l'account richiesto o il piu' recente."""
    path = None
    if account_id:
        candidate = telemetry_path(account_id)
        if candidate.exists():
            path = candidate
    if path is None:
        path = latest_telemetry_path()
    if path is None:
        return None, 503
    try:
        return json.loads(path.read_text(encoding="utf-8")), 200
    except (OSError, ValueError):
        return None, 500


def _default_account_id() -> str:
    """Account id dello snapshot piu' recente (per i POST senza campo).

    Fallback su accounts.json quando l'agente non ha ancora prodotto
    il primo snapshot: i comandi (es. FORCE_TICK) devono funzionare
    anche a freddo.
    """
    path = latest_telemetry_path()
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            acc = str(data.get("account_id") or "")
            if acc:
                return acc
        except (OSError, ValueError):
            pass
    try:
        from .config import load_accounts

        accounts = load_accounts()
        if accounts:
            return str(accounts[0].account_id or "")
    except Exception:
        pass
    return ""


class _CommandCenterHandler(BaseHTTPRequestHandler):
    server_version = "TravianCommandCenter/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.debug("command_server: " + fmt, *args)

    # ------------------------------------------------------------------
    # Helpers risposta
    # ------------------------------------------------------------------
    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type"
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json({"error": "not found"}, status=404)
            return
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # Verbi HTTP
    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        if route == "/api/dashboard-state":
            account = (parse_qs(parsed.query).get("account") or [""])[0]
            payload, status = _read_snapshot(account)
            if payload is None:
                self._send_json(
                    {
                        "status": "waiting",
                        "reason": "nessuno snapshot disponibile: "
                        "l'agente non ha ancora completato un ciclo",
                    },
                    status=status,
                )
            else:
                self._send_json(payload)
            return
        if route in ("", "/", "/command-center"):
            self._send_file(WEB_DIR / "index.html")
            return
        if route.startswith("/command-center"):
            rel = route[len("/command-center"):].lstrip("/") or "index.html"
            candidate = (WEB_DIR / rel).resolve()
            try:
                candidate.relative_to(WEB_DIR.resolve())
            except ValueError:
                self._send_json({"error": "forbidden"}, status=403)
                return
            self._send_file(candidate)
            return
        self._send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/command":
            self._send_json({"error": "not found"}, status=404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        try:
            raw = self.rfile.read(max(0, length))
            body = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"error": "invalid json"}, status=400)
            return
        action = str(body.get("action") or "").strip().upper()
        if action not in SUPPORTED_COMMANDS:
            self._send_json(
                {
                    "status": "rejected",
                    "reason": f"azione '{action}' non supportata",
                    "supported": sorted(SUPPORTED_COMMANDS),
                },
                status=400,
            )
            return
        account_id = str(body.get("account_id") or "") or _default_account_id()
        if not account_id:
            self._send_json(
                {"status": "rejected", "reason": "account_id non risolvibile"},
                status=400,
            )
            return
        result = enqueue_command(
            account_id,
            action,
            village_name=str(body.get("village_name") or ""),
            params=body.get("params") if isinstance(body.get("params"), dict) else {},
        )
        status = 200 if result.get("status") == "queued" else 400
        self._send_json(result, status=status)


def start_command_server(port: int = COMMAND_PORT) -> bool:
    """Avvia il server in un thread daemon. True se il bind e' riuscito.

    Idempotente: una seconda chiamata nello stesso processo e' un no-op;
    su porta occupata (altro processo) ritorna False senza eccezioni.
    """
    global _server_started
    with _start_lock:
        if _server_started:
            return True
        try:
            server = ThreadingHTTPServer(
                ("127.0.0.1", port), _CommandCenterHandler
            )
        except OSError as exc:
            logger.warning(
                "Command Center: porta %d non disponibile (%s)", port, exc
            )
            return False
        thread = threading.Thread(
            target=server.serve_forever,
            name="command-center-http",
            daemon=True,
        )
        thread.start()
        _server_started = True
        logger.info(
            "Command Center live su http://127.0.0.1:%d/command-center/",
            port,
        )
        return True
