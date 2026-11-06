"""Keeping finished children and reaping them (#1225 phase 1), on a real SQLite store.

Reuses the ephemeral runtime fixtures, so the kept child is a real ephemeral launch
(caller ``CALLER``, session ``cao-session``): its name release, launch record and
snapshot files are the real ones. Only the backend, the provider and the status
reads are doubles.
"""

import json
from datetime import timedelta
from test.services.test_ephemeral_bind import (  # noqa: F401
    CHILD,
    assert_released,
    launch_runtime,
    release_runtime,
    runtime_store,
)
from test.services.test_ephemeral_claim import NOW, claimed_store  # noqa: F401
from test.services.test_ephemeral_service import CALLER, create_store  # noqa: F401
from types import SimpleNamespace

import pytest

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome
from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import keep_service, terminal_service

SESSION = "cao-session"


@pytest.fixture
def keep(release_runtime, monkeypatch):
    """The runtime, plus a settable clock, status table, keep setting and teardown log."""
    env, name, backend, factory = release_runtime
    # Every pane is provably live unless a test says otherwise (keep needs that proof).
    live = {}
    backend.cleanup_terminal_exact.side_effect = lambda tid, *a, **k: SimpleNamespace(
        outcome=live.get(tid, TerminalCleanupOutcome.STILL_PRESENT), reclaimed=False, detail=""
    )
    clock = [NOW]
    monkeypatch.setattr(database, "_utcnow", lambda: clock[0])
    statuses = {}
    monkeypatch.setattr(
        keep_service.status_monitor,
        "get_status",
        lambda tid: statuses.get(tid, TerminalStatus.IDLE),
    )
    calls = []
    real_snapshot = terminal_service.capture_terminal_snapshot
    real_dismantle = terminal_service.dismantle_terminal_runtime

    def snapshot(tid):
        calls.append(("snapshot", tid))
        return real_snapshot(tid)

    def exit_cli(tid):
        calls.append(("exit", tid))

    def dismantle(tid, metadata, kill_window=True):
        calls.append(("dismantle", tid))
        return real_dismantle(tid, metadata, kill_window)

    monkeypatch.setattr(terminal_service, "capture_terminal_snapshot", snapshot)
    monkeypatch.setattr(terminal_service, "exit_terminal_cli", exit_cli)
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", dismantle)
    config = env[4]

    def setting(**block):
        config["terminal"] = {"keep_finished": {"enabled": True, **block}}

    return SimpleNamespace(
        env=env,
        name=name,
        backend=backend,
        clock=clock,
        statuses=statuses,
        calls=calls,
        setting=setting,
        config=config,
        live=live,
    )


def _child(terminal_id, session=SESSION, caller=CALLER):
    database.create_terminal(
        terminal_id, session, f"w-{terminal_id}", "claude_code", caller_id=caller
    )


def _kept_ids(session=SESSION):
    return [row["terminal_id"] for row in database.list_kept_terminals(session)]


# --- Default (off) --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_off_by_default_keeps_nothing(keep):
    await launch_runtime(release_runtime_of(keep))
    outcome = keep_service.keep_finished(CHILD, reason="delete_terminal")
    assert outcome is keep_service.NOT_APPLICABLE
    assert database.list_kept_terminals() == []


