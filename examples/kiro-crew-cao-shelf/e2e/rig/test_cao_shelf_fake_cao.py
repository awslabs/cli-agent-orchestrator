"""Tests for the fake CAO used by the contract tests and the e2e journeys (stdlib + pytest)."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fake_cao  # noqa: E402


def call(
    base: str, method: str, path: str, body: Any = None, timeout: float = 5.0
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else None


@pytest.fixture()
def fake() -> Iterator[str]:
    server = fake_cao.make_server("busy", 0, "fixture")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def test_reads_replay_the_fixtures(fake: str) -> None:
    assert call(fake, "GET", "/health")[0] == 200
    status, rows = call(fake, "GET", "/sessions")
    assert status == 200 and len(rows) == 12
    status, detail = call(fake, "GET", "/sessions/cao-payments-fix")
    assert status == 200
    assert [t["status"] for t in detail["terminals"]] == [
        "processing",
        "processing",
        "waiting_user_answer",
        "idle",
    ]
    status, term = call(fake, "GET", "/terminals/b94da5c6")
    assert status == 200 and term["caller_id"] == "b2ce9908"
    status, conductor = call(fake, "GET", "/terminals/b2ce9908")
    assert status == 200 and conductor["caller_id"] is None
    status, rows = call(fake, "GET", "/terminals/b2ce9908/inbox/messages?limit=100&order=desc")
    assert status == 200 and rows and rows[0]["receiver_id"] == "b2ce9908"
    assert call(fake, "GET", "/terminals/b94da5c6/output?mode=full")[0] == 200
    assert call(fake, "GET", "/agents/profiles")[0] == 200
    assert call(fake, "GET", "/sessions/cao-nope")[0] == 404
    assert call(fake, "GET", "/terminals/ffffffff")[0] == 404


def test_an_inbox_message_is_stored_and_its_status_depends_on_the_receiver(fake: str) -> None:
    """The Ask journey reads the status back: delivered to an idle conductor, queued otherwise."""
    for tid, expected in (("fd37fab3", "delivered"), ("b2ce9908", "pending")):
        status, posted = call(
            fake, "POST", f"/terminals/{tid}/inbox/messages?sender_id=cao-shelf&message=hi"
        )
        assert status == 200 and posted["message_id"] == 9999
        _, rows = call(fake, "GET", f"/terminals/{tid}/inbox/messages?limit=20&order=desc")
        mine = [r for r in rows if r["id"] == 9999]
        assert mine and mine[0]["status"] == expected and mine[0]["message"] == "hi"
    assert call(fake, "POST", "/terminals/ffffffff/inbox/messages?sender_id=x&message=y")[0] == 404


def test_fixture_time_is_untouched_and_rebased_time_moves(fake: str) -> None:
    _, detail = call(fake, "GET", "/sessions/cao-payments-fix")
    assert detail["terminals"][0]["last_active"] == "2026-11-15T12:00:00.000000Z"
    server = fake_cao.make_server("busy", 0, "rebased")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        _, moved = call(base, "GET", "/sessions/cao-payments-fix")
        stamp = datetime.fromisoformat(moved["terminals"][0]["last_active"].replace("Z", "+00:00"))
        assert abs((datetime.now(timezone.utc) - stamp).total_seconds()) < 60
        assert moved["terminals"][0]["last_active"].endswith("Z")
        _, rows = call(base, "GET", "/terminals/b2ce9908/inbox/messages")
        assert rows[0]["created_at"].endswith("+00:00")
    finally:
        server.shutdown()
        server.server_close()


def test_write_routes_are_recorded_and_delete_removes_the_session(fake: str) -> None:
    call(fake, "POST", "/__calls/reset")
    assert (
        call(
            fake,
            "POST",
            "/terminals/b2ce9908/inbox/messages?sender_id=cao-shelf&message=hello",
        )[0]
        == 200
    )
    assert call(fake, "POST", "/terminals/b94da5c6/input?message=y")[0] == 200
    status, body = call(fake, "DELETE", "/sessions/cao-ci-flakes")
    assert status == 200 and body["deleted"] == ["cao-ci-flakes"]
    assert call(fake, "GET", "/sessions/cao-ci-flakes")[0] == 404
    assert len(call(fake, "GET", "/sessions")[1]) == 11
    _, log = call(fake, "GET", "/__calls")
    writes = [(c["method"], c["path"]) for c in log["calls"] if c["method"] != "GET"]
    assert writes == [
        ("POST", "/terminals/b2ce9908/inbox/messages"),
        ("POST", "/terminals/b94da5c6/input"),
        ("DELETE", "/sessions/cao-ci-flakes"),
    ]


def test_failure_modes(fake: str) -> None:
    assert call(fake, "POST", "/__mode", {"mode": "401"})[0] == 200
    assert call(fake, "GET", "/sessions")[0] == 401
    assert call(fake, "GET", "/__calls")[0] == 200  # control routes still answer
    call(fake, "POST", "/__mode", {"mode": "badshape"})
    status, body = call(fake, "GET", "/sessions")
    assert status == 200 and isinstance(body, dict)
    call(fake, "POST", "/__mode", {"mode": "down"})
    with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
        call(fake, "GET", "/sessions")
    call(fake, "POST", "/__mode", {"mode": "slow", "slow_seconds": 0.5})
    assert call(fake, "GET", "/sessions", timeout=5)[0] == 200
    call(fake, "POST", "/__mode", {"mode": "ok"})
    assert call(fake, "GET", "/sessions")[0] == 200
    assert call(fake, "POST", "/__mode", {"mode": "bogus"})[0] == 400


def test_refuses_the_owners_cao_port() -> None:
    with pytest.raises(SystemExit):
        sys.argv = ["fake_cao.py", "--port", "9889"]
        fake_cao.main()


def test_session_start_adds_the_cao_prefix_once(fake: str) -> None:
    for given in ("cao-new-one", "new-two"):
        status, term = call(
            fake,
            "POST",
            f"/sessions?agent_profile=developer&provider=mock_cli&session_name={given}",
        )
        assert status == 201 and term["tmux_session"] == f"cao-{given.removeprefix('cao-')}"
    names = [s["name"] for s in call(fake, "GET", "/sessions")[1]]
    assert "cao-new-one" in names and "cao-new-two" in names and "cao-cao-new-one" not in names
