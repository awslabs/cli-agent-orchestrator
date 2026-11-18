"""Shared helpers for the CAO Shelf tests (not a test module).

* ``FIXTURES``: the shared anonymised data under ``fixtures/``.
* ``FakeCao``: a stateful in-process stand-in for CAO that replays ``fixtures/cao/<day>``. It can be
  used through ``FakeOpener`` (no sockets) or served by ``serve_fake_cao`` on a loopback port.
* ``find_port`` / ``serve``: local servers use only the ports 9991 to 9995.
"""

from __future__ import annotations

import copy
import hashlib
import http.server
import io
import json
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
SCHEMAS = ROOT / "schemas"
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

PORT_RANGE = range(9991, 9996)
SECRET = "test-secret-not-real"
SENTINEL = "SENTINEL-BODY-7f3"


def load(path: Path) -> Any:
    return json.loads(path.read_text())


RC_IDS = (
    "tag",
    "catchup_tag",
    "brief",
    "handshake",
    "handshake_unsent",
    "setup",
    "ready",
    "ended",
    "ended_reply",
    "reply_open",
    "reply_close",
    "busy",
    "waiting",
    "waiting_during",
    "still_working",
    "target_ended",
    "cao_unreachable",
    "send_failed",
    "queued",
    "cancelled",
    "stopped_waiting",
    "answer_box",
    "refuse_target",
    "crew_notice",
    "sessions",
)


def contract_texts() -> Dict[str, str]:
    """The remote-control texts of api-contract.md: the fenced block after each ``rc:`` marker."""
    import re

    text = (ROOT / "api-contract.md").read_text(encoding="utf-8")
    found = re.findall(r"<!-- rc:([a-z_]+) -->\n```text\n(.*?)\n```", text, flags=re.S)
    return {key: body for key, body in found}


def clock_of(day: str) -> datetime:
    raw = load(FIXTURES / "cao" / day / "clock.json")["now"]
    return datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fixture_view(day: str) -> Tuple[Dict[str, list], Dict[str, Optional[str]]]:
    """(cao_view, caller_ids) as the tiering function takes them."""
    base = FIXTURES / "cao" / day
    view: Dict[str, list] = {}
    callers: Dict[str, Optional[str]] = {}
    for row in load(base / "sessions.json"):
        data = load(base / f"session.{row['name'][4:]}.json")
        view[row["name"]] = data["terminals"]
        for term in data["terminals"][1:]:
            callers[term["id"]] = load(base / f"terminal.{term['id']}.json").get("caller_id")
    return view, callers


