"""Regression tests for the two independent reviews (F1 to F15, L1 to L4, the ledger address).

Each test failed before its fix. The comment above a group names the finding.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import cao_shelf_support as sup
import pytest
from cao_shelf import auth, history, prompts, tiering
from cao_shelf.errors import ShelfError
from cao_shelf.ledger import Ledger
from cao_shelf_support import ActionsRig, validate

NOW = sup.clock_of("busy")
TH = {"warm_hours": 1.0, "stale_hours": 48.0}
CAO_META = {
    "reachable": True,
    "base_url": "http://127.0.0.1:9889",
    "version": "2.5.4",
    "terminal_backend": "tmux",
    "error": None,
}


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def term(tid: str, status: str = "idle", age: Any = 5.0) -> Dict[str, Any]:
    return {
        "id": tid,
        "tmux_window": "w",
        "agent_profile": "p",
        "provider": "mock_cli",
        "status": status,
        "last_active": iso(age) if isinstance(age, (int, float)) else age,
        "pending_inbox_count": 0,
    }


def shelf_of(view: Dict[str, Any], callers: Any = None, th: Any = None) -> Dict[str, Any]:
    return tiering.build_shelf(view, callers or {}, NOW, th or TH, {}, cao=CAO_META)


def session(shelf: Dict[str, Any], name: str) -> Dict[str, Any]:
    return next(s for s in shelf["sessions"] if s["name"] == name)


def err(fn: Any, *a: Any) -> ShelfError:
    with pytest.raises(ShelfError) as exc:
        fn(*a)
    return exc.value


# ------------------------------------------------ F1: the age of a stale snapshot is not frozen
def test_f1_last_good_at_holds_while_cao_is_down_and_the_stub_carries_no_age(
    tmp_path: Path,
) -> None:
    r = ActionsRig(tmp_path, "busy").warm(3)
    good = r.poller.shelf()
    r.cao.mode = "down"
    r.now += 20
    r.poller.tick()
    first = r.poller.shelf()
    assert first["stale_snapshot"] is True and first["snapshot_age_seconds"] >= 20
    r.now += 3600
    r.poller.tick()
    # Nothing a viewer would notice changed, so the page gets the stub: no age travels in it.
    assert r.poller.shelf(since=first["revision"]) == {
        "revision": first["revision"],
        "unchanged": True,
    }
    later = r.poller.shelf()
    # ...so the page ages the snapshot itself, from a timestamp that stays put while CAO is down
    assert later["last_good_at"] == first["last_good_at"] == good["generated_at"]
    assert later["generated_at"] > later["last_good_at"]
    assert later["snapshot_age_seconds"] >= 3600
    validate("shelf.schema.json", later)


def test_f1_nothing_read_yet_has_no_last_good_time(tmp_path: Path) -> None:
    r = ActionsRig(tmp_path, "busy")
    r.cao.mode = "down"
    r.poller.tick()
    assert r.poller.shelf()["last_good_at"] is None


# ---------------------------------------------------------------- F2: the conductor was deleted
def test_f2_pick_conductor_is_none_when_the_first_row_is_an_orphaned_helper() -> None:
    rows = [term("aaaaaaa1"), term("bbbbbbb1")]
    callers = {"aaaaaaa1": "deadbeef", "bbbbbbb1": "deadbeef"}
    assert tiering.pick_conductor(rows, callers, {"aaaaaaa1", "bbbbbbb1"}) is None


def test_f2_a_live_caller_on_row_zero_still_hands_the_title_to_a_row_without_a_caller() -> None:
    rows = [term("aaaaaaa1"), term("bbbbbbb1")]
    callers = {"aaaaaaa1": "bbbbbbb1", "bbbbbbb1": None}
    assert tiering.pick_conductor(rows, callers, {"aaaaaaa1", "bbbbbbb1"}) == "bbbbbbb1"


def test_f2_shelf_flags_a_session_without_a_conductor() -> None:
    callers = {"aaaaaaa1": "deadbeef", "bbbbbbb1": "aaaaaaa1"}
    s = session(shelf_of({"cao-s1": [term("aaaaaaa1"), term("bbbbbbb1")]}, callers), "s1")
    assert s["conductor_missing"] is True
    assert s["conductor"]["role"] == "helper" and s["conductor"]["orphan"] is True
    assert s["helpers"][0]["orphan"] is False  # its caller (row 0) is alive
    ok = session(shelf_of({"cao-s1": [term("aaaaaaa1"), term("bbbbbbb1")]}), "s1")
    assert ok["conductor_missing"] is False and ok["conductor"]["role"] == "conductor"


def test_f2_poller_reads_the_first_terminals_caller_and_ask_refuses(tmp_path: Path) -> None:
    """The reviewer's probe, through the poller with real ``get_terminal`` data."""
    r = ActionsRig(tmp_path, "quiet").warm()
    when = iso(2.0)
    r.cao.add_session(
        "orph",
        [("aaaaaaa1", "idle", when, "deadbeef"), ("bbbbbbb1", "idle", when, "deadbeef")],
    )
    r.warm(2)
    s = session(r.poller.shelf(), "orph")
    assert s["conductor_missing"] is True
    assert s["conductor"]["id"] == "aaaaaaa1" and s["conductor"]["orphan"] is True
    assert s["conductor"]["role"] == "helper" and s["helpers"][0]["orphan"] is True
    before = len(r.cao.writes())
    e = err(r.actions.ask, "orph", {"message": "hello", "conductor_id": "aaaaaaa1"})
    assert e.code == "conflict_changed" and "no conductor" in e.message
    assert len(r.cao.writes()) == before


