"""A4 and the endpoint contracts: the real server and a fake CAO, validated with ``schemas/``."""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any, Dict, Iterator

import cao_shelf_support as sup
import pytest
from cao_shelf import auth
from cao_shelf.http import App, make_server
from cao_shelf_support import SECRET, Stack, validate

ACK = {"acknowledge_risk": True}

ROUTES_NEEDING_SIGNATURE = [
    ("GET", "/api/shelf"),
    ("GET", "/api/status"),
    ("GET", "/api/config"),
    ("PUT", "/api/config"),
    ("GET", "/api/history"),
    ("GET", "/api/profiles"),
    ("GET", "/api/sessions/payments-fix/history"),
    ("GET", "/api/terminals/b94da5c6/prompt"),
    ("POST", "/api/sessions"),
    ("POST", "/api/sessions/payments-fix/ask"),
    ("POST", "/api/terminals/b94da5c6/answer"),
    ("POST", "/api/stop/plan"),
    ("POST", "/api/stop/execute"),
    ("GET", "/api/relays"),
    ("POST", "/api/relays"),
    ("POST", "/api/relays/rc-aaaaaaaaaa/end"),
    ("POST", "/api/relays/rc-aaaaaaaaaa/takeover"),
]


@pytest.fixture
def stack(tmp_path: Path) -> Iterator[Stack]:
    s = Stack(tmp_path / "data", "busy")
    try:
        yield s
    finally:
        s.close()


