"""HerdrAppInboxService: herdr-core events drive delivery, status, teardown, naming."""

import asyncio
from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import herdr_app_inbox_service as mod
from cli_agent_orchestrator.services.herdr_app_inbox_service import HerdrAppInboxService

HOME = "a" * 16
FOREIGN = "f" * 16
MANAGED = {"cao.managed": "1", "cao.home": HOME, "cao.session": "cao-a", "cao.window": "w1"}


def rec(address, tid, **labels):
    return {
        "address": address,
        "state": "live",
        "labels": {**MANAGED, "cao.terminal_id": tid, **labels},
    }


def ev(seq, etype, terminal=None, **data):
    params = {"seq": seq, "type": etype, "data": data}
    if terminal:
        params["terminal"] = terminal
    return {"jsonrpc": "2.0", "method": "event", "params": params}


def status(seq, terminal, state):
    return ev(
        seq, "status", terminal, status=({"app": "kiro-cli", "state": state} if state else None)
    )


@pytest.fixture
def env(monkeypatch):
    backend = MagicMock()
    backend.herdr_app_session = "cao"
    backend.home_label = HOME
    backend.list_managed.return_value = []
    deliver = MagicMock()
    deleted = []
    monkeypatch.setattr(mod, "_teardown", lambda tid, reason: deleted.append((tid, reason)))
    recorded = []
    monkeypatch.setattr(mod, "_record_status", lambda tid, st: recorded.append((tid, st)))
    activity = []
    monkeypatch.setattr(mod, "_note_activity", lambda tid: activity.append(tid))
    svc = HerdrAppInboxService(backend, deliver)
    svc.load_snapshot(
        {
            "seq": 0,
            "terminals": [
                rec("t1", "aaaa0001"),
                {"address": "t9", "labels": {}},
                rec("t8", "ffff0008", **{"cao.home": FOREIGN}),
            ],
        }
    )
    return svc, backend, deliver, deleted, recorded, activity


def run(svc, *messages):
    async def go():
        for m in messages:
            await svc.handle(m)
        await svc.drain()

    asyncio.run(go())


def test_snapshot_maps_only_our_managed_terminals(env):
    svc = env[0]
    assert svc.mapped() == {"t1": "aaaa0001"}


def test_idle_and_done_deliver(env):
    svc, _, deliver, _, recorded, _ = env
    run(svc, status(1, "t1", "working"), status(2, "t1", "done"), status(3, "t1", "idle"))
    assert [c.args[0] for c in deliver.call_args_list] == ["aaaa0001", "aaaa0001"]
    assert recorded == [
        ("aaaa0001", TerminalStatus.PROCESSING),
        ("aaaa0001", TerminalStatus.COMPLETED),
        ("aaaa0001", TerminalStatus.IDLE),
    ]


def test_slow_delivery_does_not_hold_up_other_events(env):
    import threading

    svc, _, deliver, deleted, _, _ = env
    release = threading.Event()
    seen = []

    def slow(tid):
        seen.append(tid)
        assert release.wait(5)

    deliver.side_effect = slow

    async def go():
        await svc.handle(ev(1, "created", "t2", **rec("t2", "aaaa0002")))
        await svc.handle(status(2, "t1", "idle"))  # blocks in terminal.send
        await svc.handle(ev(3, "finished", "t2"))
        assert deleted == [("aaaa0002", "terminal_finished")]
        await svc.handle(status(4, "t1", "idle"))  # coalesced behind the first
        await svc.handle(status(5, "t1", "done"))
        release.set()
        await svc.drain()

    asyncio.run(go())
    assert seen == ["aaaa0001", "aaaa0001"]  # one in flight, one rerun


def test_settle_retry_outlasts_the_provider_flush_wait():
    # providers/base.py waits 10 s after native idle/done before COMPLETED.
    assert mod.SETTLE_RETRY_S > 10.0