def test_f2_ask_reads_the_caller_itself_when_the_cache_does_not_have_it(tmp_path: Path) -> None:
    r = ActionsRig(tmp_path, "quiet").warm()
    when = iso(2.0)
    r.cao.add_session("orph", [("aaaaaaa1", "idle", when, "deadbeef")])
    r.warm(2)
    r.poller._st.callers.clear()  # as after a failed caller read
    e = err(r.actions.ask, "orph", {"message": "hello", "conductor_id": "aaaaaaa1"})
    assert e.code == "conflict_changed"
    assert r.cao.writes() == []


# ------------------------------------------------------------------ F3: start idempotency key
KEY = "start-idempotency-key"


def start_body(**extra: Any) -> Dict[str, Any]:
    return {"agent_profile": "developer", "working_directory": "/w", **extra}


def post_queries(rig: ActionsRig) -> List[Dict[str, str]]:
    return [q for m, p, q in rig.cao.calls if m == "POST" and p == "/sessions"]


def test_f3_a_start_needs_the_key_the_dialog_chose(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    assert err(rig.actions.start, start_body()).code == "invalid_request"
    assert err(rig.actions.start, start_body(idempotency_key="short")).code == "invalid_request"
    assert post_queries(rig) == []


def test_f3_a_retry_without_a_session_name_returns_the_first_launch(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    first = rig.actions.start(start_body(idempotency_key=KEY))
    second = rig.actions.start(start_body(idempotency_key=KEY))  # the browser retried
    assert second == first
    assert len(post_queries(rig)) == 1
    assert post_queries(rig)[0]["idempotency_key"] == KEY


def test_f3_the_key_survives_a_failed_attempt(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    real = rig.cao._route
    state = {"failed": False}

    def flaky(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if method == "POST" and path == "/sessions" and not state["failed"]:
            state["failed"] = True
            return 500, {"detail": "boom"}
        return real(method, path, query, body)

    rig.cao._route = flaky  # type: ignore[method-assign]
    assert err(rig.actions.start, start_body(idempotency_key=KEY)).code == "cao_error"
    rig.actions.start(start_body(idempotency_key=KEY))
    assert [q["idempotency_key"] for q in post_queries(rig)] == [KEY, KEY]


def test_f3_another_key_or_another_request_is_a_new_launch(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    rig.actions.start(start_body(name="one", idempotency_key=KEY))
    rig.actions.start(start_body(name="two", idempotency_key=KEY))  # same key, other request
    rig.actions.start(start_body(name="three", idempotency_key="f" * 20))
    assert len(post_queries(rig)) == 3


# --------------------------------------------------------------- F5: names the shelf cannot show
def test_f5_sessions_with_unsupported_names_are_counted_not_dropped() -> None:
    view = {
        "cao-ok": [term("aaaaaaa1")],
        "cao-has.dot": [term("bbbbbbb1")],
        "cao-" + "x" * 70: [term("ccccccc1")],
        "not-ours": [term("ddddddd1")],
    }
    shelf = shelf_of(view)
    assert [s["name"] for s in shelf["sessions"]] == ["ok"]
    assert (
        shelf["counts"]["hidden_sessions"] == 2
    )  # only cao- sessions count; 'not-ours' is not ours


def test_f5_the_poller_counts_what_it_does_not_read(tmp_path: Path) -> None:
    r = ActionsRig(tmp_path, "quiet")
    r.cao.add_session("has.dot", [("eeeeeee1", "idle", iso(1.0), None)])
    r.warm(2)
    shelf = r.poller.shelf()
    assert shelf["counts"]["hidden_sessions"] == 1
    assert "has.dot" not in [s["name"] for s in shelf["sessions"]]
    assert not any("has.dot" in p for _, p, _q in r.cao.calls if p.startswith("/sessions/"))


# ------------------------------------------------------------ F6: a clock that cannot be read
@pytest.mark.parametrize("raw", ["garbage", "", None, 12345])
def test_f6_an_unreadable_clock_is_unknown_age_never_fresh(raw: Any) -> None:
    t = term("aaaaaaa1")
    t["last_active"] = raw
    s = session(shelf_of({"cao-s1": [t]}), "s1")
    assert s["tier"] == "parked" and s["tier_reason"] == "age_unknown" and s["warm"] is False
    assert s["last_active"] == "" and s["conductor"]["last_active"] == ""
    assert s["poll_class"] == "parked"


def test_f6_one_unreadable_terminal_stops_a_session_from_going_stale() -> None:
    odd = term("bbbbbbb1")
    odd["last_active"] = "garbage"
    s = session(shelf_of({"cao-s1": [term("aaaaaaa1", age=500.0), odd]}), "s1")
    assert s["tier"] == "parked" and s["tier_reason"] == "age_unknown"
    assert s["idle_seconds"] == 500 * 3600  # the readable clock is still shown


def test_f6_a_working_session_stays_live_even_if_its_clock_is_unreadable() -> None:
    t = term("aaaaaaa1", status="processing")
    t["last_active"] = "garbage"
    assert session(shelf_of({"cao-s1": [t]}), "s1")["tier"] == "live"


def test_f6_a_future_clock_is_age_zero_and_known() -> None:
    s = session(shelf_of({"cao-s1": [term("aaaaaaa1", age=-3.0)]}), "s1")
    assert s["idle_seconds"] == 0 and s["tier_reason"] == "idle_under_stale_hours"


# --------------------------------------------------------------------- F7: the reason names no 48
def test_f7_the_reason_follows_the_configured_threshold() -> None:
    th = {"warm_hours": 1.0, "stale_hours": 2.0}
    old = session(shelf_of({"cao-s1": [term("aaaaaaa1", age=3.0)]}, th=th), "s1")
    assert old["tier"] == "stale" and old["tier_reason"] == "idle_over_stale_hours"
    young = session(shelf_of({"cao-s1": [term("aaaaaaa1", age=1.5)]}, th=th), "s1")
    assert young["tier"] == "parked" and young["tier_reason"] == "idle_under_stale_hours"


# ---------------------------------------------------------- F9: Ask says what CAO did with it
def test_f9_ask_reports_delivered_for_an_idle_conductor(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    s = next(x for x in rig.poller.shelf()["sessions"] if x["conductor"]["status"] == "idle")
    got = rig.actions.ask(s["name"], {"message": "hi", "conductor_id": s["conductor"]["id"]})
    validate("actions.schema.json", got, "AskResponse")
    assert got["delivery"] == "delivered"


def test_f9_ask_reports_pending_when_cao_queued_it(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    s = next(x for x in rig.poller.shelf()["sessions"] if x["conductor"]["status"] == "processing")
    got = rig.actions.ask(s["name"], {"message": "hi", "conductor_id": s["conductor"]["id"]})
    validate("actions.schema.json", got, "AskResponse")
    assert got["delivery"] == "pending"


def test_f9_ask_still_succeeds_when_the_status_cannot_be_read_back(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    s = next(x for x in rig.poller.shelf()["sessions"] if x["conductor"]["status"] == "idle")
    real = rig.cao._route

    def no_inbox(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if method == "GET" and path.endswith("/inbox/messages"):
            return 500, {"detail": "boom"}
        return real(method, path, query, body)

    rig.cao._route = no_inbox  # type: ignore[method-assign]
    got = rig.actions.ask(s["name"], {"message": "hi", "conductor_id": s["conductor"]["id"]})
    assert got["accepted"] is True and got["delivery"] == "unknown"


# ------------------------------------------------------ F12 to F15: caps, pruning, bounded reads
def test_f12_history_times_are_capped() -> None:
    row = {"sender_id": "aaaaaaa1", "created_at": "x" * 5000, "status": "delivered"}
    out = history.rebuild([row], set(), session="s")
    assert len(out["closed"][0]["first_at"]) <= 40 and len(out["closed"][0]["last_at"]) <= 40


def test_f14_caches_of_vanished_sessions_are_pruned(tmp_path: Path) -> None:
    r = ActionsRig(tmp_path, "busy").warm(8)
    st = r.poller._st
    gone = next(iter(st.history))
    r.cao.sessions.pop(f"cao-{gone}")
    r.cao.order.remove(f"cao-{gone}")
    st.caller_retry["ffffffff"] = 1e18
    r.warm(2)
    assert gone not in st.history
    assert "ffffffff" not in st.caller_retry


def test_f15_all_history_stops_at_its_time_budget_and_says_incomplete(tmp_path: Path) -> None:
    r = ActionsRig(tmp_path, "busy").warm(8)
    r.now += 600  # every cache is old
    real = r.cao._route
    reads = {"n": 0}

    def slow(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if method == "GET" and path.endswith("/inbox/messages"):
            reads["n"] += 1
            r.now += 6.0  # each CAO read takes six seconds on the page's clock
        return real(method, path, query, body)

    r.cao._route = slow  # type: ignore[method-assign]
    out = r.poller.history_all()
    assert reads["n"] <= 2 and out["complete"] is False


def test_f15_one_failing_read_does_not_fail_all_history(tmp_path: Path) -> None:
    r = ActionsRig(tmp_path, "busy").warm(8)
    r.now += 600
    real = r.cao._route

    def broken(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if method == "GET" and path.endswith("/inbox/messages"):
            return 500, {"detail": "boom"}
        return real(method, path, query, body)

    r.cao._route = broken  # type: ignore[method-assign]
    out = r.poller.history_all()
    assert out["complete"] is False and isinstance(out["closed"], list)


# ------------------------------------------------------------------ L1: v2 headers are required
def test_l1_a_request_with_only_v1_headers_is_refused() -> None:
    v = auth.ProxyVerifier("s3cret", clock=lambda: 1000.0)
    v1 = auth.signed_headers("s3cret", "POST", "/api/ask", b"{}", now=1000, v2=False)
    assert v.verify("POST", "/api/ask", b"{}", v1) is False
    both = auth.signed_headers("s3cret", "POST", "/api/ask", b"{}", now=1000)
    assert v.verify("POST", "/api/ask", b"{}", both) is True
    assert v.verify("POST", "/api/ask", b"{}", both) is False  # replay


def test_l1_stripping_the_v2_headers_from_a_captured_request_does_not_replay_it() -> None:
    v = auth.ProxyVerifier("s3cret", clock=lambda: 1000.0)
    both = auth.signed_headers("s3cret", "POST", "/api/ask", b"{}", now=1000)
    assert v.verify("POST", "/api/ask", b"{}", both) is True
    stripped = {auth.HEADER_V1: both[auth.HEADER_V1]}
    assert v.verify("POST", "/api/ask", b"{}", stripped) is False


# ---------------------------------------------------- L3: suggestions are per provider; clean text
APPROVAL = ["output", "APPROVAL_REQUIRED: push to origin?"]


def test_l3_suggestions_are_only_for_the_provider_the_shape_was_seen_on() -> None:
    assert prompts.suggest(APPROVAL, "mock_cli") == [
        {"label": "Approve", "send": "y"},
        {"label": "Deny", "send": "n"},
    ]
    for other in ("claude_code", "kiro_cli", "codex", "", "MOCK_CLI"):
        assert prompts.suggest(APPROVAL, other) == [], other


def test_l3_prompt_of_another_provider_offers_no_buttons(tmp_path: Path) -> None:
    rig = ActionsRig(tmp_path, "busy").warm()
    for t in rig.cao.terminals.values():
        if t["id"] == "b94da5c6":
            t["provider"] = "claude_code"
    got = rig.actions.prompt("b94da5c6")
    assert got["provider"] == "claude_code" and got["suggested"] == []
    assert got["tail"][-1] == "APPROVAL_REQUIRED: push to origin?"


def test_l3_bidi_zero_width_and_c1_characters_are_removed() -> None:
    hostile = "a\u202eb\u2066c\u2069d\u200be\u200ff\u009bg\u0085h\ufeffi\u00adj"
    assert prompts.clean_tail(hostile) == ["abcdefghij"]
    assert history.row_text("x\u202ey\u009bz\u200b") == "xyz"
    for code in list(range(0x202A, 0x202F)) + list(range(0x2066, 0x206A)) + list(range(0x80, 0xA0)):
        assert prompts.clean_tail("p" + chr(code) + "q") == ["pq"], hex(code)


def test_l3_ordinary_text_is_untouched() -> None:
    text = "café 中文 → ok\ttab"
    assert prompts.clean_tail(text) == ["café 中文 → ok tab"]


# --------------------------------------------- ledger: closed helpers belong to one CAO address
def ledger_entry(tid: str = "aaaaaaa1", session: str = "s") -> Dict[str, Any]:
    return {
        "session": session,
        "terminal_id": tid,
        "rows": 2,
        "first_at": "2026-11-15T09:00:00+00:00",
        "last_at": "2026-11-15T10:00:00+00:00",
        "last_status": "delivered",
    }


LNOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
URL_A, URL_B = "http://127.0.0.1:9889", "http://127.0.0.1:9990"


def test_ledger_header_records_the_address(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.set_cao_url(URL_A)
    led.merge([ledger_entry()], LNOW)
    assert json.loads(path.read_text())["cao_url"] == URL_A


def test_ledger_for_another_address_is_set_aside_not_mixed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    first = Ledger(path)
    first.set_cao_url(URL_A)
    first.merge([ledger_entry("aaaaaaa1")], LNOW)
    again = Ledger(path)  # a restart with the address changed in settings
    again.set_cao_url(URL_B)
    assert again.entries_for("s") == []  # the same session name at another CAO has no history here
    aside = list(tmp_path.glob("ledger.json.*.aside"))
    assert len(aside) == 1 and json.loads(aside[0].read_text())["cao_url"] == URL_A
    again.merge([ledger_entry("bbbbbbb1")], LNOW)
    assert json.loads(path.read_text())["cao_url"] == URL_B
    assert [e["terminal_id"] for e in again.entries_for("s")] == ["bbbbbbb1"]


def test_ledger_switches_address_while_running(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.set_cao_url(URL_A)
    led.merge([ledger_entry("aaaaaaa1")], LNOW)
    led.set_cao_url(URL_B)
    assert led.entries_for("s") == []
    led.set_cao_url(URL_B)  # the same address again changes nothing
    led.merge([ledger_entry("bbbbbbb1")], LNOW)
    assert led.count_for("s") == 1
    assert len(list(tmp_path.glob("ledger.json.*.aside"))) == 1


def test_ledger_with_the_same_address_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    first = Ledger(path)
    first.set_cao_url(URL_A)
    first.merge([ledger_entry()], LNOW)
    again = Ledger(path)
    again.set_cao_url(URL_A)
    assert again.count_for("s") == 1 and not list(tmp_path.glob("*.aside"))


def test_ledger_without_a_header_from_an_older_file_is_adopted(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"version": 1, "entries": [ledger_entry()]}))
    led = Ledger(path)
    led.set_cao_url(URL_A)
    assert led.count_for("s") == 1 and not list(tmp_path.glob("*.aside"))