def strip(shelf: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(shelf)
    out.pop("revision", None)
    if "cao" in out:  # the fake CAO is not on 9889
        out["cao"] = dict(out["cao"], base_url="http://127.0.0.1:9889")
    return out


# --------------------------------------------------------------------------------------- health
def test_health_is_unsigned_and_never_calls_cao(stack: Stack) -> None:
    r = stack.get("/health", sign=False)
    assert r.status == 200 and r.json == {"status": "ok"}
    assert stack.cao.calls == []


def test_health_answers_without_a_secret_configured(tmp_path: Path) -> None:
    s = Stack(tmp_path / "d", secret="")
    try:
        assert s.get("/health", sign=False).status == 200
        assert s.get("/api/shelf").status == 401  # nothing is allowed without a secret
    finally:
        s.close()


# ------------------------------------------------------------------------------------ signatures
@pytest.mark.parametrize("method,path", ROUTES_NEEDING_SIGNATURE)
def test_every_route_but_health_needs_a_signature(stack: Stack, method: str, path: str) -> None:
    r = stack.call(method, path, {} if method != "GET" else None, sign=False)
    assert r.status == 401 and r.raw == b""
    assert stack.cao.calls == []


def test_bad_signatures_are_refused(stack: Stack) -> None:
    good = auth.signed_headers(SECRET, "GET", "/api/status", b"", now=stack.now)
    cases = {
        "wrong secret": auth.signed_headers("other", "GET", "/api/status", b"", now=stack.now),
        "other target": auth.signed_headers(SECRET, "GET", "/api/shelf", b"", now=stack.now),
        "other method": auth.signed_headers(SECRET, "POST", "/api/status", b"", now=stack.now),
        "stale": auth.signed_headers(SECRET, "GET", "/api/status", b"", now=stack.now - 120),
        "future": auth.signed_headers(SECRET, "GET", "/api/status", b"", now=stack.now + 120),
        "garbage": {auth.HEADER_V1: "nonsense"},
        "no colon": {auth.HEADER_V1: "123456"},
        "v2 mismatch": {**good, auth.HEADER_V2: good[auth.HEADER_V2][:-3] + "abc"},
        "v2 bad nonce": {**good, auth.HEADER_NONCE: "XYZ"},
        "v2 without nonce": {k: v for k, v in good.items() if k != auth.HEADER_NONCE},
    }
    for label, headers in cases.items():
        r = stack.call("GET", "/api/status", sign=False, headers=headers)
        assert r.status == 401, label
    assert stack.get("/api/status").status == 200


def test_body_is_bound_to_the_signature(stack: Stack) -> None:
    body = json.dumps({"scope": "stale"}).encode()
    headers = auth.signed_headers(SECRET, "POST", "/api/stop/plan", b"{}", now=stack.now)
    r = stack.call("POST", "/api/stop/plan", sign=False, raw=body, headers=headers)
    assert r.status == 401


def test_replayed_request_is_refused_and_v1_only_is_refused_too(stack: Stack) -> None:
    headers = auth.signed_headers(SECRET, "GET", "/api/status", b"", now=stack.now)
    assert stack.call("GET", "/api/status", sign=False, headers=headers).status == 200
    assert stack.call("GET", "/api/status", sign=False, headers=headers).status == 401
    v1 = auth.signed_headers(SECRET, "GET", "/api/status", b"", now=stack.now, v2=False)
    assert stack.call("GET", "/api/status", sign=False, headers=v1).status == 401
    stripped = {auth.HEADER_V1: headers[auth.HEADER_V1]}  # a captured request, v2 headers dropped
    assert stack.call("GET", "/api/status", sign=False, headers=stripped).status == 401


def test_query_is_part_of_the_signed_target(stack: Stack) -> None:
    headers = auth.signed_headers(SECRET, "GET", "/api/shelf?since=1", b"", now=stack.now)
    r = stack.call("GET", "/api/shelf?since=2", sign=False, headers=headers)
    assert r.status == 401


def test_nonce_cache_fails_closed_when_full() -> None:
    v = auth.ProxyVerifier(SECRET, max_entries=2, clock=lambda: 1000.0)
    for i in range(2):
        h = auth.signed_headers(SECRET, "GET", "/x", b"", now=1000, nonce=f"{i:032x}")
        assert v.verify("GET", "/x", b"", h)
    h = auth.signed_headers(SECRET, "GET", "/x", b"", now=1000, nonce=f"{9:032x}")
    assert not v.verify("GET", "/x", b"", h)


def test_nonce_cache_is_sized_for_the_poll_rate() -> None:
    assert auth.DEFAULT_NONCE_ENTRIES >= 60 * 50


def test_nonce_expires_with_the_window() -> None:
    clock = {"t": 1000.0}
    v = auth.ProxyVerifier(SECRET, max_entries=1, clock=lambda: clock["t"])
    h = auth.signed_headers(SECRET, "GET", "/x", b"", now=1000, nonce="a" * 32)
    assert v.verify("GET", "/x", b"", h)
    clock["t"] = 1100.0
    h2 = auth.signed_headers(SECRET, "GET", "/x", b"", now=1100, nonce="b" * 32)
    assert v.verify("GET", "/x", b"", h2)


# --------------------------------------------------------------------------------- shelf endpoint
@pytest.mark.parametrize("day", ["quiet", "busy"])
def test_shelf_matches_the_schema_and_the_expected_fixture(tmp_path: Path, day: str) -> None:
    s = Stack(tmp_path / "data", day)
    try:
        s.warm()
        r = s.get("/api/shelf")
        assert r.status == 200 and r.headers["Content-Type"].startswith("application/json")
        validate("shelf.schema.json", r.json)
        assert strip(r.json) == strip(sup.load(sup.FIXTURES / "expected" / f"{day}.shelf.json"))
    finally:
        s.close()


def test_since_the_current_revision_gives_the_stub(stack: Stack) -> None:
    stack.warm()
    rev = stack.get("/api/shelf").json["revision"]
    r = stack.get(f"/api/shelf?since={rev}")
    assert r.json == {"revision": rev, "unchanged": True}
    validate("shelf.schema.json", r.json, "unchanged")
    assert "sessions" in stack.get(f"/api/shelf?since={rev - 1}").json
    assert stack.get("/api/shelf?since=abc").status == 400


def test_shelf_from_cache_makes_no_cao_call_of_its_own(stack: Stack) -> None:
    stack.warm()
    n = len(stack.cao.calls)
    for _ in range(5):
        stack.get("/api/shelf")
    assert len(stack.cao.calls) == n


def test_cao_down_without_a_snapshot_is_200_and_not_an_empty_list(stack: Stack) -> None:
    stack.cao.mode = "down"
    stack.warm(1)
    r = stack.get("/api/shelf")
    assert r.status == 200
    validate("shelf.schema.json", r.json)
    assert (
        r.json["cao"]["reachable"] is False and r.json["cao"]["error"]["code"] == "cao_unreachable"
    )
    assert r.json["sessions"] == [] and r.json["history_total"] is None


def test_cao_down_after_a_snapshot_keeps_the_cards(stack: Stack) -> None:
    stack.warm()
    stack.cao.mode = "down"
    stack.now += 30
    stack.warm(1)
    r = stack.get("/api/shelf")
    validate("shelf.schema.json", r.json)
    assert r.json["stale_snapshot"] is True and r.json["snapshot_age_seconds"] >= 0
    assert len(r.json["sessions"]) == 12


@pytest.mark.parametrize(
    "mode,code", [("401", "cao_auth_required"), ("badshape", "cao_incompatible")]
)
def test_cao_failures_surface_in_the_shelf(stack: Stack, mode: str, code: str) -> None:
    stack.cao.mode = mode
    stack.warm(1)
    r = stack.get("/api/shelf")
    validate("shelf.schema.json", r.json)
    assert r.json["cao"]["error"]["code"] == code


def test_slow_cao_is_a_timeout(stack: Stack) -> None:
    stack.cao.mode = "slow"
    from cao_shelf import cao as cao_mod

    real = cao_mod.CaoClient._request

    def quick(self: Any, method: str, path: str, query: Any = None, **kw: Any) -> Any:
        kw["read_timeout"] = 0.3
        return real(self, method, path, query, **kw)

    cao_mod.CaoClient._request = quick  # type: ignore[method-assign]
    try:
        stack.warm(1)
    finally:
        cao_mod.CaoClient._request = real  # type: ignore[method-assign]
    assert stack.get("/api/shelf").json["cao"]["error"]["code"] == "cao_timeout"


def test_threaded_poller_answers_the_first_request_with_data(tmp_path: Path) -> None:
    s = Stack(tmp_path / "data", "quiet", thread=True)
    try:
        r = s.get("/api/shelf")
        assert r.status == 200 and len(r.json["sessions"]) == 12
        assert s.app.poller.running()
    finally:
        s.close()


def test_status_endpoint(stack: Stack) -> None:
    stack.warm(2)
    r = stack.get("/api/status")
    assert r.status == 200
    body = r.json
    assert body["version"] == "0.2.0" and body["cao_error"] is None
    assert set(body["poller"]) == {
        "running",
        "interval_seconds",
        "last_tick_at",
        "requests_last_minute",
    }
    assert body["poller"]["requests_last_minute"] > 0
    validate("actions.schema.json", body, "StatusResponse")


# -------------------------------------------------------------------------------------- config
def test_config_roundtrip_and_permissions(stack: Stack, tmp_path: Path) -> None:
    cfg = stack.get("/api/config").json
    validate("actions.schema.json", cfg, "Config")
    assert cfg["poll_seconds"] == 5 and cfg["stale_hours"] == 48 and cfg["persist_history"] is True
    new = dict(cfg, poll_seconds=10, stale_hours=24.0, warm_hours=2.0, persist_history=False)
    r = stack.call("PUT", "/api/config", new)
    assert r.status == 200
    validate("actions.schema.json", r.json, "Config")
    assert r.json["poll_seconds"] == 10 and r.json["persist_history"] is False
    path = tmp_path / "data" / "config.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    again = App(tmp_path / "data", SECRET, environ={})
    assert again.config.get()["stale_hours"] == 24.0
    patch = stack.call("PUT", "/api/config", {"poll_seconds": 7})  # a partial body is accepted
    validate("actions.schema.json", {"poll_seconds": 7}, "ConfigPatch")
    assert patch.json["poll_seconds"] == 7
    bad = stack.call("PUT", "/api/config", {"warm_hours": 30.0 - 6, "stale_hours": 20.0})
    assert bad.status == 400 and bad.json["error"]["code"] == "invalid_request"  # warm >= stale


@pytest.mark.parametrize(
    "bad,code",
    [
        ({"cao_url": "http://example.com"}, "forbidden_host"),
        ({"cao_url": "https://127.0.0.1:9889"}, "forbidden_host"),
        ({"cao_url": "http://127.0.0.1:9889/proxy"}, "forbidden_host"),
        ({"cao_url": 7}, "invalid_request"),
        ({"poll_seconds": 2}, "invalid_request"),
        ({"poll_seconds": 61}, "invalid_request"),
        ({"poll_seconds": 5.5}, "invalid_request"),
        ({"poll_seconds": True}, "invalid_request"),
        ({"warm_hours": 0.1}, "invalid_request"),
        ({"stale_hours": 1}, "invalid_request"),
        ({"stale_hours": 721}, "invalid_request"),
        ({"warm_hours": 24, "stale_hours": 2}, "invalid_request"),
        ({"persist_history": "yes"}, "invalid_request"),
        ({"extra": 1}, "invalid_request"),
    ],
)
def test_config_rejects_bad_values_and_saves_nothing(
    stack: Stack, tmp_path: Path, bad: Dict[str, Any], code: str
) -> None:
    before = stack.get("/api/config").json
    r = stack.call("PUT", "/api/config", bad)
    assert r.status == 400 and r.json["error"]["code"] == code
    validate("errors.schema.json", r.json)
    assert stack.get("/api/config").json == before
    assert not (tmp_path / "data" / "config.json").exists()


def test_config_change_takes_effect_on_the_poller(stack: Stack) -> None:
    stack.warm()
    stack.call("PUT", "/api/config", {"stale_hours": 2.0, "warm_hours": 0.25})
    stack.app.poller.tick()
    assert stack.get("/api/shelf").json["thresholds"] == {"warm_hours": 0.25, "stale_hours": 2.0}


# ---------------------------------------------------------------------------------- the envelope
def test_errors_use_the_envelope_and_never_echo_bodies(stack: Stack) -> None:
    attempts = [
        ("POST", "/api/sessions/payments-fix/ask", {"message": sup.SENTINEL}),
        (
            "POST",
            "/api/sessions/payments-fix/ask",
            {"message": sup.SENTINEL, "conductor_id": "zz", "x": 1},
        ),
        (
            "POST",
            "/api/terminals/b94da5c6/answer",
            {"answer": sup.SENTINEL, "expect_status": "idle"},
        ),
        ("POST", "/api/stop/execute", {"all": sup.SENTINEL}),
        ("PUT", "/api/config", {sup.SENTINEL: 1}),
        ("POST", "/api/sessions", {"agent_profile": sup.SENTINEL}),
    ]
    for method, path, body in attempts:
        r = stack.call(method, path, body)
        assert 400 <= r.status < 500, (path, r.status)
        validate("errors.schema.json", r.json)
        assert sup.SENTINEL not in r.raw.decode(), path


def test_malformed_and_oversized_bodies(stack: Stack) -> None:
    assert stack.call("POST", "/api/stop/plan", raw=b"{not json").status == 400
    assert stack.call("POST", "/api/stop/plan", raw=b"[1,2]").status == 400
    assert stack.call("POST", "/api/stop/plan", raw=b"\xff\xfe").status == 400
    big = stack.call("POST", "/api/sessions/payments-fix/ask", raw=b" " * (64 * 1024 + 1))
    assert big.status == 413 and big.json["error"]["code"] == "invalid_request"
    validate("errors.schema.json", big.json)
    ok = stack.call(
        "POST", "/api/stop/plan", raw=json.dumps({"scope": "stale", "pad": ""}).encode()
    )
    assert ok.status == 400  # unknown property


def test_unknown_routes_methods_and_bad_names(stack: Stack) -> None:
    r = stack.get("/api/nope")
    assert r.status == 404 and r.json["error"]["code"] == "not_found"
    r = stack.call("DELETE", "/api/shelf")
    assert r.status == 405
    validate("errors.schema.json", r.json)
    assert stack.call("POST", "/api/shelf", {}).status == 405
    assert stack.get("/api/sessions/bad%20name/history").status == 400
    assert stack.get("/api/sessions/../etc/history").status == 404
    assert stack.get("/api/terminals/XYZ/prompt").status == 400
    assert stack.get("/api/sessions/payments-fix/history/nothex00/rows").status == 400
    assert stack.cao.calls == []


def test_internal_errors_are_generic(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError(sup.SENTINEL)

    monkeypatch.setattr(stack.app.poller, "shelf", boom)
    r = stack.get("/api/shelf")
    assert r.status == 500 and r.json["error"]["code"] == "internal"
    assert sup.SENTINEL not in r.raw.decode()


# ----------------------------------------------------------------------------------------- history
def test_history_endpoints_match_the_expected_files(stack: Stack) -> None:
    stack.warm()
    for name in ("payments-fix", "migrate-db", "infra-audit"):
        r = stack.get(f"/api/sessions/{name}/history")
        assert r.status == 200, name
        validate("history.schema.json", r.json)
        assert r.json == sup.load(sup.FIXTURES / "expected" / "busy" / f"history.{name}.json")


def test_history_for_an_unknown_session(stack: Stack) -> None:
    stack.warm()
    r = stack.get("/api/sessions/no-such/history")
    assert r.status == 404 and r.json["error"]["code"] == "not_found"


def test_history_rows_endpoint(stack: Stack) -> None:
    stack.warm()
    closed = stack.get("/api/sessions/payments-fix/history").json["closed"]
    tid = closed[0]["terminal_id"]
    r = stack.get(f"/api/sessions/payments-fix/history/{tid}/rows")
    assert r.status == 200 and 1 <= len(r.json["rows"]) <= 3
    assert set(r.json["rows"][0]) == {"id", "created_at", "status", "text"}
    validate("history.schema.json", r.json, "HistoryRows")
    assert (
        stack.get("/api/sessions/payments-fix/history/1b964846/rows").status == 404
    )  # live helper


def test_all_history(stack: Stack) -> None:
    stack.warm()
    r = stack.get("/api/history")
    assert r.status == 200 and r.json["complete"] is True and len(r.json["closed"]) == 20
    assert set(r.json["closed"][0]) == {"session", "terminal_id", "rows", "last_at"}
    validate("history.schema.json", r.json, "AllHistory")
    assert r.json == sup.load(sup.FIXTURES / "expected" / "busy" / "all.history.json")


# ----------------------------------------------------------------------------------- start / ask
def test_profiles(stack: Stack) -> None:
    r = stack.get("/api/profiles")
    assert r.status == 200
    assert [p["name"] for p in r.json["profiles"]] == ["developer", "supervisor"]
    assert set(r.json["profiles"][0]) == {"name", "description", "provider"}
    validate("actions.schema.json", r.json, "Profiles")
    stack.get("/api/profiles")
    assert stack.cao.count("GET", "/agents/profiles") == 1  # cached 60 s


def test_start_session(stack: Stack) -> None:
    body = {
        "idempotency_key": "contract-test-key-0001",
        "name": "fresh-start",
        "agent_profile": "developer",
        "provider": "mock_cli",
        "working_directory": "/work/example",
        "message": sup.SENTINEL,
    }
    r = stack.post("/api/sessions", body)
    assert r.status == 201
    validate("actions.schema.json", r.json, "StartResponse")
    assert r.json["session"] == "fresh-start"
    call = [c for c in stack.cao.calls if c[0] == "POST"][0]
    assert call[1] == "/sessions"
    assert call[2]["session_name"] == "cao-fresh-start" and call[2]["agent_profile"] == "developer"
    assert (
        call[2]["provider"] == "mock_cli" and call[2]["idempotency_key"] == body["idempotency_key"]
    )
    assert sup.SENTINEL not in json.dumps(call[2])  # the first message travels in the JSON body


def test_start_validates_input(stack: Stack) -> None:
    for bad in [
        {"agent_profile": "developer"},
        {"working_directory": "/w"},
        {"agent_profile": "", "working_directory": "/w"},
        {"agent_profile": "d", "working_directory": ""},
        {"agent_profile": "d", "working_directory": "/w", "name": "bad name"},
        {"agent_profile": "d", "working_directory": "/w", "name": "-lead"},
        {"agent_profile": "d", "working_directory": "/w", "name": "x" * 60},
        {"agent_profile": "d", "working_directory": "/w", "extra": 1},
        {"agent_profile": "d", "working_directory": "/w\x00x"},
        {"agent_profile": "d", "working_directory": "/w", "message": "m" * 8001},
    ]:
        assert (
            stack.post("/api/sessions", {"idempotency_key": "contract-test-key-0003", **bad}).status
            == 400
        ), bad
    for no_key in [{}, {"idempotency_key": "short"}, {"idempotency_key": "has space in it !!"}]:
        valid = {"agent_profile": "d", "working_directory": "/w"}
        assert stack.post("/api/sessions", {**valid, **no_key}).status == 400, no_key
    assert stack.cao.writes() == []


def test_start_failure_shows_the_cao_reason_code_only(stack: Stack) -> None:
    body = {
        "idempotency_key": "contract-test-key-0002",
        "name": "payments-fix",
        "agent_profile": "developer",
        "working_directory": "/w",
    }
    r = stack.post("/api/sessions", body)  # CAO answers 409: it exists
    assert r.status == 502 and r.json["error"]["code"] == "cao_error"
    assert r.json["error"]["detail"] == {"cao_status": 409}  # the code, not CAO's own text
    assert "name is not already used" in r.json["error"]["message"]
    validate("errors.schema.json", r.json)


def test_ask_conductor(stack: Stack) -> None:
    stack.warm()
    r = stack.post(
        "/api/sessions/payments-fix/ask", {"message": "status please", "conductor_id": "b2ce9908"}
    )
    assert r.status == 200
    validate("actions.schema.json", r.json, "AskResponse")
    call = [c for c in stack.cao.calls if c[0] == "POST"][0]
    assert call[1] == "/terminals/b2ce9908/inbox/messages"
    assert call[2] == {"sender_id": "cao-shelf", "message": "status please"}


def test_ask_conflict_when_the_conductor_changed(stack: Stack) -> None:
    stack.warm()
    r = stack.post("/api/sessions/payments-fix/ask", {"message": "hi", "conductor_id": "1b964846"})
    assert r.status == 409 and r.json["error"]["code"] == "conflict_changed"
    assert stack.cao.writes() == []
    missing = stack.post("/api/sessions/no-such/ask", {"message": "hi", "conductor_id": "b2ce9908"})
    assert missing.status == 404


def test_ask_validation(stack: Stack) -> None:
    for bad in [
        {"message": "", "conductor_id": "b2ce9908"},
        {"message": "x"},
        {"conductor_id": "b2ce9908"},
        {"message": "x", "conductor_id": "B2CE9908"},
        {"message": "x" * 8001, "conductor_id": "b2ce9908"},
        {"message": "x\x00", "conductor_id": "b2ce9908"},
    ]:
        assert stack.post("/api/sessions/payments-fix/ask", bad).status == 400, bad
    assert stack.cao.writes() == []


# -------------------------------------------------------------------------------- logs and files
def test_logs_carry_no_bodies_or_names(stack: Stack, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    stack.warm()
    stack.get("/api/shelf")
    stack.post(
        "/api/sessions/payments-fix/ask", {"message": sup.SENTINEL, "conductor_id": "b2ce9908"}
    )
    stack.post(
        "/api/sessions",
        {
            "agent_profile": "developer",
            "working_directory": "/work/SECRETDIR",
            "message": sup.SENTINEL,
        },
    )
    stack.post(
        "/api/terminals/b94da5c6/answer",
        {"answer": sup.SENTINEL, "expect_status": "waiting_user_answer"},
    )
    stack.post("/api/stop/plan", {"scope": "named", "sessions": ["migrate-db"]})
    stack.post("/api/sessions/payments-fix/ask", {"message": sup.SENTINEL})
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert sup.SENTINEL not in text and "SECRETDIR" not in text
    assert "payments-fix" not in text and "migrate-db" not in text
    assert "/api/shelf" in text


def test_server_refuses_to_bind_off_loopback(tmp_path: Path) -> None:
    app = App(tmp_path / "d", SECRET, environ={})
    for host in ("0.0.0.0", "192.168.1.5", "::", "example.com", ""):
        with pytest.raises(ValueError):
            make_server(app, host, 9991)


def test_the_only_files_written_are_under_data(stack: Stack, tmp_path: Path) -> None:
    stack.warm()
    stack.call("PUT", "/api/config", {"poll_seconds": 6})
    written = sorted(p.name for p in (tmp_path / "data").iterdir())
    assert written == ["config.json", "ledger.json"]


# ---------------------------------------------------------------------------------- relays
@pytest.fixture
def relay_stack(tmp_path: Path) -> Iterator[Any]:
    gw = sup.FakeGateway()
    gateway = sup.GatewayClient(gw.environ(), opener=sup.FakeGatewayOpener(gw))
    s = Stack(tmp_path / "data", "busy", gateway=gateway)
    s.gw = gw  # type: ignore[attr-defined]
    try:
        s.warm()
        yield s
    finally:
        s.close()


def test_relay_routes_attach_list_shelf_end_and_takeover(relay_stack: Any) -> None:
    s = relay_stack
    r = s.post("/api/relays", {"session": "payments-fix", "label": "from phone", **ACK})
    assert r.status == 201, r.json
    validate("relays.schema.json", r.json, "RelaySummary")
    rid = r.json["relay_id"]
    assert r.json["slot_key"] in s.gw.slots and r.json["state"] == "active"
    listed = s.get("/api/relays")
    assert listed.status == 200
    validate("relays.schema.json", listed.json, "RelayList")
    assert [x["relay_id"] for x in listed.json["relays"]] == [rid]
    shelf = s.get("/api/shelf").json
    validate("shelf.schema.json", shelf)
    card = next(x for x in shelf["sessions"] if x["name"] == "payments-fix")
    assert card["relay"]["relay_id"] == rid and card["relay"]["label"] == "from phone"
    assert all(x["relay"] is None for x in shelf["sessions"] if x["name"] != "payments-fix")
    again = s.post("/api/relays", {"session": "payments-fix", **ACK})
    assert again.status == 409 and again.json["error"]["code"] == "relay_exists"
    validate("errors.schema.json", again.json)
    taken = s.post(f"/api/relays/{rid}/takeover", ACK)
    assert taken.status == 201
    validate("relays.schema.json", taken.json, "RelaySummary")
    ended = s.post(f"/api/relays/{taken.json['relay_id']}/end", {"delete_chat": True})
    assert ended.status == 200
    validate("relays.schema.json", ended.json, "RelayEndResponse")
    assert ended.json["relay"]["state"] == "ended" and ended.json["chat"] == "deleted"
    shelf = s.get("/api/shelf").json
    assert next(x for x in shelf["sessions"] if x["name"] == "payments-fix")["relay"] is None


def test_a_relay_change_bumps_the_shelf_revision(relay_stack: Any) -> None:
    s = relay_stack
    rev = s.get("/api/shelf").json["revision"]
    assert s.get(f"/api/shelf?since={rev}").json == {"revision": rev, "unchanged": True}
    s.post("/api/relays", {"session": "payments-fix", **ACK})
    after = s.get(f"/api/shelf?since={rev}").json
    assert after["revision"] > rev and "sessions" in after


def test_start_and_attach_route_validates_against_the_schema(relay_stack: Any) -> None:
    s = relay_stack
    body = {
        "start": {
            "idempotency_key": "relay-start-key-0001",
            "name": "via-relay",
            "agent_profile": "developer",
            "working_directory": "/w",
        },
        **ACK,
    }
    validate("relays.schema.json", body, "RelayCreateRequest")
    r = s.post("/api/relays", body)
    assert r.status == 201, r.json
    assert r.json["origin"] == "started_by_relay" and r.json["session"] == "via-relay"
    assert s.post("/api/relays", body).json["relay_id"] == r.json["relay_id"]


def test_relay_errors_use_the_envelope(relay_stack: Any) -> None:
    s = relay_stack
    for body, code, status in [
        ({"session": "no-such", **ACK}, "not_found", 404),
        ({"session": "payments-fix", "bogus": 1, **ACK}, "invalid_request", 400),
        ({}, "invalid_request", 400),
        ({"session": "payments-fix"}, "risk_not_acknowledged", 400),
        ({"session": "payments-fix", "acknowledge_risk": False}, "risk_not_acknowledged", 400),
    ]:
        r = s.post("/api/relays", body)
        assert (r.status, r.json["error"]["code"]) == (status, code), r.json
        validate("errors.schema.json", r.json)
    for path in ("/api/relays/rc-aaaaaaaaaa/end", "/api/relays/rc-aaaaaaaaaa/takeover"):
        r = s.post(path, {})
        assert r.status == 404 and r.json["error"]["code"] == "not_found"
    r = s.post("/api/relays/not-an-id/end", {})
    assert r.status == 400 and r.json["error"]["code"] == "invalid_request"


def test_relays_without_a_gateway_are_gateway_unavailable(stack: Stack) -> None:
    stack.warm()
    r = stack.post("/api/relays", {"session": "payments-fix", **ACK})
    assert r.status == 503 and r.json["error"]["code"] == "gateway_unavailable"
    assert r.json["error"]["retryable"] is True
    validate("errors.schema.json", r.json)
    assert stack.get("/api/relays").json == {"relays": []}


def test_there_is_no_approver_setting(stack: Stack) -> None:
    assert "approve_relay_tools" not in stack.get("/api/config").json
    bad = stack.call("PUT", "/api/config", {"approve_relay_tools": True})
    assert bad.status == 400 and bad.json["error"]["code"] == "invalid_request"