class TestSettleRetry:
    """After idle/done, the provider reports PROCESSING for its 10 s flush wait,
    so the immediate delivery finds the terminal busy; one more delivery runs
    once that wait is over."""

    @pytest.fixture(autouse=True)
    def fast(self, monkeypatch):
        monkeypatch.setattr(mod, "SETTLE_RETRY_S", 0.05)

    @staticmethod
    def run_and_wait(svc, *messages):
        async def go():
            for m in messages:
                await svc.handle(m)
            await asyncio.sleep(0.3)
            await svc.drain()

        asyncio.run(go())

    def test_delivers_again_after_the_flush_wait(self, env):
        svc, _, deliver, *_ = env
        self.run_and_wait(svc, status(1, "t1", "idle"))
        assert [c.args[0] for c in deliver.call_args_list] == ["aaaa0001", "aaaa0001"]

    @pytest.mark.parametrize("state", ["working", "blocked"])
    def test_new_work_cancels_the_retry(self, env, state):
        svc, _, deliver, *_ = env
        self.run_and_wait(svc, status(1, "t1", "idle"), status(2, "t1", state))
        assert deliver.call_count == 1

    def test_exit_cancels_the_retry(self, env):
        svc, _, deliver, *_ = env
        self.run_and_wait(svc, status(1, "t1", "idle"), ev(2, "finished", "t1"))
        assert deliver.call_count == 1

    def test_repeated_idle_keeps_one_retry(self, env):
        svc, _, deliver, *_ = env

        async def go():
            await svc.handle(status(1, "t1", "idle"))
            await svc.drain()
            await svc.handle(status(2, "t1", "done"))
            await svc.drain()
            assert len(svc._settling) == 1
            await asyncio.sleep(0.3)
            await svc.drain()

        asyncio.run(go())
        assert deliver.call_count == 3  # two immediate, one retry


def test_working_and_blocked_note_activity(env):
    svc, *_, activity = env
    run(svc, status(1, "t1", "working"), status(2, "t1", "blocked"))
    assert activity == ["aaaa0001", "aaaa0001"]


def test_same_state_recorded_once(env):
    svc, _, _, _, recorded, _ = env
    run(svc, status(1, "t1", "working"), status(2, "t1", "working"))
    assert len(recorded) == 1


def test_native_error_is_neither_recorded_nor_delivered(env):
    # herdr-core keeps `error` after a failed turn while the agent waits at its
    # prompt again; the provider's screen analysis is the judge, not this word.
    svc, _, deliver, _, recorded, _ = env
    run(svc, status(1, "t1", "working"), status(2, "t1", "error"))
    assert recorded == [("aaaa0001", TerminalStatus.PROCESSING)]
    assert not deliver.called
    # The next real idle after the error still records and delivers.
    run(svc, status(3, "t1", "idle"))
    assert recorded[-1] == ("aaaa0001", TerminalStatus.IDLE)
    assert deliver.called


def test_cleared_status_records_nothing(env):
    svc, _, deliver, _, recorded, _ = env
    run(svc, status(1, "t1", None))
    assert recorded == [] and not deliver.called


@pytest.mark.parametrize("address", ["t9", "t8", "t404"])
def test_unmapped_or_foreign_terminal_ignored(env, address):
    svc, _, deliver, deleted, recorded, _ = env
    run(svc, status(1, address, "idle"), ev(2, "finished", address))
    deliver.assert_not_called()
    assert deleted == [] and recorded == []


def test_failed_delivery_does_not_stop_the_stream(env):
    svc, _, deliver, *_ = env
    deliver.side_effect = [RuntimeError("db down"), None]
    run(svc, status(1, "t1", "idle"), status(2, "t1", "done"))
    assert deliver.call_count == 2


@pytest.mark.parametrize(
    "etype,reason", [("finished", "terminal_finished"), ("lost", "terminal_lost")]
)
def test_exit_tears_down_and_removes_the_terminal(env, etype, reason):
    svc, backend, _, deleted, _, _ = env
    run(svc, ev(1, etype, "t1", reason="exited"))
    assert deleted == [("aaaa0001", reason)]
    assert svc.mapped() == {}
    # A finished/lost terminal is no longer live, so the backend's kill_window
    # cannot find it by label; the service removes it by address.
    backend.kill_address.assert_called_once_with("t1")


def test_failed_teardown_does_not_stop_the_stream(env, monkeypatch):
    svc, _, deliver, *_ = env

    def boom(tid, reason):
        raise RuntimeError("teardown failed")

    monkeypatch.setattr(mod, "_teardown", boom)
    run(svc, ev(1, "finished", "t1"), ev(2, "created", "t2", **rec("t2", "aaaa0002")))
    run(svc, status(3, "t2", "idle"))
    deliver.assert_called_once_with("aaaa0002")


