"""A1: pure tiering. The expected outputs are the shared fixtures; the rest are boundary cases."""

from __future__ import annotations

import ast
import copy
from datetime import timedelta
from typing import Any, Dict, List, Optional

import cao_shelf_support as sup
import pytest
from cao_shelf import tiering

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


def term(tid: str, status: str = "idle", age: float = 5.0, window: str = "w") -> Dict[str, Any]:
    return {
        "id": tid,
        "tmux_window": window,
        "agent_profile": "p",
        "provider": "mock_cli",
        "status": status,
        "last_active": iso(age),
        "pending_inbox_count": 0,
    }


def one(
    terms: List[Dict[str, Any]],
    callers: Optional[Dict[str, Optional[str]]] = None,
    counts: Optional[Dict[str, Optional[int]]] = None,
    extra: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    view: Dict[str, Any] = {"cao-s1": terms}
    view.update(extra or {})
    return tiering.build_shelf(view, callers or {}, NOW, TH, counts or {}, cao=CAO_META)


def sess(shelf: Dict[str, Any], name: str = "s1") -> Dict[str, Any]:
    return next(s for s in shelf["sessions"] if s["name"] == name)


@pytest.mark.parametrize("day", ["quiet", "busy"])
def test_fixture_days_match_expected_shelf(day: str) -> None:
    view, callers = sup.fixture_view(day)
    history = {
        name[4:]: len(
            sup.load(sup.FIXTURES / "expected" / day / f"history.{name[4:]}.json")["closed"]
        )
        for name in view
    }
    got = tiering.build_shelf(
        view, callers, sup.clock_of(day), TH, history, cao=CAO_META, revision=1
    )
    assert got == sup.load(sup.FIXTURES / "expected" / f"{day}.shelf.json")


def test_stale_boundary() -> None:
    assert sess(one([term("aaaaaaa1", age=47.99)]))["tier"] == "parked"
    assert sess(one([term("aaaaaaa1", age=48.0)]))["tier"] == "stale"
    s = sess(one([term("aaaaaaa1", age=48.0)]))
    assert s["tier_reason"] == "idle_over_stale_hours" and s["poll_class"] == "stale"


def test_warm_boundary() -> None:
    assert sess(one([term("aaaaaaa1", age=0.99)]))["warm"] is True
    assert sess(one([term("aaaaaaa1", age=0.99)]))["poll_class"] == "hot"
    assert sess(one([term("aaaaaaa1", age=1.0)]))["warm"] is False
    assert sess(one([term("aaaaaaa1", age=1.0)]))["poll_class"] == "parked"


def test_thresholds_come_from_settings() -> None:
    shelf = tiering.build_shelf(
        {"cao-s1": [term("aaaaaaa1", age=3.0)]},
        {},
        NOW,
        {"warm_hours": 0.25, "stale_hours": 2.0},
        {},
        cao=CAO_META,
    )
    assert sess(shelf)["tier"] == "stale"
    assert shelf["thresholds"] == {"warm_hours": 0.25, "stale_hours": 2.0}


def test_idle_age_is_the_youngest_terminal() -> None:
    shelf = one([term("aaaaaaa1", age=60), term("bbbbbbb1", age=3)], {"bbbbbbb1": "aaaaaaa1"})
    s = sess(shelf)
    assert s["idle_seconds"] == 3 * 3600 and s["tier"] == "parked"
    assert s["last_active"] == iso(3)


def test_waiting_beats_processing_and_error_beats_live() -> None:
    both = one([term("aaaaaaa1", "processing"), term("bbbbbbb1", "waiting_user_answer")])
    assert sess(both)["tier"] == "needs_you"
    assert sess(both)["tier_reason"] == "waiting_user_answer"
    err = one([term("aaaaaaa1", "processing"), term("bbbbbbb1", "error")])
    assert sess(err)["tier"] == "needs_you" and sess(err)["tier_reason"] == "error"
    assert [a["status"] for a in sess(err)["asks"]] == ["error"]


def test_completed_is_live_only_while_it_just_completed() -> None:
    s = sess(one([term("aaaaaaa1", "completed", age=0.1)]))
    assert s["tier"] == "live" and s["tier_reason"] == "completed"


@pytest.mark.parametrize(
    "age, tier, reason, warm",
    [
        (1.0, "parked", "idle_under_stale_hours", False),
        (72.0, "stale", "idle_over_stale_hours", False),
        (300.0, "stale", "idle_over_stale_hours", False),
    ],
)
def test_completed_past_the_warm_window_is_tiered_by_age_like_idle(
    age: float, tier: str, reason: str, warm: bool
) -> None:
    # CAO latches ``completed`` until new input, so it says only that the screen is at a prompt.
    s = sess(one([term("aaaaaaa1", "completed", age=age)]))
    assert (s["tier"], s["tier_reason"], s["warm"]) == (tier, reason, warm)


def test_completed_with_an_unreadable_clock_is_parked_age_unknown() -> None:
    t = term("aaaaaaa1", "completed")
    t["last_active"] = "not a time"
    s = sess(one([t]))
    assert s["tier"] == "parked" and s["tier_reason"] == "age_unknown" and s["warm"] is False


def test_processing_is_live_at_any_age() -> None:
    s = sess(one([term("aaaaaaa1", "processing", age=300)]))
    assert s["tier"] == "live" and s["tier_reason"] == "processing"


def test_unknown_status_counts_as_idle_for_tiering() -> None:
    shelf = one([term("aaaaaaa1", "unknown", age=100), term("bbbbbbb1", "weird", age=100)])
    assert sess(shelf)["tier"] == "stale"
    assert shelf["counts"]["by_status"]["unknown"] == 2


def test_missing_status_is_unknown() -> None:
    t = term("aaaaaaa1")
    del t["status"]
    assert one([t])["counts"]["by_status"]["unknown"] == 1


def test_order_is_tier_then_youngest_then_name() -> None:
    view = {
        "cao-zeta": [term("00000001", age=5)],
        "cao-alpha": [term("00000002", age=5)],
        "cao-old": [term("00000003", age=100)],
        "cao-busy": [term("00000004", "processing", age=0)],
        "cao-ask": [term("00000005", "waiting_user_answer", age=9)],
        "cao-young": [term("00000006", age=2)],
    }
    shelf = tiering.build_shelf(view, {}, NOW, TH, {}, cao=CAO_META)
    assert [s["name"] for s in shelf["sessions"]] == [
        "ask",
        "busy",
        "young",
        "alpha",
        "zeta",
        "old",
    ]


def test_peer_and_helper_roles() -> None:
    terms = [term("aaaaaaa1"), term("bbbbbbb1"), term("ccccccc1")]
    shelf = one(terms, {"bbbbbbb1": "aaaaaaa1", "ccccccc1": None})
    s = sess(shelf)
    assert s["conductor"]["role"] == "conductor"
    assert [(h["id"], h["role"]) for h in s["helpers"]] == [
        ("bbbbbbb1", "helper"),
        ("ccccccc1", "peer"),
    ]
    assert s["terminal_count"] == 3


def test_conductor_is_index_zero_by_default() -> None:
    s = sess(one([term("aaaaaaa1"), term("bbbbbbb1")], {"bbbbbbb1": "aaaaaaa1"}))
    assert s["conductor"]["id"] == "aaaaaaa1"


def test_conductor_skips_index_zero_whose_caller_is_alive() -> None:
    # the oldest row is a helper of a live terminal elsewhere: the oldest row without a caller wins
    view = {
        "cao-s1": [term("aaaaaaa1"), term("bbbbbbb1"), term("ccccccc1")],
        "cao-other": [term("dddddddd")],
    }
    callers = {"aaaaaaa1": "dddddddd", "bbbbbbb1": None, "ccccccc1": "bbbbbbb1"}
    s = sess(tiering.build_shelf(view, callers, NOW, TH, {}, cao=CAO_META))
    assert s["conductor"]["id"] == "bbbbbbb1"
    assert [h["id"] for h in s["helpers"]] == ["aaaaaaa1", "ccccccc1"]


def test_conductor_falls_back_to_index_zero_when_every_row_has_a_live_caller() -> None:
    view = {"cao-s1": [term("aaaaaaa1"), term("bbbbbbb1")], "cao-o": [term("dddddddd")]}
    callers = {"aaaaaaa1": "dddddddd", "bbbbbbb1": "dddddddd"}
    s = sess(tiering.build_shelf(view, callers, NOW, TH, {}, cao=CAO_META))
    assert s["conductor"]["id"] == "aaaaaaa1"


def test_orphan_when_caller_is_not_in_the_fleet() -> None:
    s = sess(one([term("aaaaaaa1"), term("bbbbbbb1")], {"bbbbbbb1": "deadbeef"}))
    assert s["helpers"][0]["orphan"] is True and s["helpers"][0]["role"] == "helper"
    assert s["conductor"]["orphan"] is False


def test_unread_session_must_not_create_orphans() -> None:
    view: Dict[str, Any] = {"cao-s1": [term("aaaaaaa1"), term("bbbbbbb1")], "cao-unread": None}
    shelf = tiering.build_shelf(view, {"bbbbbbb1": "deadbeef"}, NOW, TH, {}, cao=CAO_META)
    assert sess(shelf)["helpers"][0]["orphan"] is False
    assert [s["name"] for s in shelf["sessions"]] == ["s1"]
    assert shelf["counts"]["sessions"] == 1
    assert shelf["history_total"] is None


def test_caller_in_another_session_is_not_an_orphan() -> None:
    view = {"cao-s1": [term("aaaaaaa1"), term("bbbbbbb1")], "cao-s2": [term("cccccccc")]}
    shelf = tiering.build_shelf(view, {"bbbbbbb1": "cccccccc"}, NOW, TH, {}, cao=CAO_META)
    assert sess(shelf)["helpers"][0]["orphan"] is False


def test_in_flight_needs_processing_conductor_and_its_own_helper() -> None:
    terms = [term("aaaaaaa1", "processing"), term("bbbbbbb1", "processing")]
    s = sess(one(terms, {"bbbbbbb1": "aaaaaaa1"}))
    assert s["in_flight"] == {"helper_terminal_id": "bbbbbbb1", "label": "w"}
    waiting = [term("aaaaaaa1", "processing"), term("bbbbbbb1", "waiting_user_answer")]
    assert sess(one(waiting, {"bbbbbbb1": "aaaaaaa1"}))["in_flight"] is not None
    # conductor idle: no in-flight line
    idle = [term("aaaaaaa1", "idle"), term("bbbbbbb1", "processing")]
    assert sess(one(idle, {"bbbbbbb1": "aaaaaaa1"}))["in_flight"] is None
    # a peer that is processing is not "waiting on helper"
    peer = [term("aaaaaaa1", "processing"), term("bbbbbbb1", "processing")]
    assert sess(one(peer, {"bbbbbbb1": None}))["in_flight"] is None
    # a helper of someone else does not count
    other = [term("aaaaaaa1", "processing"), term("bbbbbbb1", "processing")]
    assert sess(one(other, {"bbbbbbb1": "99999999"}))["in_flight"] is None


def test_asks_for_waiting_and_error_use_last_active_as_approximate_since() -> None:
    terms = [term("aaaaaaa1"), term("bbbbbbb1", "waiting_user_answer", age=0.5, window="tester")]
    ask = sess(one(terms, {"bbbbbbb1": "aaaaaaa1"}))["asks"]
    assert ask == [
        {
            "terminal_id": "bbbbbbb1",
            "label": "tester",
            "status": "waiting_user_answer",
            "since": iso(0.5),
            "since_is_approximate": True,
        }
    ]


def test_idle_seconds_is_never_negative() -> None:
    future = term("aaaaaaa1")
    future["last_active"] = (NOW + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    s = sess(one([future]))
    assert s["idle_seconds"] == 0 and s["conductor"]["idle_seconds"] == 0


def test_history_counts_and_total() -> None:
    shelf = one([term("aaaaaaa1")], counts={"s1": 3})
    assert sess(shelf)["history_count"] == 3 and shelf["history_total"] == 3
    unread = one([term("aaaaaaa1")], counts={"s1": None})
    assert sess(unread)["history_count"] is None and unread["history_total"] is None
    two = one([term("aaaaaaa1")], counts={"s1": 3, "s2": 2}, extra={"cao-s2": [term("bbbbbbb2")]})
    assert two["history_total"] == 5
    mixed = one([term("aaaaaaa1")], counts={"s1": 3}, extra={"cao-s2": [term("bbbbbbb2")]})
    assert mixed["history_total"] is None


def test_hostile_strings_pass_through_as_data_only() -> None:
    t = term("aaaaaaa1", window="<img src=x onerror=alert(1)>")
    t["agent_profile"] = "javascript:alert(1)"
    s = sess(one([t]))
    assert s["conductor"]["label"] == "<img src=x onerror=alert(1)>"
    assert s["conductor"]["profile"] == "javascript:alert(1)"


def test_terminals_with_bad_ids_are_dropped_and_empty_sessions_hidden() -> None:
    view = {"cao-s1": [term("aaaaaaa1"), term("../etc")], "cao-s2": [], "cao-s3": [term("zzzz")]}
    shelf = tiering.build_shelf(view, {}, NOW, TH, {}, cao=CAO_META)
    assert [s["name"] for s in shelf["sessions"]] == ["s1"]
    assert shelf["counts"]["terminals"] == 1


def test_names_outside_the_contract_regex_are_skipped() -> None:
    view = {
        "cao-ok": [term("aaaaaaa1")],
        "cao-bad name": [term("bbbbbbb1")],
        "other": [term("cccccccc")],
    }
    shelf = tiering.build_shelf(view, {}, NOW, TH, {}, cao=CAO_META)
    assert [s["name"] for s in shelf["sessions"]] == ["ok"]


def test_empty_shelf_when_cao_is_unreachable_and_nothing_was_read() -> None:
    err = {"code": "cao_unreachable", "message": "x", "retryable": True}
    shelf = tiering.empty_shelf(NOW, TH, {**CAO_META, "reachable": False, "error": err}, revision=4)
    assert shelf["sessions"] == [] and shelf["history_total"] is None
    assert shelf["counts"]["terminals"] == 0 and shelf["cao"]["reachable"] is False
    assert shelf["revision"] == 4


def test_change_signature_ignores_clock_only_changes() -> None:
    view, callers = sup.fixture_view("quiet")
    a = tiering.build_shelf(view, callers, NOW, TH, {}, cao=CAO_META)
    b = tiering.build_shelf(view, callers, NOW + timedelta(seconds=5), TH, {}, cao=CAO_META)
    c = tiering.build_shelf(view, callers, NOW + timedelta(hours=50), TH, {}, cao=CAO_META)
    assert tiering.change_signature(a) == tiering.change_signature(b)
    assert tiering.change_signature(a) != tiering.change_signature(c)
    changed = copy.deepcopy(view)
    changed["cao-payments-fix"][0]["status"] = "processing"
    d = tiering.build_shelf(changed, callers, NOW, TH, {}, cao=CAO_META)
    assert tiering.change_signature(a) != tiering.change_signature(d)


def test_module_is_pure() -> None:
    tree = ast.parse((sup.BACKEND / "cao_shelf" / "tiering.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"socket", "urllib", "time", "os", "http", "threading", "subprocess"}
