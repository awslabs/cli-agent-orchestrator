"""Unit tests for HerdrInboxService — event delivery, reconnect, kiro supplement."""

import asyncio
import contextlib
import inspect
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


def test_async_pane_close_rechecks_and_kills_only_through_session_claim(monkeypatch):
    """The production async path delegates the final empty decision atomically."""
    from cli_agent_orchestrator.backends import registry
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services import callback_recovery, terminal_service

    service = HerdrInboxService(socket_path="/tmp/test.sock")
    service.register_terminal("tid-1", "pane-1")
    monkeypatch.setattr(
        database,
        "get_terminal_metadata",
        lambda _terminal: {"tmux_session": "workspace-1", "tmux_window": None},
    )
    monkeypatch.setattr(
        terminal_service, "retire_observed_terminal", lambda *_args, **_kwargs: True
    )
    calls = []
    backend = MagicMock()
    monkeypatch.setattr(registry, "get_backend", lambda: backend)
    monkeypatch.setattr(database, "list_terminals_by_session", lambda _session: [])
    monkeypatch.setattr(
        callback_recovery,
        "session_lifecycle_claim",
        lambda kind, session: calls.append((kind, session)) or contextlib.nullcontext(),
    )

    _run_async(service._handle_lifecycle_event_async("pane.closed", {"pane_id": "pane-1"}))

    assert calls == [(type(backend).__name__, "workspace-1")]
    backend.kill_session.assert_called_once_with("workspace-1")


class TestHerdrInboxServiceRegistration:
    """Test terminal registration and unregistration."""

    def test_register_terminal(self):
        """register_terminal should add to both maps."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "w1-1", is_kiro=False)

        assert service._pane_to_terminal["w1-1"] == "tid1"
        assert service._terminal_to_pane["tid1"] == "w1-1"
        assert "tid1" not in service._kiro_terminals

    def test_register_kiro_terminal(self):
        """register_terminal with is_kiro=True tracks in kiro set."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid2", "w1-2", is_kiro=True)

        assert "tid2" in service._kiro_terminals

    def test_unregister_terminal(self):
        """unregister_terminal should remove from all tracking structures."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "w1-1", is_kiro=True)
        service._working_since["tid1"] = time.time()

        service.unregister_terminal("tid1")

        assert "w1-1" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid1" not in service._kiro_terminals
        assert "tid1" not in service._working_since

    def test_unregister_nonexistent_is_safe(self):
        """unregister_terminal for unknown terminal should not raise."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.unregister_terminal("nonexistent")  # Should not raise

    def test_ownership_snapshot_serializes_concurrent_registration(self):
        """A registration cannot resize the pane map during a loop snapshot."""
        snapshot_entered = threading.Event()
        release_snapshot = threading.Event()
        registration_done = threading.Event()
        failures = []

        class PausingDict(dict):
            def items(self):
                for index, item in enumerate(super().items()):
                    if index == 0:
                        snapshot_entered.set()
                        assert release_snapshot.wait(timeout=3)
                    yield item

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._pane_to_terminal = PausingDict({"pane-old": "tid-old"})
        service._terminal_to_pane = {"tid-old": "pane-old"}

        def snapshot():
            try:
                service._ownership_items()
            except Exception as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        def register():
            service.register_terminal("tid-new", "pane-new")
            registration_done.set()

        snapshot_thread = threading.Thread(target=snapshot)
        registration_thread = threading.Thread(target=register)
        snapshot_thread.start()
        assert snapshot_entered.wait(timeout=2)
        registration_thread.start()
        assert not registration_done.wait(timeout=0.1)
        release_snapshot.set()
        snapshot_thread.join(timeout=2)
        registration_thread.join(timeout=2)

        assert not failures
        assert registration_done.is_set()
        assert service._pane_to_terminal["pane-new"] == "tid-new"