def test_restarted_shell_after_teardown_is_killed(env):
    svc, backend, *_ = env
    run(svc, ev(1, "lost", "t1"))
    backend.kill_address.reset_mock()
    run(svc, ev(2, "restarted", "t1", **rec("t1", "aaaa0001")))
    backend.kill_address.assert_called_once_with("t1")


def test_restarted_foreign_terminal_is_left_alone(env):
    svc, backend, *_ = env
    run(svc, ev(1, "restarted", "t8", **rec("t8", "ffff0008", **{"cao.home": FOREIGN})))
    backend.kill_address.assert_not_called()
    assert "t8" not in svc.mapped()


def test_created_maps_new_terminal(env):
    svc = env[0]
    run(svc, ev(1, "created", "t2", **rec("t2", "aaaa0002")))
    assert svc.mapped()["t2"] == "aaaa0002"


def test_created_foreign_terminal_is_not_mapped(env):
    svc = env[0]
    run(svc, ev(1, "created", "t3", **rec("t3", "ffff0003", **{"cao.home": FOREIGN})))
    assert "t3" not in svc.mapped()


def test_labels_event_with_record_maps(env):
    svc = env[0]
    run(svc, ev(1, "labels", "t4", labels={}, record=rec("t4", "aaaa0004")))
    assert svc.mapped()["t4"] == "aaaa0004"


def test_removed_drops_mapping(env):
    svc, backend = env[0], env[1]
    run(svc, ev(1, "removed", "t1"))
    assert svc.mapped() == {}
    # Removed outside CAO: its input connection (an `hc connect` child) is closed.
    backend.release_input.assert_called_once_with("t1")


def test_register_and_unregister(env):
    svc = env[0]
    svc.register_terminal("aaaa0005", "t5", is_kiro=True)
    assert svc.mapped()["t5"] == "aaaa0005"
    svc.unregister_terminal("aaaa0005")
    assert "t5" not in svc.mapped()


class TestCallsFromWorkerThreads:
    """terminal_service registers and unregisters from worker threads (the API
    runs delete_terminal in asyncio.to_thread) while the loop handles events."""

    @pytest.fixture(autouse=True)
    def fast(self, monkeypatch):
        monkeypatch.setattr(mod, "SETTLE_RETRY_S", 30)

    def test_unregister_off_loop_unmaps_now_and_cancels_on_the_loop(self, env):
        import threading

        svc, _, deliver, *_ = env
        cancelled_on = []
        real_cancel = svc._cancel_settle

        def spy(tid):
            cancelled_on.append(threading.get_ident())
            real_cancel(tid)

        svc._cancel_settle = spy

        async def go():
            # The loop the service runs on; start() records it.
            svc._loop = asyncio.get_running_loop()
            await svc.handle(status(1, "t1", "idle"))
            await svc.drain()
            settle = svc._settling["aaaa0001"]
            cancelled_on.clear()
            worker = threading.Thread(target=svc.unregister_terminal, args=("aaaa0001",))
            worker.start()
            worker.join(5)
            # The map is updated before unregister returns, so the `finished`
            # event of the kill that follows it is never mapped.
            assert svc.mapped() == {}
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(settle, 1)
            return threading.get_ident()

        loop_thread = asyncio.run(go())
        assert cancelled_on == [loop_thread]
        assert deliver.call_count == 1

    def test_register_off_loop(self, env):
        import threading

        svc = env[0]

        async def go():
            svc._loop = asyncio.get_running_loop()
            worker = threading.Thread(target=svc.register_terminal, args=("aaaa0007", "t7"))
            worker.start()
            worker.join(5)
            await svc.handle(status(1, "t7", "idle"))
            await svc.drain()

        asyncio.run(go())
        env[2].assert_any_call("aaaa0007")


def test_doc_event_runs_naming(env):
    svc, backend, *_ = env
    run(svc, ev(1, "doc", None, name="herdr-app", version=3))
    backend.sync_layout_names.assert_called_once()


def test_other_doc_event_is_ignored(env):
    svc, backend, *_ = env
    run(svc, ev(1, "doc", None, name="something-else", version=3))
    backend.sync_layout_names.assert_not_called()


def test_heartbeat_is_ignored(env):
    svc, backend, deliver, *_ = env
    run(svc, {"jsonrpc": "2.0", "method": "heartbeat", "params": {"seq": 4}})
    deliver.assert_not_called()


