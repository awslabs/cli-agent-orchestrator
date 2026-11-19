"""A2 and the read half of A6/A7: the CAO client, its failure mapping and its read-only promise."""

from __future__ import annotations

import http.server
import inspect
import re
import time
from typing import Any, Callable, Dict, List, Tuple

import cao_shelf_support as sup
import pytest
from cao_shelf import cao
from cao_shelf.cao import CaoClient, validate_cao_url
from cao_shelf.errors import ShelfError

BASE = "http://127.0.0.1:9889"


def client_for(day: str = "busy") -> Tuple[CaoClient, sup.FakeCao, sup.FakeOpener]:
    fake = sup.FakeCao(day)
    opener = sup.FakeOpener(fake)
    return CaoClient(BASE, opener), fake, opener


# ----------------------------------------------------------------------------- the URL rule
@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:9889", "http://127.0.0.1:9889"),
        ("http://127.0.0.1:9889/", "http://127.0.0.1:9889"),
        ("http://localhost:9889", "http://localhost:9889"),
        ("http://[::1]:9889", "http://[::1]:9889"),
        ("HTTP://LOCALHOST:1", "http://localhost:1"),
        ("http://127.0.0.1", "http://127.0.0.1"),
    ],
)
def test_loopback_urls_are_accepted(url: str, expected: str) -> None:
    assert validate_cao_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:9889",
        "http://10.0.0.5:9889",
        "http://example.com",
        "http://127.0.0.1.evil.com",
        "http://localhost.evil.com",
        "http://127.0.0.2:9889",
        "http://0.0.0.0:9889",
        "http://user:pw@127.0.0.1:9889",
        "http://127.0.0.1:9889/proxy/cao",
        "http://127.0.0.1:9889/?a=b",
        "http://127.0.0.1:9889#x",
        "http://127.0.0.1:99999",
        "ftp://127.0.0.1",
        "//127.0.0.1",
        "",
        None,
        7,
    ],
)
def test_other_urls_are_refused(url: Any) -> None:
    with pytest.raises(ShelfError) as exc:
        validate_cao_url(url)
    assert exc.value.code == "forbidden_host" and exc.value.http_status == 400


def test_localhost_resolving_elsewhere_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_info = [(2, 1, 6, "", ("93.184.216.34", 80))]
    monkeypatch.setattr(cao.socket, "getaddrinfo", lambda *a, **k: fake_info)
    with pytest.raises(ShelfError) as exc:
        validate_cao_url("http://localhost:9889")
    assert exc.value.code == "forbidden_host"


def test_localhost_that_does_not_resolve_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> Any:
        raise OSError("no")

    monkeypatch.setattr(cao.socket, "getaddrinfo", boom)
    with pytest.raises(ShelfError):
        validate_cao_url("http://localhost:9889")


def test_client_refuses_a_bad_url_at_construction() -> None:
    with pytest.raises(ShelfError):
        CaoClient("http://example.com")


# ----------------------------------------------------------------------------- read-only promise
WRITE_WORDS = (
    "post",
    "put",
    "delete",
    "patch",
    "send",
    "create",
    "launch",
    "start",
    "stop",
    "answer",
    "ask",
)


def test_client_has_no_write_methods() -> None:
    public = [
        n for n, _ in inspect.getmembers(CaoClient, inspect.isfunction) if not n.startswith("_")
    ]
    assert sorted(public) == [
        "get_output",
        "get_session",
        "get_terminal",
        "health",
        "inbox_messages",
        "list_profiles",
        "list_sessions",
    ]
    for name in public:
        assert not set(name.split("_")) & set(WRITE_WORDS), name


def test_client_cannot_send_a_write_even_through_the_private_door() -> None:
    client, fake, _ = client_for()
    for method in ("POST", "DELETE", "PUT", "PATCH"):
        with pytest.raises(ShelfError):
            client._request(method, "/sessions/cao-payments-fix")
    assert fake.calls == []
    assert CaoClient.ALLOWED_METHODS == frozenset({"GET"})


def test_cao_module_names_no_write_verb() -> None:
    text = (sup.BACKEND / "cao_shelf" / "cao.py").read_text()
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(("#", '"', "*")))
    for verb in ("POST", "DELETE", "PUT", "PATCH"):
        assert not re.search(rf'["\']{verb}["\']', code), verb


