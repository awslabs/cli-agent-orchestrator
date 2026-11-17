"""A3, A6 and the schedule half of A7: the poller, its cadence and its request budget."""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import cao_shelf_support as sup
import pytest
from cao_shelf.cao import CaoClient
from cao_shelf.config import DEFAULTS
from cao_shelf.errors import ShelfError
from cao_shelf.ledger import Ledger
from cao_shelf.poller import HISTORY_OPEN_MAX_AGE, Poller

START = sup.clock_of("busy").timestamp()


class Rig:
    """A poller on a fake clock, a fake CAO and a throwaway ledger."""

    def __init__(
        self, tmp: Path, day: str = "busy", settings: Optional[Dict[str, Any]] = None
    ) -> None:
        self.cao = sup.FakeCao(day)
        self.now = sup.clock_of(day).timestamp()
        self.settings = dict(DEFAULTS, **(settings or {}))
        self.ledger = Ledger(tmp / "ledger.json", enabled=bool(self.settings["persist_history"]))
        self.opener = sup.FakeOpener(self.cao)
        self.poller = Poller(
            self._factory, lambda: self.settings, self.ledger, clock=lambda: self.now
        )

    def _factory(self, url: str, observer: Callable[[str, str], None]) -> CaoClient:
        return CaoClient(url, self.opener, observer=observer)

    def tick(self, advance: float = 5.0) -> None:
        self.poller.tick()
        self.now += advance

    def ticks(self, n: int, advance: float = 5.0) -> None:
        for _ in range(n):
            self.tick(advance)

    def mark(self) -> int:
        return len(self.cao.calls)

    def since(self, mark: int) -> List[Tuple[str, str, Dict[str, str]]]:
        return self.cao.calls[mark:]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def strip(shelf: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(shelf)
    out.pop("revision", None)
    return out


# ------------------------------------------------------------------------------------ the data
@pytest.mark.parametrize("day", ["quiet", "busy"])
def test_warm_poller_serves_the_expected_fixture_shelf(tmp_path: Path, day: str) -> None:
    r = Rig(tmp_path, day)
    r.poller.mark_active()
    r.cao.now = r.cao.now  # the fake clock is already the fixture clock
    r.poller.tick()
    r.poller.tick()  # history is read four sessions per tick
    r.poller.tick()
    r.poller.tick()
    shelf = r.poller.shelf()
    expected = sup.load(sup.FIXTURES / "expected" / f"{day}.shelf.json")
    assert strip(shelf) == strip(expected)


def test_cold_start_costs_what_the_proposal_says(rig: Rig) -> None:
    rig.tick()
    assert rig.cao.count("GET", "/sessions") == 1 + 12  # the list plus one read per session
    callers = [c for c in rig.cao.calls if c[1].startswith("/terminals/") and c[1].count("/") == 2]
    assert len(callers) == 20  # one per terminal, the first of each session too (20 terminals)
    inbox = [c for c in rig.cao.calls if "inbox" in c[1]]
    assert len(inbox) == 4
    assert rig.cao.count("GET", "/health") == 1
    assert rig.cao.writes() == []


def test_steady_state_reads_no_callers_and_no_health(rig: Rig) -> None:
    rig.ticks(6)
    mark = rig.mark()
    rig.ticks(12)
    window = rig.since(mark)
    assert not [c for c in window if c[1].startswith("/terminals/") and c[1].count("/") == 2]
    assert not [c for c in window if c[1] == "/health"]
    assert not [c for c in window if "inbox" in c[1]]


# --------------------------------------------------------------------------------- the budget
def test_quiet_day_stays_inside_the_budget(tmp_path: Path) -> None:
    r = Rig(tmp_path, "quiet")
    r.ticks(8)  # warm up: cold reads, history, callers
    mark = r.mark()
    r.ticks(12)  # 60 seconds
    per_tick = len(r.since(mark)) / 12
    assert per_tick <= 2.75, per_tick


def test_quiet_day_with_one_active_relay_stays_inside_the_same_budget(tmp_path: Path) -> None:
    """The quiet day plus the rig's one warm session, measured without and then with a relay
    on that session: the relay reconciles from what the poller read and adds no CAO request."""
    rig = sup.RelayRig(tmp_path, "quiet")

    def minute() -> int:
        mark = len(rig.cao.calls)
        for _ in range(12):
            rig.poller.tick()
            rig.now += 5
        return len(rig.cao.calls) - mark

    minute()  # warm up: cold reads, history, callers
    without = minute()
    relay = rig.attach()
    assert relay["state"] == "active"
    with_relay = minute()
    assert with_relay == without, (without, with_relay)
    assert (with_relay - 12) / 12 <= 2.75  # the warm session costs one read per tick
    assert rig.relays.list()["relays"][0]["state"] == "active"


def test_fifty_sessions_stay_inside_the_budget(tmp_path: Path) -> None:
    r = Rig(tmp_path, "quiet")
    for name in list(r.cao.order):
        for term in r.cao.sessions[name]["terminals"]:
            r.cao.terminals.pop(term["id"], None)
        del r.cao.sessions[name]
    r.cao.order.clear()
    nid = 0

    def ts(hours_ago: float) -> str:
        return (sup.clock_of("quiet") - timedelta(hours=hours_ago)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

    plan = [("processing", 0.0)] * 2 + [("idle", 5.0)] * 8 + [("idle", 100.0)] * 40
    for i, (status, age) in enumerate(plan):
        nid += 1
        r.cao.add_session(f"load-{i}", [(f"{nid:08x}", status, ts(age), None)])
    r.ticks(16)  # cold start: 13 ticks of history reads, four sessions each
    mark = r.mark()
    r.ticks(12)
    per_tick = len(r.since(mark)) / 12
    assert per_tick <= 9, per_tick
    shelf = r.poller.shelf()
    assert shelf["counts"]["sessions"] == 50 and shelf["counts"]["by_tier"]["stale"] == 40


def test_cadence_classes(tmp_path: Path) -> None:
    r = Rig(tmp_path, "busy")
    r.ticks(6)
    mark = r.mark()
    r.ticks(12)
    reads: Dict[str, int] = {}
    for _, path, _q in r.since(mark):
        if path.startswith("/sessions/cao-"):
            reads[path.rsplit("/", 1)[-1]] = reads.get(path.rsplit("/", 1)[-1], 0) + 1
    assert reads["cao-payments-fix"] == 12  # needs you: every tick
    assert reads["cao-docs-refresh"] == 12  # live: every tick
    assert reads["cao-migrate-db"] == 4  # parked: every 15 s
    assert reads["cao-ci-flakes"] == 1  # stale: every 60 s
    assert r.cao.count("GET", "/sessions") >= 12


def test_new_sessions_are_read_at_once(rig: Rig) -> None:
    rig.ticks(2)
    rig.cao.add_session("fresh-one", [("0f0f0f0f", "idle", "2026-11-15T11:59:00.000000Z", None)])
    mark = rig.mark()
    rig.tick()
    assert ("GET", "/sessions/cao-fresh-one", {}) in rig.since(mark)
    assert "fresh-one" in [s["name"] for s in rig.poller.shelf()["sessions"]]


def test_sessions_that_disappear_are_dropped(rig: Rig) -> None:
    rig.ticks(2)
    del rig.cao.sessions["cao-docs-refresh"]
    rig.cao.order.remove("cao-docs-refresh")
    rig.tick()
    assert "docs-refresh" not in [s["name"] for s in rig.poller.shelf()["sessions"]]


def test_a_session_that_404s_between_list_and_read_is_dropped(rig: Rig) -> None:
    rig.ticks(2)
    real = rig.cao._route

    def flaky(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if path == "/sessions/cao-payments-fix":
            return 404, {"detail": "gone"}
        return real(method, path, query, body)

    rig.cao._route = flaky  # type: ignore[method-assign]
    rig.tick()
    shelf = rig.poller.shelf()
    assert "payments-fix" not in [s["name"] for s in shelf["sessions"]]
    assert shelf["cao"]["error"] is None


# ---------------------------------------------------------------------------------- activity
def test_poller_runs_only_while_a_client_is_active(rig: Rig) -> None:
    assert rig.poller.step() is False and rig.cao.calls == []
    rig.poller.mark_active()
    assert rig.poller.step() is True and rig.cao.calls
    rig.now += 29
    assert rig.poller.step() is True
    rig.now += 2  # 31 seconds since the last request for the shelf
    n = len(rig.cao.calls)
    assert rig.poller.step() is False and len(rig.cao.calls) == n


# ---------------------------------------------------------------------------------- revisions
def test_revision_bumps_only_on_change(rig: Rig) -> None:
    rig.ticks(5, advance=1)  # history is read four sessions per tick
    first = rig.poller.shelf()["revision"]
    rig.tick(advance=1)
    rig.tick(advance=1)
    assert rig.poller.shelf()["revision"] == first
    assert rig.poller.shelf(since=first) == {"revision": first, "unchanged": True}
    rig.cao.set_status("8f9634f8", "processing")  # a helper starts working
    rig.tick(advance=1)
    second = rig.poller.shelf()["revision"]
    assert second > first
    assert rig.poller.shelf(since=first)["revision"] == second  # a full answer, not the stub
    assert "sessions" in rig.poller.shelf(since=first)
    assert "unchanged" not in rig.poller.shelf(since=first)


def test_revision_ignores_time_passing_on_its_own(rig: Rig) -> None:
    """Ages are computed on screen from last_active, so a quiet fleet keeps serving the stub."""
    rig.ticks(5, advance=1)
    first = rig.poller.shelf()["revision"]
    before = rig.poller.shelf()["sessions"][-1]["idle_seconds"]
    for _ in range(5):
        rig.now += 70  # about six minutes in all, no tier threshold crossed
        rig.tick(advance=1)
        assert rig.poller.shelf(since=first) == {"revision": first, "unchanged": True}
    assert rig.poller.shelf()["sessions"][-1]["idle_seconds"] > before  # a full read is current


def test_revision_moves_when_the_clock_crosses_a_tier_threshold(tmp_path: Path) -> None:
    r = Rig(tmp_path, "quiet")
    r.ticks(3)
    first = r.poller.shelf()["revision"]
    r.now += 50 * 3600  # parked sessions become stale
    r.tick()
    assert r.poller.shelf(since=first)["revision"] > first


# ---------------------------------------------------------------------------------- failures
def test_last_good_snapshot_survives_cao_going_down(rig: Rig) -> None:
    rig.ticks(3)
    good = rig.poller.shelf()
    rig.cao.mode = "down"
    rig.now += 20
    rig.tick()
    down = rig.poller.shelf()
    assert down["stale_snapshot"] is True
    assert down["cao"]["reachable"] is False and down["cao"]["error"]["code"] == "cao_unreachable"
    assert down["snapshot_age_seconds"] >= 20
    assert [s["name"] for s in down["sessions"]] == [s["name"] for s in good["sessions"]]
    assert down["revision"] > good["revision"]
    rig.cao.mode = "ok"
    rig.tick()
    back = rig.poller.shelf()
    assert (
        back["stale_snapshot"] is False
        and back["cao"]["error"] is None
        and back["cao"]["reachable"]
    )


def test_no_snapshot_and_cao_down_is_not_an_empty_shelf(rig: Rig) -> None:
    rig.cao.mode = "down"
    rig.tick()
    shelf = rig.poller.shelf()
    assert shelf["sessions"] == [] and shelf["history_total"] is None
    assert shelf["cao"]["reachable"] is False and shelf["cao"]["error"]["code"] == "cao_unreachable"
    assert shelf["counts"]["terminals"] == 0


@pytest.mark.parametrize(
    "mode,code,reachable",
    [
        ("401", "cao_auth_required", True),
        ("badshape", "cao_incompatible", True),
        ("slow", "cao_timeout", False),
    ],
)
def test_other_failures_are_named(rig: Rig, mode: str, code: str, reachable: bool) -> None:
    rig.ticks(2)
    rig.cao.mode = mode
    rig.tick()
    shelf = rig.poller.shelf()
    assert shelf["cao"]["error"]["code"] == code and shelf["cao"]["reachable"] is reachable
    assert shelf["stale_snapshot"] is True


def test_one_bad_session_does_not_stop_the_others_and_is_reported(rig: Rig) -> None:
    real = rig.cao._route

    def broken(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if path == "/sessions/cao-infra-audit":
            return 200, {"no": "terminals"}
        return real(method, path, query, body)

    rig.cao._route = broken  # type: ignore[method-assign]
    rig.tick()
    shelf = rig.poller.shelf()
    assert shelf["counts"]["sessions"] == 11
    assert shelf["cao"]["reachable"] is True and shelf["cao"]["error"]["code"] == "cao_incompatible"
    assert shelf["stale_snapshot"] is False


def test_transport_failure_stops_the_tick_early(rig: Rig) -> None:
    rig.cao.mode = "down"
    rig.tick()
    assert rig.poller.requests_last_minute() == 1  # the list failed; nothing else was tried


def test_unread_session_does_not_create_orphans(rig: Rig) -> None:
    real = rig.cao._route

    def broken(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if path == "/sessions/cao-infra-audit":
            return 500, {"detail": "x"}
        return real(method, path, query, body)

    rig.cao._route = broken  # type: ignore[method-assign]
    rig.ticks(2)
    shelf = rig.poller.shelf()
    probe = next(s for s in shelf["sessions"] if s["name"] == "search-tuning")["helpers"][0]
    assert probe["caller_id"] == "dead0001" and probe["orphan"] is False
    rig.cao._route = real  # type: ignore[method-assign]
    rig.ticks(2)
    probe = next(s for s in rig.poller.shelf()["sessions"] if s["name"] == "search-tuning")[
        "helpers"
    ][0]
    assert probe["orphan"] is True


def test_hostile_caller_ids_are_ignored(rig: Rig) -> None:
    rig.cao.terminals["b94da5c6"]["caller_id"] = "../../etc"
    rig.ticks(2)
    shelf = rig.poller.shelf()
    tester = next(h for s in shelf["sessions"] for h in s["helpers"] if h["id"] == "b94da5c6")
    assert tester["caller_id"] is None and tester["role"] == "peer"


def test_failed_caller_read_is_retried_after_a_pause(rig: Rig) -> None:
    real = rig.cao._route
    hits = {"n": 0}

    def flaky(method: str, path: str, query: Dict[str, str], body: Any) -> Any:
        if path == "/terminals/b94da5c6":
            hits["n"] += 1
            return 500, {"detail": "x"}
        return real(method, path, query, body)

    rig.cao._route = flaky  # type: ignore[method-assign]
    rig.ticks(3)
    assert hits["n"] == 1  # backed off for 30 s
    rig.now += 40
    rig.tick()
    assert hits["n"] == 2


# ------------------------------------------------------------------------------------ history
def test_history_reads_are_four_per_tick_and_then_cached(rig: Rig) -> None:
    rig.tick()
    first = [c for c in rig.cao.calls if "inbox" in c[1]]
    assert len(first) == 4 and all(q == {"limit": "100", "order": "desc"} for _, _, q in first)
    assert rig.poller.shelf()["history_total"] is None  # not every inbox was read yet
    rig.ticks(2)
    assert len([c for c in rig.cao.calls if "inbox" in c[1]]) == 12
    assert rig.poller.shelf()["history_total"] == 20
    rig.now += 600
    mark = rig.mark()
    rig.ticks(3)
    assert not [c for c in rig.since(mark) if "inbox" in c[1]]  # conductor unchanged: no re-read


def test_history_is_reread_after_five_minutes_when_the_conductor_moved(rig: Rig) -> None:
    rig.ticks(5)
    rig.now += 400
    rig.cao.set_status("b2ce9908", "completed", "2026-11-15T12:06:00.000000Z")
    mark = rig.mark()
    rig.tick()
    assert [c for c in rig.since(mark) if "inbox" in c[1]]


def test_history_counts_closed_helpers_against_the_live_fleet(rig: Rig) -> None:
    rig.ticks(5)
    view = rig.poller.history("payments-fix")
    assert [c["terminal_id"] for c in view["closed"]] == sorted(
        [c["terminal_id"] for c in view["closed"]],
        key=lambda t: [x["last_at"] for x in view["closed"] if x["terminal_id"] == t][0],
        reverse=True,
    )
    assert view == sup.load(sup.FIXTURES / "expected" / "busy" / "history.payments-fix.json")


def test_history_on_demand_reads_when_the_cache_is_old(rig: Rig) -> None:
    rig.ticks(5)
    rig.poller.history("payments-fix")
    n = rig.cao.count("GET", "/terminals/b2ce9908/inbox")
    rig.now += HISTORY_OPEN_MAX_AGE - 1
    rig.poller.history("payments-fix")
    assert (
        rig.cao.count("GET", "/terminals/b2ce9908/inbox") == n
    )  # cached: a burst of opens is cheap
    rig.now += 2
    rig.poller.history("payments-fix")
    assert rig.cao.count("GET", "/terminals/b2ce9908/inbox") == n + 1


def test_a_helper_closed_after_the_first_read_shows_when_the_drawer_opens(rig: Rig) -> None:
    rig.ticks(5)
    before = {c["terminal_id"] for c in rig.poller.history("payments-fix")["closed"]}
    terms = rig.cao.session_terminals("payments-fix")
    helper = next(t["id"] for t in terms[1:])
    # the helper wrote to its conductor, then was deleted: the terminal is gone, its row stays
    rig.cao.inbox["b2ce9908"].insert(
        0,
        {
            "id": 9001,
            "sender_id": helper,
            "receiver_id": "b2ce9908",
            "message": sup.SENTINEL,
            "status": "delivered",
            "created_at": "2026-11-15T12:00:00+00:00",
        },
    )
    terms[:] = [t for t in terms if t["id"] != helper]
    rig.cao.terminals.pop(helper)
    rig.now += HISTORY_OPEN_MAX_AGE + 1
    rig.ticks(2, advance=1)  # the polls see the terminal gone
    after = {c["terminal_id"] for c in rig.poller.history("payments-fix")["closed"]}
    assert helper in after - before


def test_all_history_refreshes_old_caches_four_sessions_at_a_time(rig: Rig) -> None:
    rig.ticks(5)
    rig.poller.history_all()
    n = len([c for c in rig.cao.calls if "inbox" in c[1]])
    rig.now += HISTORY_OPEN_MAX_AGE + 1
    rig.poller.history_all()
    assert len([c for c in rig.cao.calls if "inbox" in c[1]]) == n + 4
    rig.poller.history_all()  # the next call reads the next four (oldest caches first)
    assert len([c for c in rig.cao.calls if "inbox" in c[1]]) == n + 8


def test_history_for_an_unknown_session_is_not_found(rig: Rig) -> None:
    rig.ticks(2)
    with pytest.raises(ShelfError) as exc:
        rig.poller.history("no-such")
    assert exc.value.code == "not_found"
    with pytest.raises(ShelfError) as bad:
        rig.poller.history("../x")
    assert bad.value.code == "invalid_request"


def test_history_rows_are_live_cut_and_only_for_closed_helpers(rig: Rig) -> None:
    rig.ticks(5)
    conductor = "b2ce9908"
    closed = rig.poller.history("payments-fix")["closed"][0]["terminal_id"]
    rig.cao.inbox[conductor][0]["message"] = "A" * 5000 + "\x1b[31m"
    rig.cao.inbox[conductor][0]["sender_id"] = closed
    rows = rig.poller.history_rows("payments-fix", closed)["rows"]
    assert 1 <= len(rows) <= 3 and all(len(r["text"]) <= 2000 for r in rows)
    assert set(rows[0]) == {"id", "created_at", "status", "text"}
    with pytest.raises(ShelfError) as live:
        rig.poller.history_rows("payments-fix", "1b964846")  # a live helper
    assert live.value.code == "not_found"


def test_message_text_is_never_kept_in_the_cache_or_the_ledger(rig: Rig) -> None:
    for rows in rig.cao.inbox.values():
        for row in rows:
            row["message"] = sup.SENTINEL
    rig.ticks(5)
    assert sup.SENTINEL not in json.dumps(rig.poller.shelf())
    assert sup.SENTINEL not in json.dumps(rig.poller.history_all())
    assert sup.SENTINEL not in repr(rig.poller._st.history)
    assert sup.SENTINEL not in (rig.ledger._path).read_text()


def test_all_history_reads_four_at_most_and_says_when_incomplete(rig: Rig) -> None:
    rig.tick()  # a session list but no history yet beyond the first four
    rig.poller._st.history.clear()
    mark = rig.mark()
    agg = rig.poller.history_all()
    assert len([c for c in rig.since(mark) if "inbox" in c[1]]) == 4
    assert agg["complete"] is False
    rig.ticks(3)
    full = rig.poller.history_all()
    assert full["complete"] is True and len(full["closed"]) == 20
    assert full["closed"][0].keys() == {"session", "terminal_id", "rows", "last_at"}
    lasts = [c["last_at"] for c in full["closed"]]
    assert lasts == sorted(lasts, reverse=True)


def test_ledger_keeps_closed_helpers_after_the_window_moves_on(tmp_path: Path) -> None:
    r = Rig(tmp_path, "busy")
    r.ticks(5)
    before = r.poller.history("payments-fix")
    assert len(before["closed"]) == 3
    r.cao.inbox["b2ce9908"] = []  # CAO swept the rows
    r.now += 400
    r.cao.set_status("b2ce9908", "completed", "2026-11-15T12:06:00.000000Z")
    r.ticks(2)
    after = r.poller.history("payments-fix")
    assert len(after["closed"]) == 3 and after["rows_seen"] == 0


def test_persist_history_off_writes_no_ledger(tmp_path: Path) -> None:
    r = Rig(tmp_path, "busy", {"persist_history": False})
    r.ticks(5)
    assert not (tmp_path / "ledger.json").exists()
    assert r.poller.shelf()["history_total"] == 20


# --------------------------------------------------------------------------------- settings
def test_settings_changes_apply_on_the_next_tick(rig: Rig) -> None:
    rig.ticks(3)
    rig.settings["stale_hours"] = 2.0
    rig.settings["warm_hours"] = 0.25
    rig.tick()
    shelf = rig.poller.shelf()
    assert shelf["thresholds"] == {"warm_hours": 0.25, "stale_hours": 2.0}
    assert shelf["counts"]["by_tier"]["stale"] == 10  # all idle ones, now that 2 h is stale


def test_changing_the_address_starts_a_clean_cache(rig: Rig) -> None:
    rig.ticks(3)
    rig.settings["cao_url"] = "http://127.0.0.1:9890"
    rig.cao.mode = "down"
    rig.tick()
    shelf = rig.poller.shelf()
    assert shelf["sessions"] == [] and shelf["cao"]["base_url"] == "http://127.0.0.1:9890"


def test_bad_address_is_an_error_not_a_crash(rig: Rig) -> None:
    rig.settings["cao_url"] = "http://example.com"
    rig.tick()
    assert rig.poller.shelf()["cao"]["error"]["code"] == "forbidden_host"


def test_status_reports_counters(rig: Rig) -> None:
    rig.ticks(2)
    st = rig.poller.status()
    assert st["poller"]["requests_last_minute"] > 0 and st["poller"]["last_tick_at"].endswith("Z")
    assert st["cao_error"] is None and st["poller"]["running"] is False
    rig.cao.mode = "down"
    rig.tick()
    assert rig.poller.status()["cao_error"] == "cao_unreachable"


def test_the_poller_only_issues_gets(rig: Rig) -> None:
    rig.poller.mark_active()
    rig.ticks(6)
    rig.poller.history("payments-fix")
    rig.poller.history_all()
    assert rig.cao.writes() == []