class FakeCao:
    """Replays a fixture day; mutable so a test can change statuses or delete sessions."""

    def __init__(self, day: str = "busy") -> None:
        base = FIXTURES / "cao" / day
        self.now = clock_of(day)
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.order: List[str] = []
        self.terminals: Dict[str, Dict[str, Any]] = {}
        self.inbox: Dict[str, List[Dict[str, Any]]] = {}
        self.outputs: Dict[str, str] = {}
        self.calls: List[Tuple[str, str, Dict[str, str]]] = []
        self.bodies: List[Optional[bytes]] = []
        self.mode = "ok"  # ok | down | slow | 401 | 500 | badshape
        self.profiles: List[Dict[str, Any]] = [
            {"name": "developer", "description": "writes code", "source": "built-in"},
            {"name": "supervisor", "description": "coordinates"},
        ]
        self.lock = threading.RLock()
        self.next_message_id = 1000
        for row in load(base / "sessions.json"):
            bare = row["name"][4:]
            data = load(base / f"session.{bare}.json")
            self.sessions[row["name"]] = {"row": row, "terminals": data["terminals"]}
            self.order.append(row["name"])
            for i, term in enumerate(data["terminals"]):
                full = copy.deepcopy(term)
                full["session_name"] = row["name"]
                full["name"] = term["tmux_window"]
                full["caller_id"] = None
                if i > 0:
                    full["caller_id"] = load(base / f"terminal.{term['id']}.json")["caller_id"]
                self.terminals[term["id"]] = full
            cond = data["terminals"][0]["id"]
            self.inbox[cond] = load(base / f"inbox.{cond}.json")

    # -- helpers a test uses to change the world ------------------------------------------------
    def set_status(self, tid: str, status: str, last_active: Optional[str] = None) -> None:
        with self.lock:
            for sess in self.sessions.values():
                for term in sess["terminals"]:
                    if term["id"] == tid:
                        term["status"] = status
                        if last_active:
                            term["last_active"] = last_active
            self.terminals[tid]["status"] = status

    def add_session(self, bare: str, terminals: List[Tuple[str, str, str, Optional[str]]]) -> None:
        """terminals: (id, status, last_active, caller_id); the first one is the conductor."""
        name = f"cao-{bare}"
        rows = []
        for i, (tid, status, last, caller) in enumerate(terminals):
            row = {
                "id": tid,
                "tmux_session": name,
                "tmux_window": "conductor" if i == 0 else f"w{i}",
                "provider": "mock_cli",
                "agent_profile": "supervisor" if i == 0 else "coder",
                "last_active": last,
                "pending_inbox_count": 0,
                "status": status,
            }
            rows.append(row)
            self.terminals[tid] = dict(
                row, session_name=name, name=row["tmux_window"], caller_id=caller
            )
        with self.lock:
            self.sessions[name] = {
                "row": {"id": name, "name": name, "status": "detached"},
                "terminals": rows,
            }
            self.order.append(name)
            self.inbox[terminals[0][0]] = []

    def session_terminals(self, bare: str) -> List[Dict[str, Any]]:
        return self.sessions[f"cao-{bare}"]["terminals"]

    def writes(self) -> List[Tuple[str, str, Dict[str, str]]]:
        return [c for c in self.calls if c[0] in ("POST", "DELETE", "PUT", "PATCH")]

    def deletes(self) -> List[str]:
        return [c[1].rsplit("/", 1)[-1] for c in self.calls if c[0] == "DELETE"]

    def count(self, method: str, prefix: str) -> int:
        return sum(1 for m, p, _ in self.calls if m == method and p.startswith(prefix))

    # -- the CAO routes -------------------------------------------------------------------------
    def handle(self, method: str, target: str, body: Optional[bytes] = None) -> Tuple[int, Any]:
        parts = urllib.parse.urlsplit(target)
        path = parts.path
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query, True).items()}
        with self.lock:
            self.calls.append((method, path, query))
            self.bodies.append(body)
            if self.mode == "401":
                return 401, {"detail": "auth"}
            if self.mode == "500":
                return 500, {"detail": "internal"}
            if self.mode == "badshape":
                return 200, {"unexpected": True} if path != "/sessions" else {"not": "a list"}
            return self._route(method, path, query, body)

    def _route(
        self, method: str, path: str, query: Dict[str, str], body: Optional[bytes]
    ) -> Tuple[int, Any]:
        seg = [s for s in path.split("/") if s]
        if method == "GET":
            if seg == ["health"]:
                return 200, {"status": "ok", "version": "2.5.4", "terminal_backend": "tmux"}
            if seg == ["sessions"]:
                return 200, [self.sessions[n]["row"] for n in self.order]
            if len(seg) == 2 and seg[0] == "sessions":
                sess = self.sessions.get(seg[1])
                if sess is None:
                    return 404, {"detail": "no such session"}
                return 200, {"session": sess["row"], "terminals": sess["terminals"]}
            if len(seg) == 2 and seg[0] == "terminals":
                term = self.terminals.get(seg[1])
                return (200, term) if term else (404, {"detail": "no such terminal"})
            if len(seg) == 3 and seg[0] == "terminals" and seg[2] == "output":
                if seg[1] not in self.terminals:
                    return 404, {"detail": "no such terminal"}
                text = self.outputs.get(seg[1], "line one\nAPPROVAL_REQUIRED: push to origin?\n")
                return 200, {"output": text, "mode": query.get("mode", "full")}
            if len(seg) == 4 and seg[0] == "terminals" and seg[2:] == ["inbox", "messages"]:
                rows = list(self.inbox.get(seg[1], []))
                if "sender_id" in query:
                    rows = [r for r in rows if r["sender_id"] == query["sender_id"]]
                if query.get("order") == "desc":
                    rows.sort(key=lambda r: r["created_at"], reverse=True)
                return 200, rows[: int(query.get("limit", "10"))]
            if seg == ["agents", "profiles"]:
                return 200, self.profiles
        if method == "POST":
            if len(seg) == 4 and seg[0] == "terminals" and seg[2:] == ["inbox", "messages"]:
                if seg[1] not in self.terminals:
                    return 404, {"detail": "no such terminal"}
                self.next_message_id += 1
                receiver = self.terminals[seg[1]]
                self.inbox.setdefault(seg[1], []).append(
                    {
                        "id": self.next_message_id,
                        "sender_id": query.get("sender_id"),
                        "receiver_id": seg[1],
                        "message": query.get("message", ""),
                        "status": (
                            "delivered"
                            if receiver["status"] in ("idle", "completed")
                            else "pending"
                        ),
                        "created_at": "2026-11-15T12:00:00+00:00",
                    }
                )
                return 200, {
                    "success": True,
                    "message_id": self.next_message_id,
                    "sender_id": query.get("sender_id"),
                    "receiver_id": seg[1],
                    "created_at": "2026-11-15T12:00:00+00:00",
                }
            if len(seg) == 3 and seg[0] == "terminals" and seg[2] == "input":
                if seg[1] not in self.terminals:
                    return 404, {"detail": "no such terminal"}
                if self.terminals[seg[1]]["status"] == "waiting_user_answer":
                    self.set_status(seg[1], "completed")
                return 200, {"success": True, "dispatch_boundary": 0}
            if seg == ["sessions"]:
                name = query.get("session_name") or "cao-new-session"
                if name in self.sessions:
                    return 409, {"detail": "exists"}
                tid = f"{abs(hash(name)) % 0xFFFFFFFF:08x}"
                term = {
                    "id": tid,
                    "tmux_session": name,
                    "tmux_window": "conductor",
                    "provider": query.get("provider", "mock_cli"),
                    "agent_profile": query.get("agent_profile"),
                    "last_active": "2026-11-15T12:00:00.000000Z",
                    "pending_inbox_count": 0,
                    "status": "idle",
                    "session_name": name,
                }
                self.sessions[name] = {
                    "row": {"id": name, "name": name, "status": "detached"},
                    "terminals": [term],
                }
                self.order.append(name)
                self.terminals[tid] = dict(term, caller_id=None, name="conductor")
                self.inbox[tid] = []
                return 201, term
        if method == "DELETE" and len(seg) == 2 and seg[0] == "sessions":
            if seg[1] not in self.sessions:
                return 404, {"detail": "no such session"}
            for term in self.sessions[seg[1]]["terminals"]:
                self.terminals.pop(term["id"], None)
            del self.sessions[seg[1]]
            self.order.remove(seg[1])
            return 200, {"success": True, "deleted": [seg[1]], "errors": []}
        return 404, {"detail": "no route"}