def test_missed_replaces_map_and_drops_stale(env, monkeypatch):
    svc, backend, deliver, *_ = env
    monkeypatch.setattr(svc, "_reconcile", lambda: None)
    run(
        svc,
        {
            "jsonrpc": "2.0",
            "method": "missed",
            "params": {"seq": 10, "terminals": [rec("t3", "aaaa0003")]},
        },
        status(9, "t3", "idle"),
        status(11, "t3", "idle"),
    )
    assert svc.mapped() == {"t3": "aaaa0003"}
    assert deliver.call_count == 1
    backend.sync_layout_names.assert_called_once()


def test_missed_reconciles_with_the_new_map(env, monkeypatch):
    # A terminal that finished during the gap is in no snapshot and its
    # `finished` event never arrived: only reconcile can drop its row.
    svc, backend, *_ = env
    order = []
    monkeypatch.setattr(svc, "_reconcile", lambda: order.append(("reconcile", svc.mapped())))
    backend.sync_layout_names.side_effect = lambda: order.append(("names", None))
    missed = {"seq": 10, "terminals": [rec("t3", "aaaa0003")]}
    run(svc, {"jsonrpc": "2.0", "method": "missed", "params": missed})
    assert order == [("reconcile", {"t3": "aaaa0003"}), ("names", None)]


class _Sub:
    def __init__(self, messages, snapshot=None):
        self.snapshot = snapshot or {"seq": 0, "terminals": []}
        self._messages = list(messages)
        self.closed = False

    def next_notification(self, timeout):
        if not self._messages:
            raise EOFError
        m = self._messages.pop(0)
        if isinstance(m, Exception):
            raise m
        return m

    def close(self):
        self.closed = True


def test_reconnect_backoff(env, monkeypatch):
    svc = env[0]
    sleeps = []
    subs = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(svc, "_reconcile", lambda: None)

    def subscribe():
        subs.append(_Sub([TimeoutError()]))
        return subs[-1]

    svc._subscribe = subscribe
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.start())
    assert sleeps == [1, 2, 4, 8, 16, 30]
    assert all(s.closed for s in subs)


def test_backoff_resets_after_a_notification(env, monkeypatch):
    svc = env[0]
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(svc, "_reconcile", lambda: None)
    batches = [[EOFError()], [{"jsonrpc": "2.0", "method": "heartbeat", "params": {}}], []]
    svc._subscribe = lambda: _Sub(batches.pop(0))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.start())
    assert sleeps == [1, 1, 2]


def test_cancel_during_backoff_cancels_pending_tasks(env, monkeypatch):
    svc = env[0]

    async def cancelled_sleep(s):
        raise asyncio.CancelledError  # cancelled while waiting to reconnect

    def subscribe():
        raise RuntimeError("core down")

    svc._subscribe = subscribe

    async def go():
        delivery = asyncio.create_task(asyncio.Event().wait())
        settle = asyncio.create_task(asyncio.Event().wait())
        svc._delivering["aaaa0001"] = delivery
        svc._settling["aaaa0001"] = settle
        monkeypatch.setattr(mod.asyncio, "sleep", cancelled_sleep)
        with pytest.raises(asyncio.CancelledError):
            await svc.start()
        for task in (delivery, settle):
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)

    asyncio.run(go())


def test_stream_read_uses_the_silence_limit(env, monkeypatch):
    svc = env[0]
    timeouts = []

    class RecordingSub(_Sub):
        def next_notification(self, timeout):
            timeouts.append(timeout)
            return super().next_notification(timeout)

    async def fake_sleep(s):
        raise asyncio.CancelledError

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(svc, "_reconcile", lambda: None)
    heartbeat = {"jsonrpc": "2.0", "method": "heartbeat", "params": {}}
    svc._subscribe = lambda: RecordingSub([heartbeat])
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.start())
    assert timeouts == [mod.SILENCE_LIMIT_S, mod.SILENCE_LIMIT_S]
    assert mod.SILENCE_LIMIT_S == 45.0  # three 15 s heartbeats


def test_failed_subscribe_backs_off(env, monkeypatch):
    svc = env[0]
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)

    def subscribe():
        raise RuntimeError("core down")

    svc._subscribe = subscribe
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.start())
    assert sleeps == [1, 2]