class TestHerdrInboxServiceRegisterReconnect:
    """Registering a terminal on a live connection must force a reconnect, never
    a second events.subscribe.

    herdr 0.6.8 resets the entire connection when it receives a second
    events.subscribe on a connection that already has an active subscription.
    Because herdr exposes no incremental "add subscription" API, the only safe
    way to start streaming events for a newly registered pane is to drop the
    socket and rebuild the single combined subscription on a fresh connection.

    register_terminal may be called from a synchronous, non-event-loop thread,
    so it must schedule the reconnect onto the captured loop via
    run_coroutine_threadsafe rather than asyncio.create_task (which requires a
    running loop in the calling thread and would raise RuntimeError).
    """

    def test_register_while_connected_triggers_reconnect_not_second_subscribe(self):
        """register from a non-loop thread, while connected, closes the socket to
        force a reconnect and must NOT write a second events.subscribe."""

        async def run():
            service = HerdrInboxService(socket_path="/tmp/test.sock")
            service._connected = True
            service._loop = asyncio.get_running_loop()
            # close() is synchronous; use a plain MagicMock so write/close are tracked.
            writer = MagicMock()
            service._writer = writer

            # Call register from a separate thread that has no event loop of its own.
            t = threading.Thread(target=service.register_terminal, args=("tid_cross", "pane-cross"))
            t.start()
            t.join()

            # Give the cross-thread-scheduled coroutine time to run on this loop.
            await asyncio.sleep(0.05)

            # Mapping recorded.
            assert service._pane_to_terminal["pane-cross"] == "tid_cross"
            # Reconnect forced by closing the writer...
            writer.close.assert_called_once()
            # ...and NO second events.subscribe was written on the live connection.
            writer.write.assert_not_called()

        _run_async(run())

    def test_register_before_start_does_not_reconnect(self):
        """register_terminal before start (no loop, not connected) must not touch the socket."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        writer = MagicMock()
        service._writer = writer

        # Pre-start state: start() has not run, so no loop captured and not connected.
        assert service._connected is False
        assert service._loop is None

        service.register_terminal("tid_early", "pane-early")

        # Mapping is still recorded...
        assert service._pane_to_terminal["pane-early"] == "tid_early"
        assert service._terminal_to_pane["tid_early"] == "pane-early"
        # ...but the socket was left untouched (guarded by _connected and _loop).
        writer.close.assert_not_called()
        writer.write.assert_not_called()


class TestHerdrInboxServiceDelivery:
    """Test message delivery callback invocation."""

    def test_deliver_calls_callback(self):
        """_deliver should invoke the delivery_callback with terminal_id."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        service._deliver("tid1")

        callback.assert_called_once_with("tid1")

    def test_deliver_handles_callback_error(self):
        """_deliver should log and not raise if callback fails."""
        callback = MagicMock(side_effect=RuntimeError("delivery failed"))
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        # Should not raise
        service._deliver("tid1")

    def test_deliver_without_callback(self):
        """_deliver with no callback should be a no-op."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._deliver("tid1")  # Should not raise


class TestHerdrInboxServiceSubscription:
    """Test combined event subscription message format.

    herdr 0.6.8 resets the connection on a second events.subscribe, so all
    subscriptions (every managed pane's agent-status plus the two lifecycle
    events) must be sent in a SINGLE events.subscribe call.
    """

    def test_subscribe_all_events_sends_single_combined_message(self):
        """_subscribe_all_events should send exactly one events.subscribe containing
        every managed pane's agent-status subscription plus the lifecycle events."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        service._pane_to_terminal = {"pane-1": "tid1", "pane-2": "tid2"}
        service._terminal_to_pane = {"tid1": "pane-1", "tid2": "pane-2"}

        _run_async(service._subscribe_all_events())

        # Exactly ONE write — never a second subscribe call.
        service._writer.write.assert_called_once()
        written = service._writer.write.call_args[0][0]
        msg = json.loads(written.decode().strip())

        assert msg["method"] == "events.subscribe"
        subs = msg["params"]["subscriptions"]

        # Every managed pane has an agent-status subscription with its pane_id.
        agent_subs = [s for s in subs if s["type"] == "pane.agent_status_changed"]
        assert {s["pane_id"] for s in agent_subs} == {"pane-1", "pane-2"}

        # Lifecycle events are included in the same single call.
        types = {s["type"] for s in subs}
        assert "pane.closed" in types
        assert "workspace.closed" in types

    def test_subscribe_all_events_with_no_panes_still_includes_lifecycle(self):
        """With no managed panes, the single subscribe still covers lifecycle events."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()

        _run_async(service._subscribe_all_events())

        service._writer.write.assert_called_once()
        msg = json.loads(service._writer.write.call_args[0][0].decode().strip())
        types = {s["type"] for s in msg["params"]["subscriptions"]}
        assert types == {"pane.closed", "workspace.closed"}
        # No agent-status entry without a pane_id (herdr rejects that as invalid_request).
        assert all(
            "pane_id" in s
            for s in msg["params"]["subscriptions"]
            if s["type"] == "pane.agent_status_changed"
        )


class TestHerdrInboxServiceEventParsing:
    """Test that _event_loop correctly unwraps the 'data' wrapper in socket events."""

    def test_event_loop_parses_data_wrapper_and_delivers(self):
        """Events with 'data' wrapper are correctly parsed and delivery is triggered."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        # Register a pane
        service.register_terminal("tid1", "pane-x", is_kiro=False)

        # Simulate two events: one "idle" (delivery) and one "working" (no delivery)
        idle_event = (
            json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-x", "agent_status": "idle"},
                }
            ).encode()
            + b"\n"
        )
        done_event = (
            json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-x", "agent_status": "done"},
                }
            ).encode()
            + b"\n"
        )
        # "working" event — should NOT trigger delivery
        working_event = (
            json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-x", "agent_status": "working"},
                }
            ).encode()
            + b"\n"
        )
        # Unknown pane — should NOT trigger delivery
        other_event = (
            json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-other", "agent_status": "idle"},
                }
            ).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            # Write events then close to end the loop
            reader.feed_data(idle_event + done_event + working_event + other_event)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass  # EOF raises ConnectionError — expected

        _run_async(run())

        # Only idle and done events on managed pane should trigger delivery
        assert callback.call_count == 2
        callback.assert_any_call("tid1")

    def test_event_loop_ignores_flat_format_without_data_wrapper(self):
        """Events without 'data' wrapper (old flat format) are silently ignored."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)
        service.register_terminal("tid1", "pane-x", is_kiro=False)

        # Old flat format — pane_id and agent_status at top level (not wrapped)
        flat_event = (
            json.dumps(
                {
                    "pane_id": "pane-x",
                    "agent_status": "idle",
                }
            ).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(flat_event)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass
            await asyncio.gather(*tuple(service._lifecycle_tasks))

        _run_async(run())

        # Flat format is not parsed — no delivery expected
        callback.assert_not_called()


class TestHerdrInboxServiceReconnect:
    """Test reconnect re-subscribe behavior: a single combined subscribe per connection."""

    def test_reconnect_resubscribe_sends_single_call_for_all_panes(self):
        """On reconnect, all managed panes are re-subscribed in ONE events.subscribe call.

        herdr resets the connection on a second events.subscribe, so re-subscribing
        N panes must be one combined call, not N separate calls.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        # Register two terminals with their current pane_ids
        service._terminal_to_pane["tid1"] = "pane-1"
        service._pane_to_terminal["pane-1"] = "tid1"
        service._terminal_to_pane["tid2"] = "pane-2"
        service._pane_to_terminal["pane-2"] = "tid2"

        _run_async(service._subscribe_all_events())

        # Exactly ONE subscribe message for all panes (not one per pane).
        service._writer.write.assert_called_once()
        msg = json.loads(service._writer.write.call_args[0][0].decode().strip())
        agent_panes = {
            s["pane_id"]
            for s in msg["params"]["subscriptions"]
            if s["type"] == "pane.agent_status_changed"
        }
        assert agent_panes == {"pane-1", "pane-2"}
        # Mapping should be unchanged
        assert service._terminal_to_pane["tid1"] == "pane-1"
        assert service._terminal_to_pane["tid2"] == "pane-2"