@pytest.mark.asyncio
async def test_disabled_explicitly_keeps_nothing(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.config["terminal"] = {"keep_finished": {"enabled": False}}
    assert keep_service.keep_finished(CHILD, reason="handoff").kept is False
    assert database.get_kept_terminal(CHILD) is None


@pytest.mark.asyncio
async def test_invalid_setting_keeps_nothing(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.config["terminal"] = {"keep_finished": {"enabled": True, "on_cap": "nope"}}
    assert keep_service.keep_finished(CHILD, reason="handoff") is keep_service.NOT_APPLICABLE


def release_runtime_of(k):
    return (k.env, k.name, k.backend, None)


# --- The machine-wide setting ----------------------------------------------------------


@pytest.mark.asyncio
async def test_idle_child_is_kept_until_the_window_ends(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.setting(idle_window_minutes=30)
    outcome = keep_service.keep_finished(CHILD, reason="delete_terminal")
    assert outcome.kept is True
    assert outcome.kept_until == NOW + timedelta(minutes=30)
    # The note never claims a teardown; the caller reports what really happened.
    assert outcome.note == keep_service.KEPT_NOTE
    assert database.get_terminal_metadata(CHILD) is not None
    assert keep.calls == []  # nothing torn down
    keep.backend.kill_window.assert_not_called()
    shown = terminal_service.get_terminal(CHILD)
    assert shown["kept"] is True and shown["kept_until"] == NOW + timedelta(minutes=30)
    assert database.list_terminals_by_session(SESSION)[-1]["kept_until"] == outcome.kept_until


@pytest.mark.asyncio
async def test_keeping_twice_reports_the_first_window(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.setting()
    first = keep_service.keep_finished(CHILD, reason="handoff")
    keep.clock[0] = NOW + timedelta(minutes=10)
    again = keep_service.keep_finished(CHILD, reason="delete_terminal")
    assert again.kept is True and again.kept_until == first.kept_until


@pytest.mark.asyncio
async def test_a_busy_child_deleted_by_an_agent_is_not_kept(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.setting()
    keep.statuses[CHILD] = TerminalStatus.PROCESSING
    outcome = keep_service.keep_finished(CHILD, reason="delete_terminal")
    assert outcome.kept is False
    assert "still working" in outcome.note
    assert database.get_kept_terminal(CHILD) is None


def test_a_terminal_without_a_caller_is_not_a_child(keep):
    keep.setting()
    assert (
        keep_service.keep_finished(CALLER, reason="delete_terminal") is keep_service.NOT_APPLICABLE
    )


@pytest.mark.parametrize("outcome", [TerminalCleanupOutcome.ABSENT, TerminalCleanupOutcome.UNKNOWN])
def test_a_child_whose_pane_is_not_provably_live_is_not_kept(keep, outcome):
    """#1221: a failed create whose rollback retained the row (its pane is gone) is
    named by the 424 replay so it can be deleted. Keeping it would hide that delete."""
    keep.setting()
    _child("a0000001")
    keep.live["a0000001"] = outcome
    assert keep_service.keep_finished("a0000001", reason="delete_terminal") is (
        keep_service.NOT_APPLICABLE
    )
    assert database.list_kept_terminals() == []


def test_a_deferred_init_failure_tombstone_is_not_kept(keep):
    keep.setting()
    _child("a0000001")
    metadata = dict(database.get_terminal_metadata("a0000001"))
    metadata["deferred_init_failure"] = {"kind": "init_failed"}
    assert keep_service._runtime_is_live(metadata) is False
    metadata["deferred_init_failure"] = None
    assert keep_service._runtime_is_live(metadata) is True


def test_an_unprovable_liveness_check_reads_as_not_live(keep):
    keep.backend.cleanup_terminal_exact.side_effect = RuntimeError("tmux gone")
    keep.setting()
    _child("a0000001")
    assert keep_service.keep_finished("a0000001", reason="handoff") is keep_service.NOT_APPLICABLE


def test_a_missing_terminal_is_not_kept(keep):
    keep.setting()
    assert keep_service.keep_finished("deadbeef", reason="handoff") is keep_service.NOT_APPLICABLE


# --- The per-session cap -----------------------------------------------------------------


def _fill(k, ids, step_minutes=1):
    for i, tid in enumerate(ids):
        _child(tid)
        k.clock[0] = NOW + timedelta(minutes=i * step_minutes)
        assert keep_service.keep_finished(tid, reason="delete_terminal").kept is True


@pytest.mark.parametrize("on_cap", ["teardown_oldest", "dont_keep"])
def test_below_the_cap_every_child_is_kept(keep, on_cap):
    keep.setting(max_per_session=3, on_cap=on_cap)
    _fill(keep, ["a0000001", "a0000002", "a0000003"])
    assert _kept_ids() == ["a0000001", "a0000002", "a0000003"]


def test_dont_keep_at_the_cap_refuses_the_new_one(keep):
    keep.setting(max_per_session=2, on_cap="dont_keep")
    _fill(keep, ["a0000001", "a0000002"])
    _child("a0000003")
    outcome = keep_service.keep_finished("a0000003", reason="handoff")
    assert outcome.kept is False
    assert "max_per_session=2" in outcome.note and "dont_keep" in outcome.note
    assert _kept_ids() == ["a0000001", "a0000002"]
    assert keep.calls == []  # the caller tears the new one down, not this function


def test_teardown_oldest_at_the_cap_evicts_the_oldest_idle_one(keep):
    keep.setting(max_per_session=2)
    _fill(keep, ["a0000001", "a0000002"])
    _child("a0000003")
    keep.clock[0] = NOW + timedelta(minutes=5)
    outcome = keep_service.keep_finished("a0000003", reason="handoff")
    assert outcome.kept is True and outcome.evicted == ("a0000001",)
    assert "a0000001 is no longer kept and was torn down" in outcome.note
    assert "max_per_session=2" in outcome.note
    assert _kept_ids() == ["a0000002", "a0000003"]
    assert database.get_terminal_metadata("a0000001") is None
    assert keep.calls[:2] == [("snapshot", "a0000001"), ("exit", "a0000001")]


@pytest.mark.asyncio
async def test_an_evicted_ephemeral_child_ends_its_launch_and_releases_its_name(keep):
    env, name = keep.env, keep.name
    await launch_runtime(release_runtime_of(keep))
    keep.setting(max_per_session=1)
    assert keep_service.keep_finished(CHILD, reason="handoff").kept is True
    _child("a0000002")
    keep.clock[0] = NOW + timedelta(minutes=1)
    assert keep_service.keep_finished("a0000002", reason="handoff").evicted == (CHILD,)
    assert [c for c, _ in keep.calls] == ["snapshot", "exit", "dismantle"]
    record = database.get_launch_record(CHILD)
    assert record["end_reason"] == "kept_evicted" and record["ended_at"] is not None
    assert_released(env, name, "terminal_deleted")


def test_teardown_oldest_skips_a_busy_kept_child(keep):
    keep.setting(max_per_session=2)
    _fill(keep, ["a0000001", "a0000002"])
    keep.statuses["a0000001"] = TerminalStatus.WAITING_USER_ANSWER
    _child("a0000003")
    outcome = keep_service.keep_finished("a0000003", reason="handoff")
    assert outcome.evicted == ("a0000002",)
    assert sorted(_kept_ids()) == ["a0000001", "a0000003"]
    assert database.get_terminal_metadata("a0000001") is not None


def test_an_eviction_whose_teardown_is_deferred_is_not_reported_as_torn_down(keep, monkeypatch):
    keep.setting(max_per_session=1)
    _fill(keep, ["a0000001"])
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", lambda *a, **k: False)
    _child("a0000002")
    outcome = keep_service.keep_finished("a0000002", reason="handoff")
    assert outcome.kept is True and outcome.evicted == ("a0000001",)
    assert "a0000001 is no longer kept, but its teardown did not complete" in outcome.note
    assert "was torn down" not in outcome.note
    assert _kept_ids() == ["a0000002"]


def test_an_eviction_that_started_working_is_left_running_and_said_so(keep, monkeypatch):
    keep.setting(max_per_session=1)
    _fill(keep, ["a0000001"])
    reads = iter([TerminalStatus.IDLE, TerminalStatus.PROCESSING])
    monkeypatch.setattr(keep_service.status_monitor, "get_status", lambda tid: next(reads))
    _child("a0000002")
    outcome = keep_service.keep_finished("a0000002", reason="handoff")
    assert "started working again, so it was left running" in outcome.note
    assert database.get_terminal_metadata("a0000001") is not None
    assert keep.calls == []


def test_teardown_oldest_with_every_kept_child_busy_does_not_keep_the_new_one(keep):
    keep.setting(max_per_session=2)
    _fill(keep, ["a0000001", "a0000002"])
    keep.statuses.update(
        {"a0000001": TerminalStatus.PROCESSING, "a0000002": TerminalStatus.PROCESSING}
    )
    _child("a0000003")
    outcome = keep_service.keep_finished("a0000003", reason="handoff")
    assert outcome.kept is False and "none of them is idle" in outcome.note
    assert _kept_ids() == ["a0000001", "a0000002"]


def test_the_cap_is_per_session(keep):
    keep.setting(max_per_session=1, on_cap="dont_keep")
    _fill(keep, ["a0000001"])
    database.create_terminal("b0000001", "other", "w", "claude_code", caller_id=CALLER)
    assert keep_service.keep_finished("b0000001", reason="handoff").kept is True
    assert _kept_ids() == ["a0000001"] and _kept_ids("other") == ["b0000001"]


def test_the_cap_is_never_exceeded_over_many_keeps(keep):
    keep.setting(max_per_session=3)
    ids = [f"c000000{i}" for i in range(1, 9)]
    for i, tid in enumerate(ids):
        _child(tid)
        keep.clock[0] = NOW + timedelta(minutes=i)
        keep_service.keep_finished(tid, reason="handoff")
        assert len(_kept_ids()) <= 3
    assert _kept_ids() == ids[-3:]


# --- The reaper ----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reaper_tears_an_expired_idle_child_down_the_normal_way(keep):
    env, name = keep.env, keep.name
    await launch_runtime(release_runtime_of(keep))
    keep.backend.get_history.return_value = "what the child did\nand the tools it called"
    keep.setting(idle_window_minutes=10)
    keep_service.keep_finished(CHILD, reason="handoff")

    keep.clock[0] = NOW + timedelta(minutes=9)
    assert keep_service.reap() == {}
    assert database.get_terminal_metadata(CHILD) is not None

    keep.clock[0] = NOW + timedelta(minutes=10)
    assert keep_service.reap() == {"expired": 1}
    # Snapshot first, then the exit command, then the runtime and the row.
    assert [c for c, _ in keep.calls] == ["snapshot", "exit", "dismantle"]
    scrollback = terminal_service.TERMINAL_LOG_DIR / f"{CHILD}.scrollback"
    assert scrollback.read_text() == "what the child did\nand the tools it called"
    assert oct(scrollback.stat().st_mode & 0o777) == "0o600"
    snapshot = json.loads(
        (terminal_service.TERMINAL_LOG_DIR / f"{CHILD}.snapshot.json").read_text()
    )
    assert snapshot["caller_id"] == CALLER
    keep.backend.kill_window.assert_called_once()
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_kept_terminal(CHILD) is None
    record = database.get_launch_record(CHILD)
    assert record["end_reason"] == "kept_expired" and record["ended_at"] is not None
    assert_released(env, name, "terminal_deleted")


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [TerminalStatus.PROCESSING, TerminalStatus.WAITING_USER_ANSWER])
async def test_reaper_never_reaps_a_working_child_and_restarts_its_window(keep, busy):
    await launch_runtime(release_runtime_of(keep))
    keep.setting(idle_window_minutes=10)
    keep_service.keep_finished(CHILD, reason="handoff")
    keep.statuses[CHILD] = busy
    keep.clock[0] = NOW + timedelta(hours=5)
    assert keep_service.reap() == {"busy": 1}
    assert database.get_terminal_metadata(CHILD) is not None
    assert database.get_kept_terminal(CHILD)["kept_until"] == NOW + timedelta(hours=5, minutes=10)
    assert keep.calls == []
    # Idle again: the window counts from when CAO last saw it work.
    keep.statuses[CHILD] = TerminalStatus.IDLE
    keep.clock[0] = NOW + timedelta(hours=5, minutes=9)
    assert keep_service.reap() == {}
    keep.clock[0] = NOW + timedelta(hours=5, minutes=10)
    assert keep_service.reap() == {"expired": 1}


@pytest.mark.asyncio
async def test_reaper_leaves_a_child_whose_status_it_cannot_read(keep, monkeypatch):
    await launch_runtime(release_runtime_of(keep))
    keep.setting(idle_window_minutes=1)
    keep_service.keep_finished(CHILD, reason="handoff")

    def boom(_):
        raise RuntimeError("status backend down")

    monkeypatch.setattr(keep_service.status_monitor, "get_status", boom)
    keep.clock[0] = NOW + timedelta(hours=1)
    assert keep_service.reap() == {"unknown": 1}
    assert database.get_terminal_metadata(CHILD) is not None


@pytest.mark.asyncio
async def test_reaper_still_expires_children_after_the_setting_is_turned_off(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.setting(idle_window_minutes=5)
    keep_service.keep_finished(CHILD, reason="handoff")
    keep.config["terminal"] = {}
    keep.clock[0] = NOW + timedelta(minutes=5)
    assert keep_service.reap() == {"expired": 1}


def test_reaper_purges_marks_whose_terminal_is_gone(keep):
    keep.setting()
    _child("a0000001")
    keep_service.keep_finished("a0000001", reason="handoff")
    with database.SessionLocal() as db:  # a row removed behind CAO's back
        db.query(database.TerminalModel).filter(database.TerminalModel.id == "a0000001").delete()
        db.commit()
    assert keep_service.reap() == {"orphans": 1}
    assert database.list_kept_terminals() == []


def test_reaper_trims_a_session_back_to_a_lowered_cap(keep):
    keep.setting(max_per_session=3)
    _fill(keep, ["a0000001", "a0000002", "a0000003"])
    keep.setting(max_per_session=1)
    assert keep_service.reap() == {"evicted": 2}
    assert _kept_ids() == ["a0000003"]


def test_reaper_retries_a_deferred_teardown(keep, monkeypatch):
    keep.setting(idle_window_minutes=1)
    _child("a0000001")
    keep_service.keep_finished("a0000001", reason="handoff")
    monkeypatch.setattr(terminal_service, "dismantle_terminal_runtime", lambda *a, **k: False)
    keep.clock[0] = NOW + timedelta(minutes=2)
    assert keep_service.reap() == {"deferred": 1}
    assert _kept_ids() == ["a0000001"]


@pytest.mark.asyncio
async def test_kiro_style_private_home_survives_while_kept_and_goes_at_teardown(keep, monkeypatch):
    """A kept helper's private home is removed by its provider cleanup, which runs
    only in the final teardown, never when the child is kept."""
    await launch_runtime(release_runtime_of(keep))
    home = keep.env[2] / "kiro-home" / CHILD
    home.mkdir(parents=True)
    (home / "session.jsonl").write_text("{}")

    def cleanup(tid):
        assert tid == CHILD
        for path in sorted(home.rglob("*"), reverse=True):
            path.unlink()
        home.rmdir()
        return True

    monkeypatch.setattr(terminal_service.provider_manager, "cleanup_provider", cleanup)
    keep.setting(idle_window_minutes=1)
    assert keep_service.keep_finished(CHILD, reason="handoff").kept is True
    assert home.is_dir()
    keep.clock[0] = NOW + timedelta(minutes=1)
    keep_service.reap()
    assert not home.exists()


# --- Operator deletes and teardown paths drop the mark ---------------------------------


@pytest.mark.asyncio
async def test_an_operator_delete_of_a_kept_child_deletes_it_at_once(keep):
    await launch_runtime(release_runtime_of(keep))
    keep.setting()
    keep_service.keep_finished(CHILD, reason="handoff")
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted") is True
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_kept_terminal(CHILD) is None
    assert database.get_launch_record(CHILD)["end_reason"] == "deleted"


@pytest.mark.asyncio
async def test_session_teardown_drops_kept_marks(keep):
    from cli_agent_orchestrator.services import session_service

    await launch_runtime(release_runtime_of(keep))
    keep.setting()
    keep_service.keep_finished(CHILD, reason="handoff")
    assert SESSION in session_service.delete_session(SESSION)["deleted"]
    assert database.list_kept_terminals() == []


def test_bulk_and_by_session_deletes_drop_kept_marks(keep):
    keep.setting()
    _fill(keep, ["a0000001", "a0000002"])
    assert database.delete_terminals_by_ids(["a0000001"], end_reason="session_teardown") == 1
    assert _kept_ids() == ["a0000002"]
    database.delete_terminals_by_session(SESSION, end_reason="session_teardown")
    assert database.list_kept_terminals() == []


def test_mark_refuses_a_missing_terminal_and_evicts_nothing(keep):
    keep.setting()
    _fill(keep, ["a0000001"])
    assert (
        database.mark_terminal_kept(
            "deadbeef", SESSION, kept_until=NOW, reason="handoff", evict=["a0000001"]
        )
        is None
    )
    assert _kept_ids() == ["a0000001"]
    with pytest.raises(ValueError, match="unknown keep reason"):
        database.mark_terminal_kept("a0000001", SESSION, kept_until=NOW, reason="whim")


# --- Working again, stale windows and races -------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_delete_of_a_kept_child_that_is_working_again_deletes_it(keep):
    """Kept after its handoff, given a follow-up, now working: the supervisor's
    delete_terminal is a cancellation, so the mark is dropped and the delete runs."""
    await launch_runtime(release_runtime_of(keep))
    keep.setting()
    assert keep_service.keep_finished(CHILD, reason="handoff").kept is True
    keep.statuses[CHILD] = TerminalStatus.PROCESSING
    outcome = keep_service.keep_finished(CHILD, reason="delete_terminal")
    assert outcome.kept is False and outcome.kept_until is None
    assert outcome.note == "Not kept: it was still working."
    assert database.get_kept_terminal(CHILD) is None


@pytest.mark.asyncio
async def test_the_route_deletes_a_kept_child_that_is_working_again(keep, monkeypatch):
    """End to end: DELETE ?keep_if_enabled=true on a kept, busy child deletes it."""
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    await launch_runtime(release_runtime_of(keep))
    keep.setting()
    keep_service.keep_finished(CHILD, reason="handoff")
    keep.statuses[CHILD] = TerminalStatus.PROCESSING
    response = TestClient(main.app, base_url="http://localhost").delete(
        "/terminals/" + CHILD, params={"keep_if_enabled": "true"}
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "keep_note": "Not kept: it was still working."}
    assert database.get_terminal_metadata(CHILD) is None
    assert database.get_launch_record(CHILD)["end_reason"] == "deleted"


def test_a_mark_whose_window_has_ended_is_never_reported(keep):
    """No stale kept_until: an expired, not-yet-reaped mark is dropped and the delete runs."""
    keep.setting(idle_window_minutes=10)
    _child("a0000001")
    keep_service.keep_finished("a0000001", reason="handoff")
    keep.clock[0] = NOW + timedelta(minutes=10)
    outcome = keep_service.keep_finished("a0000001", reason="delete_terminal")
    assert outcome.kept is False and outcome.kept_until is None
    assert outcome.note == "Not kept: its keep window had ended."
    assert database.get_kept_terminal("a0000001") is None


def test_a_mark_still_in_its_window_reports_that_window(keep):
    keep.setting(idle_window_minutes=10)
    _child("a0000001")
    first = keep_service.keep_finished("a0000001", reason="handoff")
    keep.clock[0] = NOW + timedelta(minutes=9)
    again = keep_service.keep_finished("a0000001", reason="delete_terminal")
    assert again.kept is True and again.kept_until == first.kept_until > keep.clock[0]


def test_trimming_a_lowered_cap_skips_a_busy_oldest_child(keep):
    """A cap lowered from 3 to 1 with the oldest kept child busy. The busy one keeps
    its mark (it would otherwise be left running, unmarked, and never reaped); the two
    idle ones go."""
    keep.setting(max_per_session=3)
    _fill(keep, ["a0000001", "a0000002", "a0000003"])
    keep.statuses["a0000001"] = TerminalStatus.PROCESSING
    keep.setting(max_per_session=1)
    assert keep_service.reap() == {"busy": 1, "evicted": 2}
    assert _kept_ids() == ["a0000001"]
    assert database.get_terminal_metadata("a0000001") is not None
    assert database.get_terminal_metadata("a0000002") is None
    assert database.get_terminal_metadata("a0000003") is None


@pytest.mark.parametrize("cap", [1, 2])
def test_a_duplicate_delete_racing_a_keep_evicts_nothing(keep, monkeypatch, cap):
    """A retried delete_terminal(T) whose existence check ran before the first
    call's mark landed must neither evict T itself (cap 1) nor an unrelated older
    child (cap 2), and must report T's real mark."""
    keep.setting(max_per_session=cap)
    _fill(keep, ["a0000001"])  # older, unrelated
    _child("a0000002")
    keep.clock[0] = NOW + timedelta(minutes=5)
    if cap == 1:
        database.unmark_terminal_kept("a0000001")
    first = keep_service.keep_finished("a0000002", reason="delete_terminal")
    assert first.kept is True
    before = _kept_ids()
    # The second call's existence read is stale: it saw no mark.
    real = database.get_kept_terminal
    reads = []

    def stale_once(tid):
        reads.append(tid)
        return None if len(reads) == 1 else real(tid)

    monkeypatch.setattr(database, "get_kept_terminal", stale_once)
    keep.calls.clear()
    second = keep_service.keep_finished("a0000002", reason="delete_terminal")
    assert second.kept is True and second.kept_until == first.kept_until
    assert second.evicted == ()
    assert keep.calls == []
    assert _kept_ids() == before
    assert database.get_terminal_metadata("a0000002") is not None
    assert database.get_terminal_metadata("a0000001") is not None


def test_mark_reports_only_the_marks_it_removed(keep):
    keep.setting()
    _fill(keep, ["a0000001"])
    _child("a0000002")
    mark = database.mark_terminal_kept(
        "a0000002",
        SESSION,
        kept_until=NOW,
        reason="handoff",
        evict=["a0000001", "deadbeef", "a0000002"],
    )
    assert mark.written is True and mark.evicted == ("a0000001",)
    again = database.mark_terminal_kept(
        "a0000002", SESSION, kept_until=NOW + timedelta(hours=1), reason="handoff", evict=["x"]
    )
    assert again.written is False and again.kept_until == NOW and again.evicted == ()


def test_an_off_pass_only_reads(keep, monkeypatch):
    """Nothing kept (always so while the setting is off): no purge write, no settings read."""
    monkeypatch.setattr(
        database, "delete_orphan_kept_rows", lambda: pytest.fail("purged an empty table")
    )
    monkeypatch.setattr(keep_service, "current_settings", lambda: pytest.fail("read settings"))
    assert keep_service.reap() == {}


def test_an_invalid_block_warns_once_per_distinct_error(keep, caplog, monkeypatch):
    monkeypatch.setattr(keep_service, "_last_invalid_setting", None)
    keep.config["terminal"] = {"keep_finished": {"enabled": True, "on_cap": "nope"}}
    with caplog.at_level("DEBUG"):
        for _ in range(3):
            keep_service.current_settings()
        keep.config["terminal"] = {"keep_finished": {"enabled": True, "max_per_session": 0}}
        keep_service.current_settings()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2
    assert "on_cap" in warnings[0] and "max_per_session" in warnings[1]


def test_a_victim_whose_mark_is_already_gone_is_not_torn_down(keep, monkeypatch):
    """Only marks the database really removed are torn down. Here the cap count sees a
    kept row whose mark something outside the lock (an operator delete, the reaper)
    already removed: there is nothing to evict, so no teardown and no eviction sentence."""
    keep.setting(max_per_session=1)
    _child("a0000001")  # its mark is gone; its row is still there
    _child("a0000002")
    phantom = {
        "terminal_id": "a0000001",
        "session_name": SESSION,
        "kept_at": NOW,
        "kept_until": NOW + timedelta(hours=1),
        "reason": "handoff",
    }
    real = database.list_kept_terminals
    monkeypatch.setattr(
        database,
        "list_kept_terminals",
        lambda session_name=None: [phantom] if session_name == SESSION else real(session_name),
    )
    outcome = keep_service.keep_finished("a0000002", reason="handoff")
    assert outcome.kept is True and outcome.evicted == ()
    assert outcome.note == keep_service.KEPT_NOTE
    assert keep.calls == []
    assert database.get_terminal_metadata("a0000001") is not None


def test_trimming_never_tears_down_a_child_whose_mark_it_did_not_remove(keep, monkeypatch):
    """If something else removed the mark first, trimming leaves that child alone."""
    keep.setting(max_per_session=2)
    _fill(keep, ["a0000001", "a0000002"])
    keep.setting(max_per_session=1)
    monkeypatch.setattr(database, "unmark_terminal_kept", lambda terminal_id: False)
    assert "evicted" not in keep_service.reap()
    assert keep.calls == []
    assert database.get_terminal_metadata("a0000001") is not None
    assert database.get_terminal_metadata("a0000002") is not None


def test_the_same_invalid_block_warns_again_after_it_was_fixed(keep, caplog, monkeypatch):
    """Fix a typo, then make it again: the second time is reported too."""
    monkeypatch.setattr(keep_service, "_last_invalid_setting", None)
    bad = {"keep_finished": {"enabled": True, "on_cap": "nope"}}
    with caplog.at_level("DEBUG"):
        keep.config["terminal"] = bad
        keep_service.current_settings()
        keep.config["terminal"] = {"keep_finished": {"enabled": True}}
        assert keep_service.current_settings().enabled is True
        keep.config["terminal"] = bad
        keep_service.current_settings()
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2 and all("on_cap" in w for w in warnings)
