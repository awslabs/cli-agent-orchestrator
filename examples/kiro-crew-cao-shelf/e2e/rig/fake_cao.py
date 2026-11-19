#!/usr/bin/env python3
"""A fake CAO that replays ``fixtures/cao/<day>/`` (stdlib only).

    python e2e/rig/fake_cao.py --day busy --port 0 [--time fixture|rebased] [--print-url]

Replays: GET /health, /sessions, /sessions/{name}, /terminals/{id}, /terminals/{id}/inbox/messages,
/terminals/{id}/output, /agents/profiles. Write routes (POST /sessions, POST /sessions/{n}/terminals,
POST /terminals/{id}/input, POST /terminals/{id}/inbox/messages, DELETE /sessions/{n}, DELETE /terminals/{id})
are recorded, answer like CAO does, and are readable at ``GET /__calls``. Deleting a session removes it
from the replay so a later read shows it gone.

Control routes (not part of CAO): ``GET /__calls``, ``POST /__calls/reset``, ``POST /__mode`` with a JSON
body ``{"mode": "ok|down|slow|401|badshape"}`` (``slow`` answers after ``slow_seconds``, default 12).

Times: ``--time fixture`` (default) serves the fixture timestamps untouched, so the clock is
``clock.json``. ``--time rebased`` shifts every timestamp once, at start, by (real now - fixture now), so the
ages the fixtures describe hold at start-up and a backend using the real clock sees the same tiers.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "cao"
ISO_RE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|\+00:00)$")
MODES = {"ok", "down", "slow", "401", "badshape"}

PROFILES = [
    {"name": "developer", "description": "Generic developer profile", "provider": "mock_cli"},
    {"name": "reviewer", "description": "Generic reviewer profile", "provider": "mock_cli"},
]


def parse_iso(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class State:
    """What the fake serves, plus the call log. All access goes through ``lock``."""

    def __init__(self, day: str, shift: timedelta) -> None:
        self.lock = threading.Lock()
        self.day = day
        self.shift = shift
        base = FIXTURES / day
        self.sessions: list[dict[str, Any]] = self._load(base / "sessions.json")
        self.session_detail: dict[str, dict[str, Any]] = {}
        self.terminals: dict[str, dict[str, Any]] = {}
        self.inbox: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(base.glob("session.*.json")):
            detail = self._load(path)
            self.session_detail[detail["session"]["name"]] = detail
            for term in detail["terminals"]:
                full = copy.deepcopy(term)
                full.setdefault("session_name", detail["session"]["name"])
                full.setdefault("name", term.get("tmux_window"))
                full.setdefault("caller_id", None)
                self.terminals[term["id"]] = full
        for path in sorted(base.glob("terminal.*.json")):
            term = self._load(path)
            self.terminals[term["id"]] = term
        for path in sorted(base.glob("inbox.*.json")):
            self.inbox[path.stem.split(".", 1)[1]] = self._load(path)
        self.fixture_now = parse_iso(self._load(base / "clock.json")["now"])
        self.mode = "ok"
        self.slow_seconds = 12.0
        self.calls: list[dict[str, Any]] = []

    def _load(self, path: Path) -> Any:
        return self._shift(json.loads(path.read_text(encoding="utf-8")))

    def _shift(self, value: Any) -> Any:
        if not self.shift:
            return value
        if isinstance(value, dict):
            return {k: self._shift(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._shift(v) for v in value]
        if isinstance(value, str):
            m = ISO_RE.match(value)
            if m:
                moved = parse_iso(value) + self.shift
                frac = f".{moved.microsecond:06d}" if m.group(2) else ""
                return moved.strftime("%Y-%m-%dT%H:%M:%S") + frac + m.group(3)
        return value

    def now(self) -> datetime:
        return self.fixture_now + self.shift


class Handler(BaseHTTPRequestHandler):
    server: "FakeServer"
    protocol_version = "HTTP/1.1"

    def log_message(
        self, format: str, *args: Any
    ) -> None:  # noqa: A002 - signature fixed by the base class
        return

    # -- helpers --------------------------------------------------------------------------------
    def _send(self, status: int, payload: Any = None, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Any:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def _record(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> None:
        with self.server.state.lock:
            self.server.state.calls.append(
                {
                    "method": method,
                    "path": path,
                    "query": {k: v[0] for k, v in query.items()},
                    "body_keys": sorted(body) if isinstance(body, dict) else None,
                    "at": time.time(),
                }
            )

    def _gate(self, path: str) -> bool:
        """Apply the failure mode. Returns True when the request was already answered."""
        mode = self.server.state.mode
        if path.startswith("/__"):
            return False
        if mode == "down":
            self.close_connection = True
            self.connection.close()
            return True
        if mode == "401":
            self._send(401, {"detail": "Not authenticated"})
            return True
        if mode == "slow":
            time.sleep(self.server.state.slow_seconds)
        return False

    # -- verbs ----------------------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path.rstrip("/") or "/"
        if not path.startswith("/__"):
            self._record("GET", path, query, None)
        if self._gate(path):
            return
        state = self.server.state
        if path == "/__calls":
            with state.lock:
                self._send(200, {"calls": list(state.calls), "mode": state.mode})
            return
        if state.mode == "badshape" and (path == "/sessions" or path.startswith("/sessions/")):
            self._send(200, {"unexpected": True})
            return
        with state.lock:
            if path == "/health":
                self._send(
                    200, {"status": "ok", "service": "cli-agent-orchestrator", "version": "2.5.4"}
                )
            elif path == "/sessions":
                self._send(200, state.sessions)
            elif path == "/agents/profiles":
                self._send(200, PROFILES)
            elif re.fullmatch(r"/sessions/[A-Za-z0-9_-]+", path):
                name = path.rsplit("/", 1)[1]
                detail = state.session_detail.get(name)
                if detail:
                    self._send(200, detail)
                else:
                    self._send(404, {"detail": "Session not found"})
            elif re.fullmatch(r"/terminals/[a-f0-9]{8}", path):
                term = state.terminals.get(path.rsplit("/", 1)[1])
                if term:
                    self._send(200, term)
                else:
                    self._send(404, {"detail": "Terminal not found"})
            elif re.fullmatch(r"/terminals/[a-f0-9]{8}/inbox/messages", path):
                tid = path.split("/")[2]
                if tid not in state.terminals:
                    self._send(404, {"detail": "Terminal not found"})
                    return
                rows = list(state.inbox.get(tid, []))
                limit = int(query.get("limit", ["100"])[0])
                self._send(200, rows[: max(1, min(limit, 100))])
            elif re.fullmatch(r"/terminals/[a-f0-9]{8}/output", path):
                tid = path.split("/")[2]
                term = state.terminals.get(tid)
                if term is None:
                    self._send(404, {"detail": "Terminal not found"})
                    return
                text = "generic output line\n"
                if term.get("status") == "waiting_user_answer":
                    text += "APPROVAL_REQUIRED: push to origin?\n"
                self._send(200, {"output": text, "mode": query.get("mode", ["full"])[0]})
            else:
                self._send(404, {"detail": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        query = parse_qs(url.query)
        path = url.path.rstrip("/")
        body = self._body()
        if path == "/__mode":
            mode = (body or {}).get("mode") if isinstance(body, dict) else None
            if mode not in MODES:
                self._send(400, {"detail": f"mode must be one of {sorted(MODES)}"})
                return
            with self.server.state.lock:
                self.server.state.mode = mode
                if isinstance(body, dict) and "slow_seconds" in body:
                    self.server.state.slow_seconds = float(body["slow_seconds"])
            self._send(200, {"mode": mode})
            return
        if path == "/__calls/reset":
            with self.server.state.lock:
                self.server.state.calls.clear()
            self._send(200, {"ok": True})
            return
        self._record("POST", path, query, body)
        if self._gate(path):
            return
        state = self.server.state
        with state.lock:
            if path == "/sessions":
                name = (query.get("session_name") or ["new-session"])[0]
                name = name[4:] if name.startswith("cao-") else name  # CAO adds the prefix once
                tid = hashlib.sha1(name.encode()).hexdigest()[:8]
                term = {
                    "id": tid,
                    "tmux_session": f"cao-{name}",
                    "tmux_window": "conductor",
                    "provider": (query.get("provider") or ["mock_cli"])[0],
                    "agent_profile": (query.get("agent_profile") or ["developer"])[0],
                    "status": "idle",
                    "last_active": state.now().strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    "pending_inbox_count": 0,
                }
                state.sessions.append(
                    {
                        "id": f"cao-{name}",
                        "name": f"cao-{name}",
                        "status": "detached",
                        "working_directory": "/work/example",
                        "agent_profile": term["agent_profile"],
                    }
                )
                state.session_detail[f"cao-{name}"] = {
                    "session": {"id": f"cao-{name}", "name": f"cao-{name}", "status": "detached"},
                    "terminals": [term],
                }
                state.terminals[tid] = dict(
                    term, session_name=f"cao-{name}", name="conductor", caller_id=None
                )
                self._send(201, term)
            elif re.fullmatch(r"/terminals/[a-f0-9]{8}/input", path):
                self._send(200, {"success": True})
            elif re.fullmatch(r"/terminals/[a-f0-9]{8}/inbox/messages", path):
                tid = path.split("/")[2]
                receiver = state.terminals.get(tid)
                if receiver is None:
                    self._send(404, {"detail": "Terminal not found"})
                    return
                # like CAO: stored at once, delivered when the receiver is idle or done, else queued
                delivered = receiver.get("status") in ("idle", "completed")
                state.inbox.setdefault(tid, []).insert(
                    0,
                    {
                        "id": 9999,
                        "sender_id": (query.get("sender_id") or [""])[0],
                        "receiver_id": tid,
                        "message": (query.get("message") or [""])[0],
                        "status": "delivered" if delivered else "pending",
                        "created_at": state.now().strftime("%Y-%m-%dT%H:%M:%S.%f"),
                    },
                )
                self._send(200, {"success": True, "message_id": 9999})
            elif re.fullmatch(r"/sessions/[A-Za-z0-9_-]+/terminals", path):
                self._send(201, {"id": "0badcafe", "status": "idle"})
            else:
                self._send(404, {"detail": "Not Found"})

    def do_DELETE(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        self._record("DELETE", path, parse_qs(url.query), None)
        if self._gate(path):
            return
        state = self.server.state
        with state.lock:
            m = re.fullmatch(r"/sessions/([A-Za-z0-9_-]+)", path)
            if m:
                name = m.group(1)
                if name not in state.session_detail:
                    self._send(404, {"detail": "Session not found"})
                    return
                ids = [t["id"] for t in state.session_detail[name]["terminals"]]
                state.sessions = [s for s in state.sessions if s["name"] != name]
                del state.session_detail[name]
                for tid in ids:
                    state.terminals.pop(tid, None)
                self._send(200, {"success": True, "deleted": [name], "errors": []})
            elif re.fullmatch(r"/terminals/[a-f0-9]{8}", path):
                tid = path.rsplit("/", 1)[1]
                state.terminals.pop(tid, None)
                self._send(200, {"success": True})
            else:
                self._send(404, {"detail": "Not Found"})


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], state: State) -> None:
        self.state = state
        super().__init__(addr, Handler)


def make_server(day: str = "busy", port: int = 0, time_mode: str = "fixture") -> FakeServer:
    """Build (not start) a fake CAO bound to 127.0.0.1. Used by the tests and by ``main``."""
    base = FIXTURES / day
    if not base.is_dir():
        raise SystemExit(f"fake_cao: no fixture day {day!r} under {FIXTURES}")
    fixture_now = parse_iso(json.loads((base / "clock.json").read_text(encoding="utf-8"))["now"])
    shift = timedelta(0)
    if time_mode == "rebased":
        shift = datetime.now(timezone.utc) - fixture_now
    return FakeServer(("127.0.0.1", port), State(day, shift))


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--day", choices=["quiet", "busy"], default="busy")
    ap.add_argument("--port", type=int, default=0, help="0 picks a free port")
    ap.add_argument("--time", choices=["fixture", "rebased"], default="fixture")
    ap.add_argument(
        "--print-url",
        action="store_true",
        help="print one line 'FAKE_CAO_URL=http://...' when ready",
    )
    args = ap.parse_args()
    if args.port == 9889:
        raise SystemExit("fake_cao: refusing port 9889 (the owner's live CAO)")
    server = make_server(args.day, args.port, args.time)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"FAKE_CAO_URL={url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
