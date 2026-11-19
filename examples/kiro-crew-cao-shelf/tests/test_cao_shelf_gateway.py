"""``cao_shelf.gateway``: the Kiro Crew chat calls, against a fake gateway."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Dict

import pytest
from cao_shelf.errors import ShelfError
from cao_shelf.gateway import GatewayClient
from cao_shelf_support import (
    SECRET,
    FakeGateway,
    FakeGatewayOpener,
    serve_fake_gateway,
)


def client(gw: FakeGateway, env: Dict[str, str] | None = None) -> GatewayClient:
    return GatewayClient(env if env is not None else gw.environ(), opener=FakeGatewayOpener(gw))


def test_create_slot_names_the_agent_as_a_template_and_returns_the_key() -> None:
    gw = FakeGateway()
    key = client(gw).create_slot("cao-shelf-rc-aaaaaaaaaa", "Remote control: payments-fix")
    assert key == "cao-shelf-rc-aaaaaaaaaa"
    slot = gw.slots[key]
    assert slot["agent"] == "cao-remote-control" and slot["agent_kind"] == "template"
    assert slot["title"] == "Remote control: payments-fix"


def test_app_token_is_fetched_once_and_sent_as_the_port_cookie_with_the_origin() -> None:
    gw = FakeGateway()
    c = client(gw)
    key = c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    c.set_mode(key, "normal")
    c.post_message(key, "hello")
    assert gw.count("POST", "/api/apps/cao-shelf/token") == 1
    token_call = gw.headers[0]
    assert token_call["x-app-secret"] == SECRET
    for headers in gw.headers[1:]:
        assert headers["cookie"] == "mc_token_5499=tok-1"
        assert headers["origin"] == gw.origin


def test_a_401_drops_the_token_and_retries_once_with_a_new_one() -> None:
    gw = FakeGateway()
    c = client(gw)
    key = c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    gw.tokens.append("rotated")  # the cached token is now stale: the next call gets 401
    c.set_mode(key, "normal")
    assert gw.slots[key]["mode"] == "normal"
    assert gw.count("POST", "/api/apps/cao-shelf/token") == 2


def test_set_mode_post_message_rows_and_delete() -> None:
    gw = FakeGateway()
    c = client(gw)
    key = c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    c.set_mode(key, "normal")
    c.post_message(key, "setup text")
    assert gw.slots[key]["mode"] == "normal"
    assert gw.messages(key) == ["setup text"]
    rows = c.slot_rows(key)
    assert rows and rows[0]["content"] == "setup text"
    assert c.delete_slot(key) is True
    assert c.delete_slot(key) is False  # already gone is not an error
    with pytest.raises(ShelfError) as err:
        c.slot_rows(key)
    assert err.value.code == "not_found"


def test_post_message_uses_the_receipt_form_of_the_chat_route() -> None:
    gw = FakeGateway()
    opener = FakeGatewayOpener(gw)
    c = GatewayClient(gw.environ(), opener=opener)
    key = c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    c.post_message(key, "x")
    assert any(u.endswith("/api/chat?ws=1") for u in opener.urls)


def test_only_the_normal_trust_mode_can_be_set() -> None:
    gw = FakeGateway()
    c = client(gw)
    key = c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    for mode in ("trust", "trust_reads", "yolo"):
        with pytest.raises(ShelfError):
            c.set_mode(key, mode)
    assert gw.count("POST", "/api/chat/mode") == 0


def test_a_refusal_is_gateway_refused_with_the_status_and_code_but_no_body_text() -> None:
    gw = FakeGateway()
    c = client(gw)
    gw.fail[("POST", "/api/chat/slots")] = 409
    with pytest.raises(ShelfError) as err:
        c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    assert err.value.code == "gateway_refused"
    assert err.value.detail == {"gateway_status": 409, "gateway_code": "test_refusal"}
    assert "test_refusal" not in err.value.message


def test_gateway_down_is_gateway_unavailable_and_retryable() -> None:
    gw = FakeGateway()
    opener = FakeGatewayOpener(gw)
    opener.down = True
    with pytest.raises(ShelfError) as err:
        GatewayClient(gw.environ(), opener=opener).create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    assert err.value.code == "gateway_unavailable" and err.value.retryable


@pytest.mark.parametrize(
    "origin",
    [
        "http://10.0.0.5:5499",
        "http://example.com:5499",
        "https://127.0.0.1:5499",
        "http://127.0.0.1",
        "http://127.0.0.1:5499/prefix",
        "http://user:pw@127.0.0.1:5499",
        "",
    ],
)
def test_a_non_loopback_or_odd_origin_is_refused_before_any_call(origin: str) -> None:
    gw = FakeGateway()
    env = dict(gw.environ(), KIROCREW_GATEWAY_ORIGIN=origin)
    opener = FakeGatewayOpener(gw)
    c = GatewayClient(env, opener=opener)
    assert c.available() is False
    with pytest.raises(ShelfError) as err:
        c.create_slot("cao-shelf-rc-aaaaaaaaaa", "t")
    assert err.value.code == "gateway_unavailable"
    assert opener.urls == []


def test_ipv6_loopback_origin_is_accepted() -> None:
    env = {"KIROCREW_GATEWAY_ORIGIN": "http://[::1]:5499", "KIROCREW_PROXY_SECRET": SECRET}
    assert GatewayClient(env, opener=object()).available() is True


def test_missing_secret_is_unavailable() -> None:
    gw = FakeGateway()
    env = dict(gw.environ())
    del env["KIROCREW_PROXY_SECRET"]
    assert GatewayClient(env, opener=FakeGatewayOpener(gw)).available() is False


def test_a_wrong_origin_proof_is_refused_and_a_right_one_accepted() -> None:
    gw = FakeGateway()
    good = hmac.new(SECRET.encode(), gw.origin.encode(), hashlib.sha256).hexdigest()
    ok = dict(gw.environ(), KIROCREW_GATEWAY_ORIGIN_PROOF=good)
    bad = dict(gw.environ(), KIROCREW_GATEWAY_ORIGIN_PROOF="0" * 64)
    assert GatewayClient(ok, opener=FakeGatewayOpener(gw)).available() is True
    assert GatewayClient(bad, opener=FakeGatewayOpener(gw)).available() is False


@pytest.mark.parametrize("slot", ["../x", "a/b", "", "x" * 200, "a b"])
def test_a_bad_slot_key_never_reaches_a_url(slot: str) -> None:
    gw = FakeGateway()
    opener = FakeGatewayOpener(gw)
    c = GatewayClient(gw.environ(), opener=opener)
    with pytest.raises(ShelfError) as err:
        c.slot_rows(slot)
    assert err.value.code == "invalid_request"
    assert opener.urls == []


def test_secret_token_and_bodies_never_reach_the_log(caplog: pytest.LogCaptureFixture) -> None:
    gw = FakeGateway()
    served = serve_fake_gateway(gw)
    try:
        caplog.set_level(logging.DEBUG)
        c = GatewayClient(gw.environ())  # the real loopback opener, over a socket
        key = c.create_slot("cao-shelf-rc-aaaaaaaaaa", "Remote control: SENTINEL-TITLE")
        c.set_mode(key, "normal")
        c.post_message(key, "sentinel-body")
        c.slot_rows(key)
        gw.fail[("POST", "/api/chat")] = 500
        with pytest.raises(ShelfError):
            c.post_message(key, "sentinel-body")
    finally:
        served.close()
    text = caplog.text
    assert "gateway POST /api/chat/slots 200" in text
    for secret in (SECRET, "tok-1", "sentinel-body", "SENTINEL-TITLE", key):
        assert secret not in text, secret