def test_connect_loads_snapshot_reconciles_and_names(env, monkeypatch):
    svc, backend, deliver, *_ = env
    reconciled = []

    async def fake_sleep(s):
        raise asyncio.CancelledError

    monkeypatch.setattr(mod.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(svc, "_reconcile", lambda: reconciled.append(dict(svc.mapped())))
    snap = {"seq": 5, "terminals": [rec("t6", "aaaa0006")]}
    svc._subscribe = lambda: _Sub([status(5, "t6", "idle"), status(6, "t6", "idle")], snap)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(svc.start())
    assert reconciled == [{"t6": "aaaa0006"}]
    backend.sync_layout_names.assert_called_once()
    deliver.assert_called_once_with("aaaa0006")  # seq 5 is not above the snapshot


def finished(address, tid, **labels):
    return {**rec(address, tid, **labels), "state": "finished"}


def core_with(backend, *records):
    """list_managed() returns the live records; include_finished=True returns all."""

    def list_managed(include_finished=False):
        return [r for r in records if include_finished or r["state"] == "live"]

    backend.list_managed.side_effect = list_managed


class TestReconcile:
    def test_drops_finished_rows_and_kills_orphans(self, env, monkeypatch):
        svc, backend, _, deleted, _, _ = env
        core_with(backend, rec("t1", "aaaa0001"), rec("t5", "orphan01"), finished("t6", "ghost001"))
        rows = [
            {"id": "aaaa0001", "tmux_session": "cao-a"},
            {"id": "ghost001", "tmux_session": "cao-a"},
        ]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows)
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == ["ghost001"]
        backend.kill_address.assert_called_once_with("t5")

    def test_session_with_only_finished_terminals(self, env, monkeypatch):
        svc, backend, *_ = env
        core_with(backend, finished("t7", "ghost002", **{"cao.session": "cao-gone"}))
        rows = [{"id": "ghost002", "tmux_session": "cao-gone"}]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-gone": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows if s == "cao-gone" else [])
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == ["ghost002"]
        backend.kill_address.assert_not_called()

    def test_rows_from_another_backend_are_untouched(self, env, monkeypatch):
        # Switching from tmux or the herdr TUI: the core has never heard of
        # these terminals, so their rows are not ours to drop.
        svc, backend, *_ = env
        core_with(backend, rec("t1", "aaaa0001"))
        rows = [
            {"id": "aaaa0001", "tmux_session": "cao-a"},
            {"id": "tmux0001", "tmux_session": "cao-a"},
        ]
        tui = [{"id": "tui00001", "tmux_session": "cao-tui"}]
        by_session = {"cao-a": rows, "cao-tui": tui}
        monkeypatch.setattr(mod, "_rows_by_session", lambda: by_session)
        monkeypatch.setattr(mod, "_rows_for", lambda s: by_session.get(s, []))
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == []
        backend.kill_address.assert_not_called()

    def test_finished_record_of_another_home_does_not_count(self, env, monkeypatch):
        svc, backend, *_ = env
        core_with(backend, finished("t8", "ghost003", **{"cao.home": FOREIGN}))
        rows = [{"id": "ghost003", "tmux_session": "cao-a"}]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows)
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == []

    def test_row_with_a_live_record_among_finished_ones_stays(self, env, monkeypatch):
        svc, backend, *_ = env
        core_with(backend, finished("t2", "aaaa0001"), rec("t3", "aaaa0001"))
        rows = [{"id": "aaaa0001", "tmux_session": "cao-a"}]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows)
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == []

    def test_finished_and_unknown_state_records_keep_the_row(self, env, monkeypatch):
        svc, backend, *_ = env
        core_with(backend, finished("t2", "aaaa0001"), {**rec("t3", "aaaa0001"), "state": "parked"})
        rows = [{"id": "aaaa0001", "tmux_session": "cao-a"}]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows)
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == []

    def test_record_in_an_unknown_state_is_not_finished(self, env, monkeypatch):
        svc, backend, *_ = env
        core_with(backend, {**rec("t2", "aaaa0001"), "state": "parked"})
        rows = [{"id": "aaaa0001", "tmux_session": "cao-a"}]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows)
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == []

    def test_finished_lookup_failure_drops_nothing(self, env, monkeypatch):
        svc, backend, *_ = env

        def list_managed(include_finished=False):
            if include_finished:
                raise RuntimeError("down")
            return [rec("t5", "orphan01")]

        backend.list_managed.side_effect = list_managed
        rows = [{"id": "ghost001", "tmux_session": "cao-a"}]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": rows})
        monkeypatch.setattr(mod, "_rows_for", lambda s: rows)
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == [] and not backend.kill_address.called

    def test_holds_the_session_lock(self, env, monkeypatch):
        svc, backend, *_ = env
        held = []
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": []})

        def rows_for(session):
            from cli_agent_orchestrator.services import session_lock

            entry = session_lock._session_locks.get(session)
            held.append(entry is not None and entry[0].locked())
            return []

        monkeypatch.setattr(mod, "_rows_for", rows_for)
        svc._reconcile()
        assert held == [True]

    def test_skips_on_lookup_failure(self, env, monkeypatch):
        svc, backend, *_ = env
        backend.list_managed.side_effect = RuntimeError("down")
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": [{"id": "ghost001"}]})
        monkeypatch.setattr(mod, "_rows_for", lambda s: [{"id": "ghost001"}])
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == [] and not backend.kill_address.called

    def test_skips_session_when_fresh_lookup_fails(self, env, monkeypatch):
        svc, backend, *_ = env
        backend.list_managed.side_effect = [[], RuntimeError("down")]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {"cao-a": [{"id": "ghost001"}]})
        monkeypatch.setattr(mod, "_rows_for", lambda s: [{"id": "ghost001"}])
        ghosts = []
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: ghosts.append(tid))
        svc._reconcile()
        assert ghosts == []

    def test_foreign_terminals_are_never_orphans(self, env, monkeypatch):
        # list_managed already filters by cao.home; a record that slipped
        # through with another home is still not ours to kill.
        svc, backend, *_ = env
        backend.list_managed.return_value = [rec("t8", "ffff0008", **{"cao.home": FOREIGN})]
        monkeypatch.setattr(mod, "_rows_by_session", lambda: {})
        monkeypatch.setattr(mod, "_rows_for", lambda s: [])
        monkeypatch.setattr(mod, "_drop_ghost_row", lambda tid: None)
        svc._reconcile()
        backend.kill_address.assert_not_called()