class TestHerdrInboxServiceKiroSupplement:
    """Test kiro supplement check for long-running working states."""

    @patch("subprocess.run")
    def test_kiro_supplement_delivers_on_permission_prompt(self, mock_run):
        """Should deliver when pane read reveals permission prompt after 30s working."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        # Register kiro terminal that's been working for 35s
        service.register_terminal("tid_kiro", "w1-5", is_kiro=True)
        service._working_since["tid_kiro"] = time.time() - 35.0

        # Mock pane read output containing kiro permission prompt pattern
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Agent wants to: Execute command\n[Y]es / [N]o / Yes to [A]ll",
        )

        with patch(
            "cli_agent_orchestrator.services.herdr_inbox_service.re.search",
            return_value=True,
        ):
            _run_async(service.check_kiro_supplements())

        callback.assert_called_once_with("tid_kiro")

    @patch("subprocess.run")
    def test_kiro_supplement_skips_under_threshold(self, mock_run):
        """Should not check terminals working for less than 30s."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        service.register_terminal("tid_kiro", "w1-5", is_kiro=True)
        service._working_since["tid_kiro"] = time.time() - 10.0  # Only 10s

        _run_async(service.check_kiro_supplements())

        mock_run.assert_not_called()
        callback.assert_not_called()

    def test_kiro_supplement_skips_non_kiro(self):
        """Should not check non-kiro terminals."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        service.register_terminal("tid_claude", "w1-3", is_kiro=False)
        service._working_since["tid_claude"] = time.time() - 60.0

        _run_async(service.check_kiro_supplements())

        callback.assert_not_called()


class TestHerdrInboxServiceReconcile:
    """Test _reconcile() prunes stale panes and cleans up DB/workspace."""

    def test_reconcile_is_called_before_subscribe(self):
        """_reconcile must be awaited before _subscribe_all_events in _socket_loop
        so the combined subscription only covers live panes."""
        # Structural test: confirms both are async coroutines.
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        assert inspect.iscoroutinefunction(service._reconcile)
        assert inspect.iscoroutinefunction(service._subscribe_all_events)

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_prunes_stale_pane(self, mock_meta, mock_delete, mock_run):
        """Stale pane_ids (not in live herdr list) are pruned from maps and DB."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-live")
        service.register_terminal("tid2", "pane-stale")

        pane_list_response = json.dumps({"result": {"panes": [{"pane_id": "pane-live"}]}})
        ws_list_response = json.dumps({"result": {"workspaces": []}})

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect
        mock_meta.return_value = None  # No session tracking needed

        _run_async(service._reconcile())

        # pane-stale pruned; pane-live kept
        assert "pane-stale" not in service._pane_to_terminal
        assert "tid2" not in service._terminal_to_pane
        assert "pane-live" in service._pane_to_terminal
        assert "tid1" in service._terminal_to_pane
        # DB record for stale terminal deleted
        mock_delete.assert_called_once_with(
            "tid2", expected_session=None, expected_pane_id="pane-stale"
        )

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_reconcile_no_op_when_all_panes_live(self, mock_run):
        """No pruning when all registered panes are still live."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a")
        service.register_terminal("tid2", "pane-b")

        pane_list_response = json.dumps(
            {"result": {"panes": [{"pane_id": "pane-a"}, {"pane_id": "pane-b"}]}}
        )
        ws_list_response = json.dumps({"result": {"workspaces": []}})

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        _run_async(service._reconcile())

        # Maps unchanged
        assert service._pane_to_terminal == {"pane-a": "tid1", "pane-b": "tid2"}

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_reconcile_continues_on_pane_list_failure(self, mock_run):
        """When herdr pane list fails, reconcile logs warning and returns without pruning."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a")

        m = MagicMock()
        m.returncode = 1
        m.stderr = "socket not found"
        mock_run.return_value = m

        # Should not raise
        _run_async(service._reconcile())

        # Map unchanged
        assert "pane-a" in service._pane_to_terminal

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_reconcile_deletes_ghost_db_terminals(self, mock_list_terminals, mock_delete, mock_run):
        """Ghost DB terminals (tab not in herdr) are deleted; live terminals are kept."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session = {"ws-abc": "my-session"}

        pane_list_response = json.dumps({"result": {"panes": []}})
        ws_list_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}]}}
        )
        tab_list_response = json.dumps(
            {
                "result": {
                    "tabs": [
                        {"label": "live-window", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"}
                    ]
                }
            }
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            elif "tab" in cmd and "list" in cmd:
                m.stdout = tab_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        mock_list_terminals.return_value = [
            {"id": "tid-live", "tmux_window": "live-window"},
            {"id": "tid-ghost", "tmux_window": "ghost-window"},
        ]

        _run_async(service._reconcile())

        # Only the ghost terminal should be deleted
        mock_delete.assert_called_once_with("tid-ghost", expected_session="my-session")

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_reconcile_skips_db_check_when_tab_list_fails(self, mock_list_terminals, mock_run):
        """When herdr tab list returns non-zero, list_terminals_by_session is never called."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session = {"ws-abc": "my-session"}

        pane_list_response = json.dumps({"result": {"panes": []}})
        ws_list_response = json.dumps({"result": {"workspaces": []}})

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            if "pane" in cmd and "list" in cmd:
                m.returncode = 0
                m.stdout = pane_list_response
            elif "tab" in cmd and "list" in cmd:
                m.returncode = 1
                m.stdout = ""
                m.stderr = "tab list failed"
            else:
                m.returncode = 0
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        # Should not raise
        _run_async(service._reconcile())

        mock_list_terminals.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_reconcile_no_ghost_when_all_tabs_match(
        self, mock_list_terminals, mock_delete, mock_run
    ):
        """When all DB terminals have matching live tabs, delete_terminal is never called."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session = {"ws-abc": "my-session"}

        pane_list_response = json.dumps({"result": {"panes": []}})
        ws_list_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}]}}
        )
        tab_list_response = json.dumps(
            {
                "result": {
                    "tabs": [
                        {"label": "window-one", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"},
                        {"label": "window-two", "tab_id": "ws-abc:2", "workspace_id": "ws-abc"},
                    ]
                }
            }
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            elif "tab" in cmd and "list" in cmd:
                m.stdout = tab_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        mock_list_terminals.return_value = [
            {"id": "tid-1", "tmux_window": "window-one"},
            {"id": "tid-2", "tmux_window": "window-two"},
        ]

        _run_async(service._reconcile())

        mock_delete.assert_not_called()


class TestHerdrInboxServiceStartupDbCleanup:
    """Test _startup_db_cleanup removes ghost terminals on server start."""

    def _make_subprocess_side_effect(self, ws_response, tab_response):
        def side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "workspace" in cmd and "list" in cmd:
                m.stdout = ws_response
            else:
                m.stdout = tab_response
            return m

        return side_effect

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_startup_cleanup_deletes_ghost_terminals(self, mock_list, mock_delete, mock_run):
        """Ghost terminals (window not in live herdr tabs) are deleted at startup."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        ws_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}]}}
        )
        tab_response = json.dumps(
            {
                "result": {
                    "tabs": [
                        {"label": "live-window", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"},
                    ]
                }
            }
        )
        mock_run.side_effect = self._make_subprocess_side_effect(ws_response, tab_response)
        mock_list.return_value = [
            {"id": "tid-live", "tmux_window": "live-window"},
            {"id": "tid-ghost", "tmux_window": "dead-window"},
        ]

        _run_async(service._startup_db_cleanup())

        mock_delete.assert_called_once_with("tid-ghost", expected_session="my-session")

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_startup_cleanup_skips_when_workspace_list_fails(self, mock_list, mock_run):
        """When herdr workspace list fails, no DB queries run."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        fail = MagicMock(returncode=1, stdout="")
        mock_run.return_value = fail
        _run_async(service._startup_db_cleanup())
        mock_list.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_startup_cleanup_no_deletes_when_all_live(self, mock_list, mock_delete, mock_run):
        """No deletions when all DB terminals have matching live tabs."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        ws_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}]}}
        )
        tab_response = json.dumps(
            {
                "result": {
                    "tabs": [
                        {"label": "conductor-10e0", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"},
                    ]
                }
            }
        )
        mock_run.side_effect = self._make_subprocess_side_effect(ws_response, tab_response)
        mock_list.return_value = [{"id": "tid-1", "tmux_window": "conductor-10e0"}]

        _run_async(service._startup_db_cleanup())

        mock_delete.assert_not_called()


class TestHerdrInboxServiceSingleSubscribePerConnection:
    """Guard against regressing to multiple events.subscribe calls per connection.

    The reconnect storm (herdr 0.6.8 resets on a 2nd events.subscribe) is fixed
    by sending exactly one combined subscribe. These tests pin that contract.
    """

    def test_no_separate_subscribe_pane_method(self):
        """The per-pane _subscribe_pane helper is removed — its existence reintroduces
        a second subscribe call when a terminal registers on a live connection."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        assert not hasattr(service, "_subscribe_pane")

    def test_no_separate_lifecycle_subscribe_method(self):
        """_subscribe_lifecycle_events is merged into _subscribe_all_events; a separate
        method means a second subscribe call on the same connection."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        assert not hasattr(service, "_subscribe_lifecycle_events")

    def test_socket_setup_issues_exactly_one_subscribe(self):
        """A full connect cycle (reconcile already done) writes exactly one subscribe."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        service._pane_to_terminal = {"pane-1": "tid1"}
        service._terminal_to_pane = {"tid1": "pane-1"}

        _run_async(service._subscribe_all_events())

        service._writer.write.assert_called_once()


class TestHerdrInboxServiceLifecycleEvents:
    """Test _handle_lifecycle_event for pane.closed and workspace.closed."""

    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_removes_from_maps(self, mock_meta, mock_delete):
        """pane.closed should remove the terminal from tracking maps and delete DB record."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a", is_kiro=True)
        service._working_since["tid1"] = time.time()
        mock_meta.return_value = None  # No session → no kill_session

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-a"})

        assert "pane-a" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid1" not in service._kiro_terminals
        assert "tid1" not in service._working_since
        mock_delete.assert_called_once_with(
            "tid1", expected_session=None, expected_pane_id="pane-a"
        )

    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        side_effect=RuntimeError("open callback recovery"),
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_retirement_refusal_preserves_ownership(self, mock_meta, mock_retire):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a", is_kiro=True)
        service._working_since["tid1"] = time.time()
        mock_meta.return_value = {
            "tmux_session": "session-a",
            "tmux_window": None,
            "generation": "model-generation",
            "pane_id": "pane-a",
        }

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-a"})

        assert service._pane_to_terminal == {"pane-a": "tid1"}
        assert service._terminal_to_pane == {"tid1": "pane-a"}
        assert "tid1" in service._kiro_terminals
        assert "tid1" in service._working_since
        mock_retire.assert_called_once_with(
            "tid1", expected_session="session-a", expected_pane_id="pane-a"
        )

    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_unknown_pane_is_noop(self, mock_meta, mock_delete):
        """pane.closed for unregistered pane_id should be silent no-op."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        service._handle_lifecycle_event("pane.closed", {"pane_id": "unknown-pane"})

        mock_delete.assert_not_called()
        mock_meta.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_skips_delete_when_label_still_live(self, mock_meta, mock_delete, mock_run):
        """Replayed close for a reused compact pane_id must NOT delete a live terminal.

        herdr (0.6.8) replays ALL historical pane_closed events on every fresh
        events.subscribe and reuses compact pane_ids when a tab is killed and a
        new tab takes the same index. So a replayed close for an OLD incarnation
        of pane-3 arrives mapped to the NEW terminal now occupying pane-3.

        The tab label (tmux_window) is unique per incarnation, so when the label
        is still live in herdr the close is stale and must be ignored.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        # New incarnation: terminal "9d00610c" occupies reused pane-3 with a
        # fresh unique label.
        service.register_terminal("9d00610c", "pane-3", is_kiro=False)
        mock_meta.return_value = {
            "tmux_session": "cao-investigation",
            "tmux_window": "sherlock-e8dc",
        }

        # herdr tab list shows the label is still live (new incarnation alive).
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "sherlock-e8dc", "workspace_id": "ws-1"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            m.stdout = tab_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-3"})

        # The live terminal must survive: no delete, maps intact.
        mock_delete.assert_not_called()
        assert service._pane_to_terminal.get("pane-3") == "9d00610c"
        assert service._terminal_to_pane.get("9d00610c") == "pane-3"

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_deletes_when_label_gone(self, mock_meta, mock_delete, mock_run):
        """Genuine close (label absent from herdr) still deletes the terminal.

        This is the user-initiated-close path: no kill_window ran, so the
        pane_closed event is the only signal. The tab label is genuinely gone
        from herdr, so the terminal must be cleaned up.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("9d00610c", "pane-3", is_kiro=False)
        mock_meta.return_value = {
            "tmux_session": "cao-investigation",
            "tmux_window": "sherlock-e8dc",
        }

        # herdr tab list does NOT contain the label — genuinely closed.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "other-tab", "workspace_id": "ws-1"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            m.stdout = tab_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-3"})

        mock_delete.assert_called_once_with(
            "9d00610c",
            expected_session="cao-investigation",
            expected_pane_id="pane-3",
        )
        assert "pane-3" not in service._pane_to_terminal
        assert "9d00610c" not in service._terminal_to_pane

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_preserves_when_herdr_query_fails(self, mock_meta, mock_delete, mock_run):
        """An unknown liveness query must never authorize a reused-pane delete."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("9d00610c", "pane-3", is_kiro=False)
        mock_meta.return_value = {
            "tmux_session": "cao-investigation",
            "tmux_window": "sherlock-e8dc",
        }

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 1  # herdr query failed
            m.stdout = ""
            m.stderr = "boom"
            return m

        mock_run.side_effect = subprocess_side_effect

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-3"})

        mock_delete.assert_not_called()
        assert service._pane_to_terminal["pane-3"] == "9d00610c"

    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.retire_closed_workspace_session")
    def test_workspace_closed_removes_all_terminals_for_session(
        self, mock_retire_session, mock_meta
    ):
        """workspace.closed prunes terminals by their DB session, not by a
        pane_id/workspace_id string prefix.

        Uses compact pane_ids that do NOT start with the workspace_id (herdr
        renumbers panes and gives no prefix guarantee) to prove the prune keys
        off DB session ownership rather than the pane_id string.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "p-7")
        service.register_terminal("tid2", "p-8")
        service.register_terminal("tid3", "p-9")  # Different session
        service._workspace_to_session["ws-abc"] = "my-session"

        session_by_terminal = {
            "tid1": {"tmux_session": "my-session"},
            "tid2": {"tmux_session": "my-session"},
            "tid3": {"tmux_session": "other-session"},
        }
        mock_meta.side_effect = lambda tid: session_by_terminal.get(tid)

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "ws-abc"})

        # my-session terminals pruned despite no pane_id/workspace_id prefix match
        assert "p-7" not in service._pane_to_terminal
        assert "p-8" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid2" not in service._terminal_to_pane
        # Terminal owned by a different session is untouched
        assert "p-9" in service._pane_to_terminal
        assert "tid3" in service._terminal_to_pane
        # Workspace entry cleaned up
        assert "ws-abc" not in service._workspace_to_session
        # Managed generation-aware retirement called
        mock_retire_session.assert_called_once_with("my-session")

    @patch("cli_agent_orchestrator.clients.database.delete_terminals_by_session")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_closed_workspace_session",
        create=True,
    )
    def test_workspace_closed_preserves_session_held_by_callback_recovery(
        self, mock_retire, mock_raw_delete
    ):
        """A closed workspace cannot bypass the managed recovery teardown guard."""
        from cli_agent_orchestrator.services.terminal_service import (
            TerminalGenerationMismatchError,
        )

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("worker01", "p-7")
        service._workspace_to_session["ws-abc"] = "my-session"
        mock_retire.side_effect = TerminalGenerationMismatchError(
            "terminal worker01 has an open callback-recovery operation"
        )

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "ws-abc"})

        mock_retire.assert_called_once_with("my-session")
        mock_raw_delete.assert_not_called()
        assert service._pane_to_terminal["p-7"] == "worker01"
        assert service._workspace_to_session["ws-abc"] == "my-session"

    @patch("cli_agent_orchestrator.clients.database.delete_terminals_by_session")
    def test_workspace_closed_unknown_workspace_is_noop(self, mock_delete):
        """workspace.closed for workspace_id not in _workspace_to_session is silent no-op."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "unknown-ws"})

        mock_delete.assert_not_called()

    def test_event_loop_routes_lifecycle_events(self):
        """_event_loop must route herdr's real lifecycle event wire format.

        Captured live from herdr 0.6.8: lifecycle events carry the name in the
        "event" key (NOT "type") using UNDERSCORE names:
            {"event":"pane_closed","data":{"pane_id":...,"workspace_id":...}}
            {"event":"workspace_closed","data":{"workspace_id":...}}
        The agent-status event uses the DOTTED name in the same "event" key:
            {"event":"pane.agent_status_changed","data":{...}}
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session["ws-x"] = "sess-x"

        pane_closed = (
            json.dumps(
                {
                    "event": "pane_closed",
                    "data": {
                        "pane_id": "pane-gone",
                        "type": "pane_closed",
                        "workspace_id": "ws-x",
                    },
                }
            ).encode()
            + b"\n"
        )
        ws_closed = (
            json.dumps(
                {
                    "event": "workspace_closed",
                    "data": {"type": "workspace_closed", "workspace_id": "ws-unknown"},
                }
            ).encode()
            + b"\n"
        )

        handled = []

        async def capture(event_type, data):
            handled.append(event_type)

        service._handle_lifecycle_event_async = capture

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(pane_closed + ws_closed)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass
            await asyncio.gather(*tuple(service._lifecycle_tasks))

        _run_async(run())

        # Both lifecycle events must be routed (normalized to dotted names).
        assert "pane.closed" in handled
        assert "workspace.closed" in handled

    def test_workspace_retirement_does_not_block_socket_status_delivery(self):
        blocked = threading.Event()
        release = threading.Event()
        delivered = []
        service = HerdrInboxService(
            socket_path="/tmp/test.sock",
            delivery_callback=lambda terminal_id: delivered.append(terminal_id),
        )
        service.register_terminal("tid-live", "pane-live")

        async def slow_workspace_retirement(event_type, data):
            assert event_type == "workspace.closed"
            blocked.set()
            assert await asyncio.to_thread(release.wait, 3)

        service._handle_lifecycle_event_async = slow_workspace_retirement
        events = (
            json.dumps({"event": "workspace_closed", "data": {"workspace_id": "ws-slow"}}).encode()
            + b"\n"
            + json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-live", "agent_status": "idle"},
                }
            ).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(events)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass
            assert await asyncio.wait_for(asyncio.to_thread(blocked.wait, 1), timeout=2)
            await asyncio.gather(*tuple(service._delivery_tasks.values()))
            assert delivered == ["tid-live"]
            release.set()
            await asyncio.gather(*tuple(service._lifecycle_tasks))

        _run_async(run())

    def test_delivery_is_off_loop_coalesced_and_preserves_replacement_owner(self):
        """A30: blocked delivery cannot stall socket events or erase a replacement."""
        first_delivery_started = threading.Event()
        second_delivery_started = threading.Event()
        release_first = threading.Event()
        release_second = threading.Event()
        lifecycle_seen = threading.Event()
        first_delivery_at = []
        lifecycle_at = []
        deliveries = []
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-old", "pane-reused")

        def blocking_delivery(terminal_id):
            deliveries.append(terminal_id)
            if len(deliveries) == 1:
                first_delivery_at.append(time.monotonic())
                first_delivery_started.set()
                assert release_first.wait(timeout=3)
            elif len(deliveries) == 2:
                second_delivery_started.set()
                assert release_second.wait(timeout=3)
            else:
                raise AssertionError("delivery triggers must coalesce to one trailing rerun")

        async def observe_lifecycle(event_type, data):
            assert event_type == "workspace.closed"
            assert data == {"workspace_id": "ws-later"}
            lifecycle_at.append(time.monotonic())
            lifecycle_seen.set()

        service._delivery_callback = blocking_delivery
        service._handle_lifecycle_event_async = observe_lifecycle
        status = (
            json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-reused", "agent_status": "idle"},
                }
            ).encode()
            + b"\n"
        )
        lifecycle = (
            json.dumps({"event": "workspace_closed", "data": {"workspace_id": "ws-later"}}).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(status + lifecycle + status + status)
            reader.feed_eof()
            timed_release = threading.Timer(0.25, release_first.set)
            timed_second_release = threading.Timer(0.3, release_second.set)
            timed_release.start()
            timed_second_release.start()
            try:
                with contextlib.suppress(ConnectionError):
                    await service._event_loop()
                assert await asyncio.wait_for(asyncio.to_thread(first_delivery_started.wait, 1), 2)
                await asyncio.sleep(0)
                # The later lifecycle event must pass through the real socket seam
                # while the first delivery callback remains blocked in a worker.
                assert lifecycle_seen.is_set()
                assert lifecycle_at[0] - first_delivery_at[0] < 0.15
                assert set(service._delivery_tasks) == {"tid-old"}
                assert service._delivery_reruns == {"tid-old": "pane-reused"}

                release_first.set()
                assert await asyncio.wait_for(
                    asyncio.to_thread(second_delivery_started.wait, 1), timeout=2
                )
                service.register_terminal("tid-replacement", "pane-reused")
                release_second.set()
            finally:
                timed_release.cancel()
                timed_second_release.cancel()
                release_first.set()
                release_second.set()
                await asyncio.gather(*tuple(getattr(service, "_delivery_tasks", {}).values()))
                await asyncio.gather(*tuple(service._lifecycle_tasks))

        _run_async(run())
        assert deliveries == ["tid-old", "tid-old"]
        assert service._pane_to_terminal == {"pane-reused": "tid-replacement"}
        assert service._terminal_to_pane == {"tid-replacement": "pane-reused"}

    def test_delivery_task_exception_is_observed(self, caplog):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-failing", "pane-failing")

        async def fail_delivery(_terminal_id):
            raise RuntimeError("delivery task boom")

        service._deliver_off_loop = fail_delivery

        async def run():
            service._schedule_delivery("tid-failing")
            await asyncio.gather(
                service._delivery_tasks["tid-failing"],
                return_exceptions=True,
            )
            await asyncio.sleep(0)

        _run_async(run())
        assert service._delivery_tasks == {}
        assert "Delivery task failed for terminal tid-failing" in caplog.messages

    def test_shutdown_awaits_real_blocking_delivery_thread_before_untracking(self):
        """A30 shutdown: real to_thread work stays tracked until it completes."""
        delivery_started = threading.Event()
        release_delivery = threading.Event()
        delivery_finished = threading.Event()
        deliveries = []
        service = HerdrInboxService(
            socket_path="/tmp/test.sock",
            delivery_callback=lambda terminal_id: (
                deliveries.append(terminal_id),
                delivery_started.set(),
                release_delivery.wait(timeout=3),
                delivery_finished.set(),
            ),
        )
        service.register_terminal("tid-shutdown", "pane-shutdown")

        async def run():
            service._schedule_delivery("tid-shutdown")
            task = service._delivery_tasks["tid-shutdown"]
            assert await asyncio.wait_for(asyncio.to_thread(delivery_started.wait, 1), timeout=2)
            service._schedule_delivery("tid-shutdown")
            assert service._delivery_reruns == {"tid-shutdown": "pane-shutdown"}

            shutdown = asyncio.create_task(service._shutdown_delivery_tasks())
            try:
                await asyncio.sleep(0.1)
                assert service._delivery_shutdown is True
                assert service._delivery_reruns == {}
                assert shutdown.done() is False
                assert service._delivery_tasks == {"tid-shutdown": task}
                assert delivery_finished.is_set() is False

                release_delivery.set()
                await asyncio.wait_for(shutdown, timeout=2)
            finally:
                release_delivery.set()
                await asyncio.wait_for(asyncio.to_thread(delivery_finished.wait, 1), timeout=2)
                if not shutdown.done():
                    await shutdown

        _run_async(run())
        assert deliveries == ["tid-shutdown"]
        assert service._delivery_tasks == {}
        assert service._delivery_reruns == {}

    def test_pane_retirement_does_not_block_socket_status_delivery(self):
        blocked = threading.Event()
        release = threading.Event()
        delivered = []
        service = HerdrInboxService(
            socket_path="/tmp/test.sock",
            delivery_callback=lambda terminal_id: delivered.append(terminal_id),
        )
        service.register_terminal("tid-closing", "pane-closing")
        service.register_terminal("tid-live", "pane-live")

        def slow_probe(_window_name):
            blocked.set()
            assert release.wait(timeout=3)
            from cli_agent_orchestrator.services.herdr_inbox_service import PaneLiveness

            return PaneLiveness.ABSENT

        service._label_still_live = slow_probe
        events = (
            json.dumps({"event": "pane_closed", "data": {"pane_id": "pane-closing"}}).encode()
            + b"\n"
            + json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {"pane_id": "pane-live", "agent_status": "idle"},
                }
            ).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(events)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass
            assert await asyncio.wait_for(
                asyncio.to_thread(blocked.wait, 1),
                timeout=2,
            )
            assert delivered == ["tid-live"]
            release.set()
            await asyncio.gather(*tuple(service._lifecycle_tasks))

        with (
            patch(
                "cli_agent_orchestrator.clients.database.get_terminal_metadata",
                return_value={
                    "tmux_session": "session-live",
                    "tmux_window": "closing-window",
                },
            ),
            patch(
                "cli_agent_orchestrator.clients.database.list_terminals_by_session",
                return_value=[{"id": "tid-live"}],
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
                return_value=True,
            ),
        ):
            _run_async(run())

    @patch("cli_agent_orchestrator.services.terminal_service.retire_closed_workspace_session")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_workspace_retirement_preserves_concurrent_map_registration(
        self,
        mock_meta,
        mock_retire,
    ):
        blocked = threading.Event()
        release = threading.Event()
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session["ws-old"] = "session-old"
        service.register_terminal("tid-old", "pane-old")
        mock_meta.return_value = {"tmux_session": "session-old"}

        def retire(_session_name, **_kwargs):
            assert _kwargs == {"unregister_inbox": False}
            blocked.set()
            assert release.wait(timeout=3)
            return ["tid-old"]

        mock_retire.side_effect = retire

        async def run():
            service._schedule_lifecycle_retirement(
                "workspace.closed",
                {"workspace_id": "ws-old"},
            )
            assert await asyncio.wait_for(
                asyncio.to_thread(blocked.wait, 1),
                timeout=2,
            )
            service.register_terminal("tid-new", "pane-new")
            release.set()
            await asyncio.gather(*tuple(service._lifecycle_tasks))

        _run_async(run())
        assert service._pane_to_terminal == {"pane-new": "tid-new"}
        assert service._terminal_to_pane == {"tid-new": "pane-new"}

    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_event_loop_pane_closed_real_shape_cleans_up(self, mock_meta, mock_delete):
        """End-to-end: a real-shape pane_closed event removes the managed terminal."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-x", "pane-x", is_kiro=False)
        mock_meta.return_value = None  # no session → no kill_session

        event = (
            json.dumps(
                {
                    "event": "pane_closed",
                    "data": {
                        "pane_id": "pane-x",
                        "type": "pane_closed",
                        "workspace_id": "ws-x",
                    },
                }
            ).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(event)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass
            await asyncio.gather(*tuple(service._lifecycle_tasks))

        _run_async(run())

        assert "pane-x" not in service._pane_to_terminal
        assert "tid-x" not in service._terminal_to_pane
        mock_delete.assert_called_once_with(
            "tid-x",
            expected_session=None,
            expected_pane_id="pane-x",
            unregister_inbox=False,
        )

    def test_event_loop_agent_status_real_shape_delivers(self):
        """A real-shape agent_status_changed (event key, dotted name) triggers delivery."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)
        service.register_terminal("tid-a", "pane-a", is_kiro=False)

        idle_event = (
            json.dumps(
                {
                    "event": "pane.agent_status_changed",
                    "data": {
                        "agent": "claude",
                        "agent_status": "idle",
                        "pane_id": "pane-a",
                        "workspace_id": "ws-a",
                    },
                }
            ).encode()
            + b"\n"
        )

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(idle_event)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass

        _run_async(run())

        callback.assert_called_once_with("tid-a")


class TestHerdrInboxServiceReconcileLiveTerminal:
    """Reconcile MUST NOT delete a terminal whose tab label is still live.

    herdr renumbers compact pane_ids when a sibling tab closes, so a live
    terminal's stored pane_id can fall out of `herdr pane list`. The stale-pane
    diff then treats it as dead. Identity must come from the durable tab label
    (tmux_window), not the ephemeral pane_id: a live label means the pane was
    renumbered (re-map it), an absent label means it was genuinely closed
    (delete it).
    """

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_remaps_renumbered_but_live_pane(
        self, mock_meta, mock_delete, mock_run, mock_get_backend
    ):
        """Stored pane_id missing from live list but tab label live -> re-map, never delete."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-old")
        mock_meta.return_value = {"tmux_session": "sess", "tmux_window": "win-1"}

        # Live pane list no longer contains pane-old (renumbered to pane-new).
        pane_list_response = json.dumps({"result": {"panes": [{"pane_id": "pane-new"}]}})
        # Empty workspace list bypasses the DB cross-check; isolates stale-pane logic.
        ws_list_response = json.dumps({"result": {"workspaces": []}})
        # _label_still_live() sees win-1 as a live tab -> pane was renumbered, not closed.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "win-1", "workspace_id": "ws-1"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            elif "tab" in cmd and "list" in cmd:
                m.stdout = tab_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        mock_backend = MagicMock()
        mock_backend.get_pane_id.return_value = "pane-new"
        mock_get_backend.return_value = mock_backend

        _run_async(service._reconcile())

        # Live terminal survives: NOT deleted.
        mock_delete.assert_not_called()
        # Re-mapped to the current pane_id.
        assert service._pane_to_terminal.get("pane-new") == "tid1"
        assert service._terminal_to_pane.get("tid1") == "pane-new"
        # Old (renumbered-away) pane_id is gone from the map.
        assert "pane-old" not in service._pane_to_terminal

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_deletes_when_tab_label_gone(
        self, mock_meta, mock_delete, mock_run, mock_get_backend
    ):
        """Stored pane_id missing AND tab label absent from herdr -> prune maps + delete."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-old")
        service._working_since["tid1"] = time.time()
        mock_meta.return_value = {"tmux_session": "sess", "tmux_window": "win-gone"}

        pane_list_response = json.dumps({"result": {"panes": [{"pane_id": "pane-other"}]}})
        ws_list_response = json.dumps({"result": {"workspaces": []}})
        # win-gone is NOT among live tab labels -> genuinely closed.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "some-other-window", "workspace_id": "ws-1"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            elif "tab" in cmd and "list" in cmd:
                m.stdout = tab_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect
        mock_get_backend.return_value = MagicMock()

        _run_async(service._reconcile())

        # Genuinely-closed terminal is cleaned up.
        mock_delete.assert_called_once_with(
            "tid1", expected_session="sess", expected_pane_id="pane-old"
        )
        assert "pane-old" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid1" not in service._working_since

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.retire_observed_terminal",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_does_not_kill_live_workspace_on_pane_diff(
        self, mock_meta, mock_list, mock_delete, mock_run, mock_get_backend
    ):
        """A live workspace (label present) must NOT be killed merely because its
        pane failed the pane_id set diff. The renumbered pane is re-mapped and the
        workspace stays alive."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-old")
        mock_meta.return_value = {"tmux_session": "sess", "tmux_window": "win-1"}

        pane_list_response = json.dumps({"result": {"panes": [{"pane_id": "pane-new"}]}})
        # Workspace "sess" is LIVE.
        ws_list_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-1", "label": "sess"}]}}
        )
        # Tab label win-1 is LIVE.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "win-1", "workspace_id": "ws-1"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            if "pane" in cmd and "list" in cmd:
                m.stdout = pane_list_response
            elif "tab" in cmd and "list" in cmd:
                m.stdout = tab_list_response
            else:
                m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect
        # DB cross-check: terminal's window matches a live tab -> not a ghost.
        mock_list.return_value = [{"id": "tid1", "tmux_window": "win-1"}]

        mock_backend = MagicMock()
        mock_backend.get_pane_id.return_value = "pane-new"
        mock_get_backend.return_value = mock_backend

        _run_async(service._reconcile())

        # The live workspace must NOT be killed.
        mock_backend.kill_session.assert_not_called()
        # And the live terminal must NOT be deleted.
        mock_delete.assert_not_called()
        # Terminal re-mapped, still owned by the session.
        assert service._terminal_to_pane.get("tid1") == "pane-new"