# ----------------------------------------------------------------------------- reads on the fake
def test_reads_return_the_fixture_data() -> None:
    client, fake, opener = client_for()
    names = [s["name"] for s in client.list_sessions()]
    assert len(names) == 12 and names[0] == "cao-payments-fix"
    session = client.get_session("cao-payments-fix")
    assert [t["tmux_window"] for t in session["terminals"]] == [
        "conductor",
        "coder-1",
        "tester",
        "reviewer",
    ]
    assert client.get_terminal("b94da5c6")["caller_id"] == "b2ce9908"
    assert client.health()["version"] == "2.5.4"
    rows = client.inbox_messages("b2ce9908", limit=100, order="desc")
    assert rows and rows[0]["created_at"] >= rows[-1]["created_at"]
    assert client.get_output("b94da5c6").endswith("push to origin?\n")
    assert all(m == "GET" for m, _, _ in fake.calls)
    assert set(opener.timeouts) == {cao.READ_TIMEOUT}


def test_cao_names_are_checked_and_bad_names_never_reach_cao() -> None:
    client, fake, _ = client_for()
    client.get_session("cao-payments-fix")
    assert fake.calls[-1][1] == "/sessions/cao-payments-fix"
    assert cao.cao_session_name("payments-fix") == "cao-payments-fix"
    assert cao.cao_session_name("cao-foo") == "cao-cao-foo"  # never collapsed onto "cao-foo"
    before = len(fake.calls)
    for bad in ("payments-fix", "../etc", "cao-", "a b", "x" * 80, "cao-../x", "a/b", "é", ""):
        with pytest.raises(ShelfError) as exc:
            client.get_session(bad)
        assert exc.value.code == "invalid_request"
    for bad_id in ("../etc/pass", "ABCDEF12", "abc", "abcdefg1", "abcdef123", "", "b2ce9908/x"):
        with pytest.raises(ShelfError):
            client.get_terminal(bad_id)
        with pytest.raises(ShelfError):
            client.inbox_messages(bad_id)
        with pytest.raises(ShelfError):
            client.get_output(bad_id)
    with pytest.raises(ShelfError):
        client.inbox_messages("b2ce9908", sender_id="not-an-id")
    with pytest.raises(ShelfError):
        client.inbox_messages("b2ce9908", limit=101)
    with pytest.raises(ShelfError):
        client.get_output("b2ce9908", mode="; rm")
    assert len(fake.calls) == before


def test_inbox_query_carries_limit_order_and_sender() -> None:
    client, fake, _ = client_for()
    client.inbox_messages("b2ce9908", limit=3, order="desc", sender_id="1b964846")
    _, path, query = fake.calls[-1]
    assert path == "/terminals/b2ce9908/inbox/messages"
    assert query == {"limit": "3", "order": "desc", "sender_id": "1b964846"}


def test_failures_map_to_contract_codes_in_process() -> None:
    client, fake, _ = client_for()
    fake.mode = "down"
    with pytest.raises(ShelfError) as down:
        client.list_sessions()
    assert (down.value.code, down.value.http_status, down.value.retryable) == (
        "cao_unreachable",
        502,
        True,
    )
    fake.mode = "slow"
    with pytest.raises(ShelfError) as slow:
        client.list_sessions()
    assert (slow.value.code, slow.value.http_status, slow.value.retryable) == (
        "cao_timeout",
        504,
        True,
    )
    fake.mode = "401"
    with pytest.raises(ShelfError) as auth:
        client.list_sessions()
    assert (auth.value.code, auth.value.http_status) == ("cao_auth_required", 502)
    fake.mode = "badshape"
    with pytest.raises(ShelfError) as shape:
        client.list_sessions()
    assert shape.value.code == "cao_incompatible"
    with pytest.raises(ShelfError) as shape2:
        client.get_session("cao-payments-fix")
    assert shape2.value.code == "cao_incompatible"
    fake.mode = "ok"
    with pytest.raises(ShelfError) as missing:
        client.get_session("cao-nope")
    assert missing.value.code == "not_found" and missing.value.http_status == 404


def test_error_messages_never_echo_cao_text() -> None:
    client, fake, _ = client_for()
    fake.mode = "401"
    with pytest.raises(ShelfError) as exc:
        client.list_sessions()
    assert "auth" not in exc.value.message.lower().replace("authentication", "")


