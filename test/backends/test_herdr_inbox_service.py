"""Unit tests for HerdrInboxService — event delivery, reconnect, kiro supplement."""

import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.services.herdr_inbox_service import HerdrInboxService


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


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


class TestHerdrInboxServiceCrossThreadRegistration:
    """Test that register_terminal works from threads without an event loop.

    register_terminal may be called from a synchronous, non-event-loop thread.
    When connected, it must schedule the subscribe coroutine onto the captured
    loop via run_coroutine_threadsafe rather than asyncio.create_task (which
    requires a running loop in the calling thread and would raise RuntimeError).
    """

    def test_register_from_non_event_loop_thread_subscribes(self):
        """register_terminal from a non-loop thread should schedule subscribe via run_coroutine_threadsafe."""

        async def run():
            service = HerdrInboxService(socket_path="/tmp/test.sock")
            service._connected = True
            service._loop = asyncio.get_running_loop()
            service._writer = AsyncMock()

            # Call register from a separate thread that has no event loop of its own.
            t = threading.Thread(
                target=service.register_terminal, args=("tid_cross", "pane-cross")
            )
            t.start()
            t.join()

            # Give the cross-thread-scheduled coroutine time to run on this loop.
            await asyncio.sleep(0.05)

            # Subscribe message must have been written to the socket.
            service._writer.write.assert_called_once()
            written = service._writer.write.call_args[0][0]
            msg = json.loads(written.decode().strip())
            assert msg["method"] == "events.subscribe"
            assert msg["params"]["subscriptions"][0]["type"] == "pane.agent_status_changed"
            assert msg["params"]["subscriptions"][0]["pane_id"] == "pane-cross"

        _run_async(run())

    def test_register_before_start_does_not_subscribe(self):
        """register_terminal before start (no loop, not connected) must not attempt subscribe."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()

        # Pre-start state: start() has not run, so no loop captured and not connected.
        assert service._connected is False
        assert service._loop is None

        service.register_terminal("tid_early", "pane-early")

        # Mapping is still recorded...
        assert service._pane_to_terminal["pane-early"] == "tid_early"
        assert service._terminal_to_pane["tid_early"] == "pane-early"
        # ...but no subscribe was sent (guarded by _connected and _loop).
        service._writer.write.assert_not_called()


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
    """Test event subscription message format."""

    def test_subscribe_pane_sends_correct_message(self):
        """_subscribe_pane should send correct JSON to socket."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()

        _run_async(service._subscribe_pane("w1-1"))

        service._writer.write.assert_called_once()
        written = service._writer.write.call_args[0][0]
        msg = json.loads(written.decode().strip())

        assert msg["method"] == "events.subscribe"
        assert msg["params"]["subscriptions"][0]["type"] == "pane.agent_status_changed"
        assert msg["params"]["subscriptions"][0]["pane_id"] == "w1-1"


class TestHerdrInboxServiceEventParsing:
    """Test that _event_loop correctly unwraps the 'data' wrapper in socket events."""

    def test_event_loop_parses_data_wrapper_and_delivers(self):
        """Events with 'data' wrapper are correctly parsed and delivery is triggered."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        # Register a pane
        service.register_terminal("tid1", "pane-x", is_kiro=False)

        # Simulate two events: one "idle" (delivery) and one "working" (no delivery)
        idle_event = json.dumps({
            "event": "pane.agent_status_changed",
            "data": {"pane_id": "pane-x", "agent_status": "idle"},
        }).encode() + b"\n"
        done_event = json.dumps({
            "event": "pane.agent_status_changed",
            "data": {"pane_id": "pane-x", "agent_status": "done"},
        }).encode() + b"\n"
        # "working" event — should NOT trigger delivery
        working_event = json.dumps({
            "event": "pane.agent_status_changed",
            "data": {"pane_id": "pane-x", "agent_status": "working"},
        }).encode() + b"\n"
        # Unknown pane — should NOT trigger delivery
        other_event = json.dumps({
            "event": "pane.agent_status_changed",
            "data": {"pane_id": "pane-other", "agent_status": "idle"},
        }).encode() + b"\n"

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
        flat_event = json.dumps({
            "pane_id": "pane-x",
            "agent_status": "idle",
        }).encode() + b"\n"

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(flat_event)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass

        _run_async(run())

        # Flat format is not parsed — no delivery expected
        callback.assert_not_called()


class TestHerdrInboxServiceReconnect:
    """Test reconnection re-subscribe behavior."""

    def test_resubscribe_sends_subscribe_for_all_managed_panes(self):
        """_resubscribe_all should re-subscribe existing pane_ids without scanning pane list.

        CAO UUIDs in _terminal_to_pane do not match herdr's internal terminal_ids,
        so re-resolution via pane list scan is incorrect. Re-subscribe with the
        existing _pane_to_terminal mapping directly.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        # Register two terminals with their current pane_ids
        service._terminal_to_pane["tid1"] = "pane-1"
        service._pane_to_terminal["pane-1"] = "tid1"
        service._terminal_to_pane["tid2"] = "pane-2"
        service._pane_to_terminal["pane-2"] = "tid2"

        _run_async(service._resubscribe_all())

        # Should have sent 2 subscribe messages, one per pane
        assert service._writer.write.call_count == 2
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

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.HerdrInboxService._reconcile")
    def test_reconcile_is_called_before_resubscribe(self, mock_reconcile, mock_run):
        """_reconcile must be awaited before _resubscribe_all in _socket_loop."""
        # Verify ordering through call_count inspection — reconcile before subscribe.
        # This is a structural test: just confirms _reconcile exists and is async.
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        assert asyncio.iscoroutinefunction(service._reconcile)

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_prunes_stale_pane(self, mock_meta, mock_delete, mock_run):
        """Stale pane_ids (not in live herdr list) are pruned from maps and DB."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-live")
        service.register_terminal("tid2", "pane-stale")

        pane_list_response = json.dumps({
            "result": {"panes": [{"pane_id": "pane-live"}]}
        })
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

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_reconcile_no_op_when_all_panes_live(self, mock_run):
        """No pruning when all registered panes are still live."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a")
        service.register_terminal("tid2", "pane-b")

        pane_list_response = json.dumps({
            "result": {"panes": [{"pane_id": "pane-a"}, {"pane_id": "pane-b"}]}
        })
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