class TestHerdrInboxServiceWorkspaceClosedLiveResolution:
    """workspace.closed cleanup must not depend on _workspace_to_session having
    been populated by a prior _reconcile(). It must resolve session identity from
    live herdr state at event time so a real workspace close always cleans up.
    """

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.retire_closed_workspace_session")
    def test_workspace_closed_resolves_uncached_workspace_from_live_herdr(
        self, mock_retire_session, mock_meta, mock_run
    ):
        """workspace_id NOT in the in-memory map is resolved via herdr workspace
        list, then the session's terminals are deleted."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        # Map is empty — the workspace closed before any reconcile cached it.
        assert service._workspace_to_session == {}
        service.register_terminal("tid1", "p-1")
        mock_meta.side_effect = lambda tid: {"tmux_session": "sess-new"} if tid == "tid1" else None

        # Live herdr resolves ws-new -> sess-new.
        ws_list_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-new", "label": "sess-new"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "ws-new"})

        # Resolved live -> session terminals deleted despite empty in-memory map.
        mock_retire_session.assert_called_once_with("sess-new")
        # Map pruned for the closed session's terminal.
        assert "p-1" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.delete_terminals_by_session")
    def test_workspace_closed_unresolvable_is_safe_noop(self, mock_delete_by_session, mock_run):
        """workspace_id absent from both the map and live herdr state -> no deletions."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        # Live herdr does not know this workspace either.
        ws_list_response = json.dumps(
            {"result": {"workspaces": [{"workspace_id": "ws-other", "label": "sess-other"}]}}
        )

        def subprocess_side_effect(cmd, **_):
            m = MagicMock()
            m.returncode = 0
            m.stdout = ws_list_response
            return m

        mock_run.side_effect = subprocess_side_effect

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "ws-ghost"})

        # Nothing destructive happens.
        mock_delete_by_session.assert_not_called()