class _Resp:
    def __init__(self, status: int, payload: bytes) -> None:
        self.status = status
        self._buf = io.BytesIO(payload)

    def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)

    def close(self) -> None:
        pass

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


class FakeOpener:
    """Looks like ``urllib.request.OpenerDirector`` for the client, backed by ``FakeCao``."""

    def __init__(self, cao: FakeCao) -> None:
        self.cao = cao
        self.timeouts: List[Optional[float]] = []

    def open(self, req: urllib.request.Request, timeout: Optional[float] = None) -> _Resp:
        self.timeouts.append(timeout)
        if self.cao.mode == "down":
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        if self.cao.mode == "slow":
            raise urllib.error.URLError(socket.timeout("timed out"))
        parts = urllib.parse.urlsplit(req.full_url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        status, payload = self.cao.handle(req.get_method(), target, req.data)
        raw = json.dumps(payload).encode()
        if status >= 400:
            body = io.BytesIO(raw)
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, body)  # type: ignore[arg-type]
        return _Resp(status, raw)


# ---------------------------------------------------------------- loopback servers (9991 to 9995)
def find_port(skip: Tuple[int, ...] = ()) -> int:
    """First port in 9991-9995 that can be bound on 127.0.0.1 right now."""
    for port in PORT_RANGE:
        if port in skip:
            continue
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free test port in 9991-9995")