class TestTeardown:
    """_teardown: the real seam behind a `finished` / `lost` event."""

    @pytest.fixture
    def db(self, monkeypatch):
        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.services import herdr_inbox_service, terminal_service

        rows = {"aaaa0001": {"id": "aaaa0001", "tmux_session": "cao-a"}}
        monkeypatch.setattr(database, "terminal_exists", lambda tid: tid in rows)
        monkeypatch.setattr(database, "get_terminal_metadata", lambda tid: rows.get(tid))
        retained = []
        monkeypatch.setattr(
            herdr_inbox_service,
            "_retain_deferred_failure_tombstone",
            lambda tid: tid in retained,
        )
        deleted = []

        def delete_terminal(tid, **kwargs):
            from cli_agent_orchestrator.services import session_lock

            entry = session_lock._session_locks.get("cao-a")
            deleted.append((tid, kwargs, entry is not None and entry[0].locked()))

        monkeypatch.setattr(terminal_service, "delete_terminal", delete_terminal)
        return rows, retained, deleted

    def test_retains_deferred_tombstone(self, db):
        _, retained, deleted = db
        retained.append("aaaa0001")
        mod._teardown("aaaa0001", "terminal_finished")
        assert deleted == []

    def test_deletes_with_end_reason_under_the_session_lock(self, db):
        _, _, deleted = db
        mod._teardown("aaaa0001", "terminal_lost")
        assert deleted == [("aaaa0001", {"end_reason": "terminal_lost"}, True)]

    def test_row_already_gone_is_left_alone(self, db):
        # CAO deleted the terminal itself (its own kill produced the event).
        rows, _, deleted = db
        rows.clear()
        mod._teardown("aaaa0001", "terminal_finished")
        assert deleted == []

    def test_waits_for_a_session_teardown_and_skips_its_terminal(self, db):
        # delete_session holds the lock from kill_session (which makes the
        # `finished` event) until it has deleted the session's rows.
        import threading

        from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

        rows, _, deleted = db
        done = threading.Event()
        with session_lifecycle_lock("cao-a"):
            worker = threading.Thread(
                target=lambda: (mod._teardown("aaaa0001", "terminal_finished"), done.set())
            )
            worker.start()
            assert not done.wait(0.3), "teardown did not wait for the session teardown"
            rows.clear()  # the session teardown drops its rows, then releases
        worker.join(5)
        assert done.is_set() and deleted == []