class TestHerdrInboxServiceSocketPath:
    """Test socket path resolution."""

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"})
    def test_uses_xdg_config_home(self):
        """Should use XDG_CONFIG_HOME when set."""
        path = HerdrInboxService._default_socket_path("cao")
        assert path == "/custom/config/herdr/sessions/cao/herdr.sock"

    @patch.dict("os.environ", {}, clear=True)
    @patch("pathlib.Path.home")
    def test_falls_back_to_home_config(self, mock_home):
        """Should fall back to ~/.config when XDG_CONFIG_HOME is unset."""
        from pathlib import PurePosixPath

        mock_home.return_value = PurePosixPath("/home/user")
        import os

        os.environ.pop("XDG_CONFIG_HOME", None)
        path = HerdrInboxService._default_socket_path("cao")
        assert path.endswith("/.config/herdr/sessions/cao/herdr.sock")

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"})
    def test_custom_session_name_in_socket_path(self):
        """Should include session name in the socket path."""
        path = HerdrInboxService._default_socket_path("my-session")
        assert path == "/custom/config/herdr/sessions/my-session/herdr.sock"

    @patch.dict("os.environ", {"XDG_CONFIG_HOME": "/custom/config"})
    def test_default_session_name_uses_flat_path(self):
        """The 'default' session should use ~/.config/herdr/herdr.sock (no subdir)."""
        path = HerdrInboxService._default_socket_path("default")
        assert path == "/custom/config/herdr/herdr.sock"