class Served:
    def __init__(self, server: http.server.HTTPServer, thread: threading.Thread) -> None:
        self.server = server
        self.thread = thread
        self.port = int(server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def serve(handler: Callable[..., http.server.BaseHTTPRequestHandler]) -> Served:
    last: Optional[Exception] = None
    for port in PORT_RANGE:
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError as exc:
            last = exc
            continue
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        thread.start()
        return Served(server, thread)
    raise RuntimeError(f"no free test port in 9991-9995: {last}")


def serve_fake_cao(cao: FakeCao) -> Served:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _go(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            if cao.mode == "down":
                self.close_connection = True
                return
            if cao.mode == "slow":
                threading.Event().wait(1.5)
            status, payload = cao.handle(self.command, self.path, body)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_DELETE = do_PUT = _go  # noqa: N815

        def log_message(self, *args: Any) -> None:
            return None

    return serve(Handler)


# --------------------------------------------------------------------------- the real backend
import http.client  # noqa: E402

from cao_shelf.auth import signed_headers  # noqa: E402
from cao_shelf.http import App, make_server  # noqa: E402


def schema_registry() -> Any:
    from referencing import Registry, Resource

    resources = []
    for path in sorted(SCHEMAS.glob("*.json")):
        doc = load(path)
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def validate(schema_file: str, instance: Any, defn: Optional[str] = None) -> None:
    """Validate against ``schemas/<schema_file>`` (or one of its ``$defs``)."""
    import jsonschema

    doc = load(SCHEMAS / schema_file)
    schema: Any = doc if defn is None else {"$ref": f"{doc['$id']}#/$defs/{defn}"}
    jsonschema.Draft202012Validator(schema, registry=schema_registry()).validate(instance)


class Reply:
    def __init__(self, status: int, raw: bytes, headers: Dict[str, str]) -> None:
        self.status = status
        self.raw = raw
        self.headers = headers

    @property
    def json(self) -> Any:
        return json.loads(self.raw.decode()) if self.raw else None


class Stack:
    """The real backend on a loopback port, talking to a fake CAO on another."""

    def __init__(
        self,
        data_dir: Path,
        day: str = "busy",
        *,
        secret: str = SECRET,
        thread: bool = False,
        environ: Optional[Dict[str, str]] = None,
        gateway: Optional[Any] = None,
    ) -> None:
        self.cao = FakeCao(day)
        self.fake = serve_fake_cao(self.cao)
        self.now = clock_of(day).timestamp()
        self.secret = secret
        env = {"CAO_SHELF_CAO_URL": f"http://127.0.0.1:{self.fake.port}"}
        env.update(environ or {})
        self.app = App(data_dir, secret, environ=env, clock=lambda: self.now, gateway=gateway)
        port = find_port(skip=(self.fake.port,))
        self.server = make_server(self.app, "127.0.0.1", port)
        self.port = port
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()
        if thread:
            self.app.start()

    def warm(self, ticks: int = 4) -> "Stack":
        for _ in range(ticks):
            self.app.poller.tick()
        return self

    def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        sign: bool = True,
        raw: Optional[bytes] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Reply:
        payload = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        hdrs: Dict[str, str] = {}
        if sign:
            hdrs.update(signed_headers(self.secret, method, path, payload, now=self.now))
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=payload if payload else None, headers=hdrs)
            resp = conn.getresponse()
            return Reply(resp.status, resp.read(), {k: v for k, v in resp.getheaders()})
        finally:
            conn.close()

    def get(self, path: str, **kw: Any) -> Reply:
        return self.call("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Reply:
        return self.call("POST", path, body, **kw)

    def close(self) -> None:
        self.app.stop()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(5)
        self.fake.close()


# ------------------------------------------------------------------ in-process steering rig
from cao_shelf.actions import Actions, CaoWriter  # noqa: E402
from cao_shelf.cao import CaoClient  # noqa: E402
from cao_shelf.config import DEFAULTS  # noqa: E402
from cao_shelf.ledger import Ledger  # noqa: E402
from cao_shelf.poller import Poller  # noqa: E402


class ActionsRig:
    """Poller and Actions on a fake clock over ``FakeCao`` with no sockets."""

    def __init__(
        self, tmp: Path, day: str = "busy", settings: Optional[Dict[str, Any]] = None
    ) -> None:
        self.cao = FakeCao(day)
        self.now = clock_of(day).timestamp()
        self.settings: Dict[str, Any] = dict(DEFAULTS, **(settings or {}))
        self.opener = FakeOpener(self.cao)
        self.poller = Poller(
            lambda url, obs: CaoClient(url, self.opener, observer=obs),
            lambda: self.settings,
            Ledger(tmp / "ledger.json", enabled=False),
            clock=lambda: self.now,
        )
        self.actions = Actions(
            self.poller,
            lambda: self.settings,
            lambda url, obs: CaoWriter(url, self.opener, observer=obs),
            clock=lambda: self.now,
        )

    def warm(self, ticks: int = 6) -> "ActionsRig":
        for _ in range(ticks):
            self.poller.tick()
            self.now += 5
        return self

    def add_stale(self, count: int, hours_ago: float = 100.0, prefix: str = "old") -> List[str]:
        names = []
        for i in range(count):
            when = (clock_of("busy") - timedelta(hours=hours_ago + i)).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            )
            tid = hashlib.sha1(f"{prefix}/{i}".encode()).hexdigest()[:8]
            self.cao.add_session(f"{prefix}-{i}", [(tid, "idle", when, None)])
            names.append(f"{prefix}-{i}")
        return names


# ------------------------------------------------------------------ a fake Kiro Crew gateway
class FakeGateway:
    """The Kiro Crew chat routes CAO Shelf calls, with an app-token check.

    ``slots`` maps a slot key to ``{"title", "agent", "agent_kind", "mode", "messages"}``. A test
    can set ``fail`` to ``{(method, route): status}`` (route as in ``route_of``) to refuse a call,
    append to ``tokens`` so the cached one gets 401, and append rows to a slot's ``messages``.
    """

    APP = "cao-shelf"

    def __init__(self, secret: str = SECRET, port: int = 5499) -> None:
        self.secret = secret
        self.port = port
        self.tokens: List[str] = []
        self.slots: Dict[str, Dict[str, Any]] = {}
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.headers: List[Dict[str, str]] = []
        self.fail: Dict[Tuple[str, str], int] = {}
        self.approved: List[Tuple[str, str]] = []
        self.lock = threading.RLock()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def environ(self) -> Dict[str, str]:
        return {
            "KIROCREW_GATEWAY_ORIGIN": self.origin,
            "KIROCREW_PROXY_SECRET": self.secret,
            "KIROCREW_APP_NAME": self.APP,
        }

    @staticmethod
    def route_of(path: str) -> str:
        seg = [s for s in path.split("/") if s]
        if len(seg) >= 4 and seg[:3] == ["api", "chat", "slots"]:
            return "/api/chat/slots/{slot}" + ("/" + "/".join(seg[4:]) if len(seg) > 4 else "")
        return path

    def count(self, method: str, route: str) -> int:
        return sum(1 for m, r, _ in self.calls if m == method and r == route)

    def messages(self, slot: str) -> List[str]:
        return [str(m["content"]) for m in self.slots[slot]["messages"] if m["role"] == "user"]

    def add_rows(self, slot: str, *rows: Dict[str, Any]) -> None:
        with self.lock:
            self.slots[slot]["messages"].extend(rows)

    def handle(
        self, method: str, target: str, headers: Mapping[str, str], body: Optional[bytes]
    ) -> Tuple[int, Any]:
        parts = urllib.parse.urlsplit(target)
        path = parts.path
        route = self.route_of(path)
        data = json.loads(body.decode()) if body else {}
        with self.lock:
            self.calls.append((method, route, data))
            self.headers.append({k.lower(): v for k, v in headers.items()})
            if route == f"/api/apps/{self.APP}/token" and method == "POST":
                if headers.get("X-App-Secret") != self.secret:
                    return 403, {"error": "invalid secret"}
                self.tokens.append(f"tok-{len(self.tokens) + 1}")
                return 200, {"token": self.tokens[-1]}
            cookie = headers.get("Cookie", "")
            if not self.tokens or cookie != f"mc_token_{self.port}={self.tokens[-1]}":
                return 401, {"error": "unauthorized"}
            if (method, route) in self.fail:
                return self.fail[(method, route)], {"error": "refused", "code": "test_refusal"}
            return self._route(method, path, route, data, parts.query)

    def _route(
        self, method: str, path: str, route: str, data: Dict[str, Any], query: str
    ) -> Tuple[int, Any]:
        seg = [s for s in path.split("/") if s]
        slot = urllib.parse.unquote(seg[3]) if len(seg) >= 4 else ""
        if method == "POST" and route == "/api/chat/slots":
            key = str(data.get("name"))
            self.slots[key] = {
                "title": data.get("title"),
                "agent": data.get("agent"),
                "agent_kind": data.get("agent_kind"),
                "mode": None,
                "messages": [],
            }
            return 200, {"key": key, "title": data.get("title")}
        if method == "POST" and route == "/api/chat/mode":
            if data.get("slot") not in self.slots:
                return 404, {"error": "not found", "code": "slot_not_found"}
            self.slots[str(data["slot"])]["mode"] = data.get("mode")
            return 200, {"ok": True, "mode": data.get("mode")}
        if method == "POST" and route == "/api/chat":
            if data.get("slot") not in self.slots:
                return 404, {"error": "not found", "code": "slot_not_found"}
            self.slots[str(data["slot"])]["messages"].append(
                {"role": "user", "content": data.get("message"), "meta": {"turnActor": "app"}}
            )
            return 200, {"ok": True, "slot": data["slot"]}
        if slot not in self.slots:
            return 404, {"error": "not found", "code": "slot_not_found"}
        if method == "DELETE" and route == "/api/chat/slots/{slot}":
            del self.slots[slot]
            return 200, {"ok": True}
        if method == "GET" and route == "/api/chat/slots/{slot}":
            return 200, {"key": slot, "messages": list(self.slots[slot]["messages"])}
        if method == "POST" and route == "/api/chat/slots/{slot}/approve":
            self.approved.append((slot, str(data.get("request_id"))))
            for row in self.slots[slot]["messages"]:
                if row.get("role") == "permission":
                    cls = json.loads(row.get("cls") or "{}")
                    if cls.get("request_id") == data.get("request_id"):
                        cls["resolved"] = data.get("action")
                        row["cls"] = json.dumps(cls)
            return 200, {"ok": True}
        return 404, {"error": "no route"}


class FakeGatewayOpener:
    """``urllib`` opener stand-in backed by ``FakeGateway`` (no sockets)."""

    def __init__(self, gw: FakeGateway) -> None:
        self.gw = gw
        self.down = False
        self.urls: List[str] = []

    def open(self, req: urllib.request.Request, timeout: Optional[float] = None) -> _Resp:
        self.urls.append(req.full_url)
        if self.down:
            raise urllib.error.URLError(ConnectionRefusedError(111, "refused"))
        parts = urllib.parse.urlsplit(req.full_url)
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        headers = {k.title(): v for k, v in req.header_items()}
        status, payload = self.gw.handle(req.get_method(), target, headers, req.data)
        raw = json.dumps(payload).encode()
        if status >= 400:
            raise urllib.error.HTTPError(
                req.full_url, status, "err", {}, io.BytesIO(raw)  # type: ignore[arg-type]
            )
        return _Resp(status, raw)


def serve_fake_gateway(gw: FakeGateway) -> Served:
    class Handler(http.server.BaseHTTPRequestHandler):
        def _go(self) -> None:
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else None
            headers = {k.title(): v for k, v in self.headers.items()}
            status, payload = gw.handle(self.command, self.path, headers, body)
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = do_DELETE = _go  # noqa: N815

        def log_message(self, *args: Any) -> None:
            return None

    served = serve(Handler)
    gw.port = served.port
    return served


# ------------------------------------------------------------------ relays over both fakes
from cao_shelf.gateway import GatewayClient  # noqa: E402
from cao_shelf.relays import Relays  # noqa: E402

RELAY_SESSION = "rc-target"
RELAY_CONDUCTOR = "acc0de01"
RELAY_HELPER = "acc0de02"


class RelayRig(ActionsRig):
    """``ActionsRig`` plus a fake gateway and ``Relays`` writing to ``<tmp>/data``.

    It adds one idle session, ``rc-target``, whose conductor is ``acc0de01``."""

    def __init__(
        self,
        tmp: Path,
        day: str = "busy",
        settings: Optional[Dict[str, Any]] = None,
        *,
        ops_target: Optional[str] = None,
    ) -> None:
        super().__init__(tmp, day, settings)
        when = clock_of(day).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        self.cao.add_session(
            RELAY_SESSION,
            [
                (RELAY_CONDUCTOR, "idle", when, None),
                (RELAY_HELPER, "idle", when, RELAY_CONDUCTOR),
            ],
        )
        self.gw = FakeGateway()
        self.gw_opener = FakeGatewayOpener(self.gw)
        self.gateway = GatewayClient(self.gw.environ(), opener=self.gw_opener)
        self.data = tmp / "data"
        self.data.mkdir(exist_ok=True)
        self.ops_target = ops_target
        self.relays = self.make_relays()

    def make_relays(self) -> Relays:
        return Relays(
            self.data / "relays.json",
            self.actions,
            self.poller,
            lambda: self.settings,
            self.gateway,
            clock=lambda: self.now,
            ops_target=self.ops_target,
        )

    def cao_writes(self) -> List[Tuple[str, str, Dict[str, str]]]:
        return self.cao.writes()

    def attach(self, session: str = RELAY_SESSION, **extra: Any) -> Dict[str, Any]:
        return self.relays.create({"session": session, "acknowledge_risk": True, **extra})

    def data_text(self) -> str:
        return "\n".join(
            p.read_text(encoding="utf-8", errors="replace")
            for p in sorted(self.data.rglob("*"))
            if p.is_file()
        )
