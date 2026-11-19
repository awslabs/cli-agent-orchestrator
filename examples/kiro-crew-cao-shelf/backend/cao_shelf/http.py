"""The HTTP layer: ``ThreadingHTTPServer`` on loopback, the routes of api-contract.md, signatures.

Order of checks for every request except ``GET /health``: a proxy signature header must be present
(else 401, empty body), the body must fit in 64 KiB (413), the signature must verify over the raw
request target and the body (else 401), then the route runs. Errors leave as the envelope of
``errors.schema.json``. Logs carry the route template, the status and the duration, never a body,
a query or a session name.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Pattern, Tuple

from . import __version__
from .actions import Actions, CaoWriter, WriterFactory
from .auth import HEADER_V1, ProxyVerifier
from .cao import CaoClient
from .config import ConfigStore
from .errors import ShelfError
from .gateway import GatewayClient
from .ledger import Ledger
from .poller import ClientFactory, Poller
from .relays import Relays, ops_target_from_manifest
from .validation import check_name, check_terminal_id

log = logging.getLogger("cao_shelf.http")

MAX_BODY_BYTES = 64 * 1024
Response = Tuple[int, Any]
Route = Tuple[Pattern[str], str, Callable[..., Response]]


class App:
    """Everything the handlers need, wired once."""

    def __init__(
        self,
        data_dir: Path,
        secret: str,
        *,
        environ: Optional[Mapping[str, str]] = None,
        client_factory: Optional[ClientFactory] = None,
        writer_factory: Optional[WriterFactory] = None,
        clock: Callable[[], float] = time.time,
        gateway: Optional[GatewayClient] = None,
        manifest_path: Optional[Path] = None,
    ) -> None:
        self.data_dir = data_dir
        data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config = ConfigStore(data_dir / "config.json", environ)
        self.ledger = Ledger(
            data_dir / "ledger.json", enabled=bool(self.config.get()["persist_history"])
        )
        self.poller = Poller(
            client_factory or (lambda url, obs: CaoClient(url, observer=obs)),
            self.config.get,
            self.ledger,
            clock=clock,
        )
        self.actions = Actions(
            self.poller,
            self.config.get,
            writer_factory or (lambda url, obs: CaoWriter(url, observer=obs)),
            clock=clock,
        )
        self.verifier = ProxyVerifier(secret, clock=clock)
        self.gateway = gateway or GatewayClient(dict(os.environ if environ is None else environ))
        # inside Kiro Crew the data folder sits in the installed App, next to its app.json
        self.relays = Relays(
            data_dir / "relays.json",
            self.actions,
            self.poller,
            self.config.get,
            self.gateway,
            clock=clock,
            ops_target=ops_target_from_manifest(manifest_path or data_dir.parent / "app.json"),
        )

    def start(self) -> None:
        self.poller.start()
        self.relays.start()

    def stop(self) -> None:
        self.relays.stop()
        self.poller.stop()


# ----------------------------------------------------------------------------------- handlers
def _since(query: Mapping[str, List[str]]) -> Optional[int]:
    values = query.get("since")
    if not values:
        return None
    if re.fullmatch(r"[0-9]{1,12}", values[-1]) is None:
        raise ShelfError("invalid_request", "since must be a revision number")
    return int(values[-1])


def _h_shelf(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.poller.shelf(_since(query))


def _h_status(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, {"version": __version__, **app.poller.status()}


def _h_config_get(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.config.get()


def _h_config_put(app: App, m: Any, query: Any, body: Any) -> Response:
    out = app.config.update(_object(body))
    app.ledger.set_enabled(bool(out["persist_history"]))
    app.poller.wake()
    return 200, out


def _h_history(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.poller.history(check_name(m.group("name")))


def _h_history_rows(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.poller.history_rows(
        check_name(m.group("name")), check_terminal_id(m.group("tid"))
    )


def _h_history_all(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.poller.history_all()


def _h_prompt(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.actions.prompt(m.group("tid"))


def _h_profiles(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.actions.profiles()


def _h_ask(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.actions.ask(m.group("name"), _object(body))


def _h_answer(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.actions.answer(m.group("tid"), _object(body))


def _h_start(app: App, m: Any, query: Any, body: Any) -> Response:
    return 201, app.actions.start(_object(body))


def _h_stop_plan(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.actions.stop_plan(_object(body))


def _h_stop_execute(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.actions.stop_execute(_object(body))


def _h_relays(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.relays.list()


def _h_relay_create(app: App, m: Any, query: Any, body: Any) -> Response:
    return 201, app.relays.create(_object(body))


def _h_relay_end(app: App, m: Any, query: Any, body: Any) -> Response:
    return 200, app.relays.end(m.group("rid"), _object(body))


def _h_relay_takeover(app: App, m: Any, query: Any, body: Any) -> Response:
    return 201, app.relays.takeover(m.group("rid"), _object(body))


def _object(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise ShelfError("invalid_request", "the body must be a JSON object")
    return body


_NAME = r"(?P<name>[^/]+)"
_TID = r"(?P<tid>[^/]+)"
_RID = r"(?P<rid>[^/]+)"
GET_ROUTES: List[Route] = [
    (re.compile(r"/api/shelf"), "/api/shelf", _h_shelf),
    (re.compile(r"/api/status"), "/api/status", _h_status),
    (re.compile(r"/api/config"), "/api/config", _h_config_get),
    (re.compile(r"/api/history"), "/api/history", _h_history_all),
    (re.compile(r"/api/profiles"), "/api/profiles", _h_profiles),
    (re.compile(rf"/api/sessions/{_NAME}/history"), "/api/sessions/{name}/history", _h_history),
    (
        re.compile(rf"/api/sessions/{_NAME}/history/{_TID}/rows"),
        "/api/sessions/{name}/history/{id}/rows",
        _h_history_rows,
    ),
    (re.compile(rf"/api/terminals/{_TID}/prompt"), "/api/terminals/{id}/prompt", _h_prompt),
    (re.compile(r"/api/relays"), "/api/relays", _h_relays),
]
POST_ROUTES: List[Route] = [
    (re.compile(rf"/api/sessions/{_NAME}/ask"), "/api/sessions/{name}/ask", _h_ask),
    (re.compile(rf"/api/terminals/{_TID}/answer"), "/api/terminals/{id}/answer", _h_answer),
    (re.compile(r"/api/sessions"), "/api/sessions", _h_start),
    (re.compile(r"/api/stop/plan"), "/api/stop/plan", _h_stop_plan),
    (re.compile(r"/api/stop/execute"), "/api/stop/execute", _h_stop_execute),
    (re.compile(r"/api/relays"), "/api/relays", _h_relay_create),
    (re.compile(rf"/api/relays/{_RID}/end"), "/api/relays/{id}/end", _h_relay_end),
    (re.compile(rf"/api/relays/{_RID}/takeover"), "/api/relays/{id}/takeover", _h_relay_takeover),
]
PUT_ROUTES: List[Route] = [(re.compile(r"/api/config"), "/api/config", _h_config_put)]
TABLES = {"GET": GET_ROUTES, "POST": POST_ROUTES, "PUT": PUT_ROUTES}


def _match(method: str, path: str) -> Tuple[Optional[Route], Optional[Any], bool]:
    """(route, match, path_known_for_another_method)."""
    other = False
    for verb, table in TABLES.items():
        for route in table:
            found = route[0].fullmatch(path)
            if found is None:
                continue
            if verb == method:
                return route, found, True
            other = True
    return None, None, other


class Handler(BaseHTTPRequestHandler):
    server_version = "cao-shelf"
    sys_version = ""
    app: App  # set on the subclass made by ``make_server``

    # -- low level
    def _send(self, status: int, payload: Any = None, *, close: bool = False) -> None:
        raw = b"" if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        if payload is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _fail(self, err: ShelfError, *, close: bool = False) -> int:
        self._send(err.http_status, err.envelope(), close=close)
        return err.http_status

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    # -- one request
    def _serve(self) -> None:
        started = time.monotonic()
        template = "unmatched"
        status = 500
        try:
            parts = urllib.parse.urlsplit(self.path)
            method = self.command
            if method == "GET" and parts.path == "/health":
                template, status = "/health", 200
                self._send(200, {"status": "ok"})
                return
            if not self.headers.get(HEADER_V1):
                status = 401
                self._send(401, None, close=True)
                return
            length_text = self.headers.get("Content-Length", "0")
            if (
                self.headers.get("Transfer-Encoding")
                or re.fullmatch(r"[0-9]{1,10}", length_text) is None
            ):
                status = self._fail(
                    ShelfError("invalid_request", "a Content-Length is required"), close=True
                )
                return
            length = int(length_text)
            if length > MAX_BODY_BYTES:
                status = self._fail(
                    ShelfError("invalid_request", "the request body is too large", http_status=413),
                    close=True,
                )
                return
            raw = self.rfile.read(length) if length else b""
            if not self.app.verifier.verify(method, self.path, raw, self.headers):
                status = 401
                self._send(401, None)
                return
            route, found, known = _match(method, parts.path)
            if route is None:
                if known:
                    status = self._fail(
                        ShelfError("invalid_request", "method not allowed", http_status=405)
                    )
                else:
                    status = self._fail(ShelfError("not_found", "no such route"))
                return
            template = route[1]
            body: Any = None
            if method in ("POST", "PUT"):
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, UnicodeDecodeError):
                    raise ShelfError("invalid_request", "the body is not valid JSON") from None
            query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
            status, payload = route[2](self.app, found, query, body)
            self._send(status, payload)
        except ShelfError as err:
            status = self._fail(err)
        except Exception as exc:
            log.error("unhandled %s in %s", type(exc).__name__, template)
            status = self._fail(ShelfError("internal", "internal error"))
        finally:
            log.info(
                "%s %s %d %dms", self.command, template, status, (time.monotonic() - started) * 1000
            )

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _serve  # noqa: N815


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.error("connection error: %s", type(sys.exc_info()[1]).__name__)


class Server6(Server):
    address_family = socket.AF_INET6


def make_server(app: App, host: str, port: int) -> Server:
    """Bind a server. Refuses any address that is not loopback."""
    try:
        if not ipaddress.ip_address(host).is_loopback:
            raise ValueError
    except ValueError:
        raise ValueError(f"refusing to listen on {host!r}: loopback addresses only") from None
    handler = type("BoundHandler", (Handler,), {"app": app})
    return (Server6 if ":" in host else Server)((host, port), handler)


def data_dir_for(app_dir: Path, environ: Mapping[str, str]) -> Path:
    """``<app>/data`` inside Kiro Crew; ``CAO_SHELF_DATA_DIR`` only outside it (tests, dev)."""
    override = environ.get("CAO_SHELF_DATA_DIR")
    if override and "KIROCREW_SPAWNED" not in environ:
        return Path(override)
    return app_dir / "data"


def main(
    app_dir: Path, argv: Optional[List[str]] = None, environ: Optional[Mapping[str, str]] = None
) -> int:
    env = dict(os.environ if environ is None else environ)
    args = list(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    port_text = args[args.index("--port") + 1] if "--port" in args else env.get("PORT", "")
    if re.fullmatch(r"[0-9]{1,5}", port_text or "") is None:
        print("PORT is not set (Kiro Crew sets it) and no --port was given", file=sys.stderr)
        return 2
    app = App(
        data_dir_for(app_dir, env),
        env.get("KIROCREW_PROXY_SECRET", ""),
        environ=env,
        manifest_path=app_dir / "app.json",
    )
    if not env.get("KIROCREW_PROXY_SECRET"):
        log.warning("KIROCREW_PROXY_SECRET is not set: every route except /health will answer 401")
    server = make_server(app, "127.0.0.1", int(port_text))
    app.start()

    def shutdown(*_: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    log.info("CAO Shelf backend listening on 127.0.0.1:%d", server.server_address[1])
    try:
        server.serve_forever()
    finally:
        app.stop()
        server.server_close()
    return 0