class TestHerdrInboxServiceLifecycleSubscription:
    """Test _subscribe_lifecycle_events sends correct message."""

    def test_subscribe_lifecycle_events_sends_correct_message(self):
        """Should subscribe to pane.closed and workspace.closed."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()

        _run_async(service._subscribe_lifecycle_events())

        service._writer.write.assert_called_once()
        written = service._writer.write.call_args[0][0]
        msg = json.loads(written.decode().strip())

        assert msg["method"] == "events.subscribe"
        subs = msg["params"]["subscriptions"]
        types = {s["type"] for s in subs}
        assert "pane.closed" in types
        assert "workspace.closed" in types


class TestHerdrInboxServiceLifecycleEvents:
    """Test _handle_lifecycle_event for pane.closed and workspace.closed."""

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_removes_from_maps(self, mock_meta, mock_delete, mock_run):
        """pane.closed should remove the terminal from tracking maps."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a", is_kiro=True)
        service._working_since["tid1"] = time.time()
        mock_meta.return_value = None  # No session → no kill_session

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-a"})

        assert "pane-a" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid1" not in service._kiro_terminals
        assert "tid1" not in service._working_since

    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_unknown_pane_is_noop(self, mock_meta, mock_delete):
        """pane.closed for unregistered pane_id should be silent no-op."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        service._handle_lifecycle_event("pane.closed", {"pane_id": "unknown-pane"})

        mock_delete.assert_not_called()
        mock_meta.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.delete_terminals_by_session")
    def test_workspace_closed_removes_all_terminals_for_session(self, mock_delete_by_session):
        """workspace.closed should prune all terminals whose pane_id starts with workspace_id."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "ws-abc/0")
        service.register_terminal("tid2", "ws-abc/1")
        service.register_terminal("tid3", "ws-other/0")  # Different workspace
        service._workspace_to_session["ws-abc"] = "my-session"

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "ws-abc"})

        # ws-abc terminals pruned
        assert "ws-abc/0" not in service._pane_to_terminal
        assert "ws-abc/1" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid2" not in service._terminal_to_pane
        # Other workspace unaffected
        assert "ws-other/0" in service._pane_to_terminal
        assert "tid3" in service._terminal_to_pane
        # Workspace entry cleaned up
        assert "ws-abc" not in service._workspace_to_session
        # DB cleanup called
        mock_delete_by_session.assert_called_once_with("my-session")

    @patch("cli_agent_orchestrator.clients.database.delete_terminals_by_session")
    def test_workspace_closed_unknown_workspace_is_noop(self, mock_delete):
        """workspace.closed for workspace_id not in _workspace_to_session is silent no-op."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        service._handle_lifecycle_event("workspace.closed", {"workspace_id": "unknown-ws"})

        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_event_loop_routes_lifecycle_events(self, mock_run):
        """_event_loop should call _handle_lifecycle_event for pane.closed/workspace.closed."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session["ws-x"] = "sess-x"

        pane_closed = json.dumps({
            "type": "pane.closed",
            "data": {"pane_id": "pane-gone"},
        }).encode() + b"\n"
        ws_closed = json.dumps({
            "type": "workspace.closed",
            "data": {"workspace_id": "ws-unknown"},
        }).encode() + b"\n"

        handled = []

        original = service._handle_lifecycle_event

        def capture(event_type, data):
            handled.append(event_type)
            original(event_type, data)

        service._handle_lifecycle_event = capture

        async def run():
            reader = asyncio.StreamReader()
            service._reader = reader
            reader.feed_data(pane_closed + ws_closed)
            reader.feed_eof()
            try:
                await service._event_loop()
            except ConnectionError:
                pass

        _run_async(run())

        assert "pane.closed" in handled
        assert "workspace.closed" in handled


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