# ----------------------------------------------------------------------------- a real socket
class Scripted:
    """A throwaway http.server whose answer a test chooses."""

    def __init__(self, respond: Callable[[http.server.BaseHTTPRequestHandler], None]) -> None:
        self.hits: List[str] = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits.append(self.path)
                respond(self)

            def log_message(self, *a: Any) -> None:
                return None

        self.served = sup.serve(H)
        self.url = f"http://127.0.0.1:{self.served.port}"

    def close(self) -> None:
        self.served.close()


def send(
    handler: http.server.BaseHTTPRequestHandler, status: int, body: bytes, **headers: str
) -> None:
    handler.send_response(status)
    for k, v in headers.items():
        handler.send_header(k.replace("_", "-"), v)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def test_real_socket_roundtrip_and_status_mapping() -> None:
    state = {"status": 200, "body": b"[]"}
    srv = Scripted(lambda h: send(h, state["status"], state["body"]))  # type: ignore[arg-type]
    try:
        client = CaoClient(srv.url)
        assert client.list_sessions() == []
        for status, code in [
            (401, "cao_auth_required"),
            (403, "cao_auth_required"),
            (404, "not_found"),
            (500, "cao_error"),
            (400, "cao_error"),
            (409, "cao_error"),
        ]:
            state["status"] = status
            with pytest.raises(ShelfError) as exc:
                client.list_sessions()
            assert exc.value.code == code, status
        state.update(status=200, body=b"not json at all")
        with pytest.raises(ShelfError) as exc2:
            client.list_sessions()
        assert exc2.value.code == "cao_incompatible"
        state.update(status=200, body=b'{"a": 1}')
        with pytest.raises(ShelfError) as exc3:
            client.list_sessions()
        assert exc3.value.code == "cao_incompatible"
    finally:
        srv.close()


def test_redirects_are_not_followed() -> None:
    target = Scripted(lambda h: send(h, 200, b"[]"))
    redirecting = Scripted(lambda h: send(h, 302, b"", Location=target.url + "/sessions"))
    try:
        with pytest.raises(ShelfError) as exc:
            CaoClient(redirecting.url).list_sessions()
        assert exc.value.code == "cao_error"
        assert target.hits == []
    finally:
        redirecting.close()
        target.close()


def test_oversized_response_is_refused() -> None:
    big = b"[" + b"1," * (cao.MAX_RESPONSE_BYTES // 2 + 10) + b"1]"
    srv = Scripted(lambda h: send(h, 200, big))
    try:
        with pytest.raises(ShelfError) as exc:
            CaoClient(srv.url).list_sessions()
        assert exc.value.code == "cao_error"
    finally:
        srv.close()


def test_slow_answer_times_out_with_the_read_timeout() -> None:
    def slow(h: http.server.BaseHTTPRequestHandler) -> None:
        time.sleep(1.2)
        try:
            send(h, 200, b"[]")
        except OSError:
            pass

    srv = Scripted(slow)
    try:
        client = CaoClient(srv.url)
        started = time.monotonic()
        with pytest.raises(ShelfError) as exc:
            client._request("GET", "/sessions", read_timeout=0.3)
        assert exc.value.code == "cao_timeout"
        assert time.monotonic() - started < 1.1
    finally:
        srv.close()


def test_refused_connection_is_unreachable() -> None:
    port = sup.find_port()
    with pytest.raises(ShelfError) as exc:
        CaoClient(f"http://127.0.0.1:{port}").list_sessions()
    assert exc.value.code == "cao_unreachable"


def test_requests_carry_no_origin_and_ignore_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: Dict[str, Any] = {}

    def record(h: http.server.BaseHTTPRequestHandler) -> None:
        seen["headers"] = dict(h.headers)
        send(h, 200, b"[]")

    srv = Scripted(record)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    try:
        CaoClient(srv.url).list_sessions()
    finally:
        srv.close()
    assert "Origin" not in seen["headers"] and "Cookie" not in seen["headers"]
    assert seen["headers"]["Host"].startswith("127.0.0.1")


def test_observer_sees_each_request_without_the_query() -> None:
    fake = sup.FakeCao("busy")
    seen: List[Tuple[str, str]] = []
    client = CaoClient(BASE, sup.FakeOpener(fake), observer=lambda m, p: seen.append((m, p)))
    client.inbox_messages("b2ce9908", sender_id="1b964846")
    assert seen == [("GET", "/terminals/b2ce9908/inbox/messages")]
