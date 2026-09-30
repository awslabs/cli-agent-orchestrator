"""Unit tests for HerdrInboxService — event delivery, reconnect, kiro supplement."""

import asyncio
import inspect
import json
import time
from contextlib import contextmanager
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch

from cli_agent_orchestrator.services.herdr_inbox_service import (
    HerdrInboxService,
    _retain_deferred_failure_tombstone,
)


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


class TestHerdrInboxServiceRegisterReconnect:
    """Registering a terminal must NOT touch the socket.

    The subscription is a single broadcast pane.updated (no pane_id) covering
    every pane, so a newly registered pane's events already arrive on the live
    connection. Registration therefore only updates the in-memory maps — it must
    never close the socket or write a second events.subscribe (herdr 0.7.x resets
    the connection on a second subscribe, which caused past reconnect storms).
    """

    def test_register_while_connected_does_not_touch_socket(self):
        """With broadcast subscription, a newly registered pane's events already
        arrive — registration must NOT close the socket, write, or schedule a
        reconnect coroutine."""
        import asyncio

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        writer = MagicMock()
        service._writer = writer
        # Simulate a live connection with a captured loop, the state under which
        # the removed force-reconnect used to fire.
        service._connected = True
        service._loop = MagicMock()

        # Behavioral assertion: registration must not schedule ANY coroutine onto
        # the loop. This is the real contract (not a private-name check) and it is
        # non-vacuous — writer.close/write alone pass even if a coroutine is merely
        # scheduled on an un-run loop, so assert on the scheduling call itself.
        with patch.object(asyncio, "run_coroutine_threadsafe") as mock_schedule:
            service.register_terminal("tid1", "w1:p1", is_kiro=False)
            mock_schedule.assert_not_called()

        assert service._pane_to_terminal["w1:p1"] == "tid1"
        writer.close.assert_not_called()
        writer.write.assert_not_called()
        # Belt-and-braces: the force-reconnect method is gone entirely, so it
        # cannot be reintroduced without also updating this guard.
        assert not hasattr(service, "_force_reconnect")

    def test_register_before_start_does_not_reconnect(self):
        """register_terminal before start() has run must not touch the socket."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        writer = MagicMock()
        service._writer = writer

        # Pre-start state: start() has not run. Registration only updates the
        # in-memory maps; it must not schedule a coroutine or write to the socket.
        assert not hasattr(service, "_force_reconnect")

        service.register_terminal("tid_early", "pane-early")

        # Mapping is still recorded...
        assert service._pane_to_terminal["pane-early"] == "tid_early"
        assert service._terminal_to_pane["tid_early"] == "pane-early"
        # ...but the socket is left untouched — registration never writes to it.
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

    herdr 0.7.5 resets the connection on a second events.subscribe, so all
    subscriptions must be sent in a SINGLE events.subscribe call. The
    subscription is a broadcast pane.updated (no pane_id) that carries
    agent_status for every pane, plus the two lifecycle events.
    """

    def test_subscribe_all_events_sends_single_broadcast_message(self):
        """One events.subscribe with broadcast pane.updated + lifecycle, NO pane_id.

        herdr 0.7.5 resets the connection on a second events.subscribe, so this
        must stay a single call. pane.updated is a broadcast (no pane_id) that
        carries agent_status for every pane, so per-pane subscriptions are gone.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        # Empty map: the broadcast subscription shape must NOT depend on any
        # registered panes — it is a single pane.updated with no per-pane entries.
        service._pane_to_terminal = {}

        _run_async(service._subscribe_all_events())

        service._writer.write.assert_called_once()
        msg = json.loads(service._writer.write.call_args[0][0].decode().strip())
        assert msg["method"] == "events.subscribe"
        types = {s["type"] for s in msg["params"]["subscriptions"]}
        assert types == {"pane.updated", "pane.closed", "workspace.closed"}
        # Broadcast subscriptions carry no pane_id.
        assert all("pane_id" not in s for s in msg["params"]["subscriptions"])


class TestHerdrInboxServiceEventParsing:
    """Test that _event_loop correctly unwraps the 'data' wrapper in socket events."""

    def test_event_loop_parses_data_wrapper_and_delivers(self):
        """Events with the nested data.pane wrapper are parsed and delivery is triggered.

        Reflects the real subscribed wire shape (broadcast pane.updated with the
        pane object under data.pane), not the retired top-level
        pane.agent_status_changed shape.
        """
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)

        # Register a pane
        service.register_terminal("tid1", "pane-x", is_kiro=False)

        # Simulate two events: one "idle" (delivery) and one "working" (no delivery)
        idle_event = (
            json.dumps(
                {
                    "event": "pane_updated",
                    "data": {"pane": {"pane_id": "pane-x", "agent_status": "idle"}},
                }
            ).encode()
            + b"\n"
        )
        done_event = (
            json.dumps(
                {
                    "event": "pane_updated",
                    "data": {"pane": {"pane_id": "pane-x", "agent_status": "done"}},
                }
            ).encode()
            + b"\n"
        )
        # "working" event — should NOT trigger delivery
        working_event = (
            json.dumps(
                {
                    "event": "pane_updated",
                    "data": {"pane": {"pane_id": "pane-x", "agent_status": "working"}},
                }
            ).encode()
            + b"\n"
        )
        # Unknown pane — should NOT trigger delivery
        other_event = (
            json.dumps(
                {
                    "event": "pane_updated",
                    "data": {"pane": {"pane_id": "pane-other", "agent_status": "idle"}},
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

        _run_async(run())

        # Flat format is not parsed — no delivery expected
        callback.assert_not_called()

    def test_event_loop_reads_pane_updated_nested_pane(self):
        """pane.updated wraps the pane object under data.pane; extraction must
        read pane_id/agent_status from there and deliver for a managed pane."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        callback = MagicMock()
        service._delivery_callback = callback
        service._pane_to_terminal = {"w1:p1": "tid1"}

        frame = {
            "event": "pane_updated",
            "data": {"pane": {"pane_id": "w1:p1", "agent_status": "idle"}},
        }
        reader = AsyncMock()
        reader.readline.side_effect = [
            (json.dumps(frame) + "\n").encode(),
            b"",  # EOF ends the loop
        ]
        service._reader = reader
        try:
            _run_async(service._event_loop())
        except ConnectionError:
            pass  # EOF raises ConnectionError("Socket closed") — expected

        callback.assert_called_once_with("tid1")

    def test_event_loop_ignores_pane_updated_for_unmanaged_pane(self):
        """Broadcast now delivers events for ALL panes; the managed-pane filter
        must drop events for panes CAO does not track."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        callback = MagicMock()
        service._delivery_callback = callback
        service._pane_to_terminal = {"w1:p1": "tid1"}

        frame = {
            "event": "pane_updated",
            "data": {"pane": {"pane_id": "w9:p9", "agent_status": "idle"}},
        }
        reader = AsyncMock()
        reader.readline.side_effect = [(json.dumps(frame) + "\n").encode(), b""]
        service._reader = reader
        try:
            _run_async(service._event_loop())
        except ConnectionError:
            pass

        callback.assert_not_called()

    def test_event_loop_survives_pane_null(self):
        """A malformed pane.updated with data.pane=null must not raise (which
        would escape _event_loop/_socket_loop and permanently kill delivery)."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        callback = MagicMock()
        service._delivery_callback = callback
        service._pane_to_terminal = {"w1:p1": "tid1"}

        frame = {"event": "pane_updated", "data": {"pane": None}}
        reader = AsyncMock()
        reader.readline.side_effect = [(json.dumps(frame) + "\n").encode(), b""]
        service._reader = reader
        try:
            _run_async(service._event_loop())
        except ConnectionError:
            pass  # EOF — expected
        # No AttributeError; malformed event is simply ignored (no delivery).
        callback.assert_not_called()


class TestHerdrInboxServiceReconnect:
    """Test reconnect re-subscribe behavior: a single combined subscribe per connection."""

    def test_reconnect_resubscribe_sends_single_call_for_all_panes(self):
        """On reconnect, the broadcast subscription is re-sent in ONE events.subscribe call.

        herdr resets the connection on a second events.subscribe, so re-subscribing
        must be one combined call. The subscription is a broadcast pane.updated
        (no pane_id) covering every pane, plus the two lifecycle events.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._writer = AsyncMock()
        # Register two terminals with their current pane_ids
        service._terminal_to_pane["tid1"] = "pane-1"
        service._pane_to_terminal["pane-1"] = "tid1"
        service._terminal_to_pane["tid2"] = "pane-2"
        service._pane_to_terminal["pane-2"] = "tid2"

        _run_async(service._subscribe_all_events())

        # Exactly ONE broadcast subscribe message (not one per pane).
        service._writer.write.assert_called_once()
        msg = json.loads(service._writer.write.call_args[0][0].decode().strip())
        types = {s["type"] for s in msg["params"]["subscriptions"]}
        assert types == {"pane.updated", "pane.closed", "workspace.closed"}
        # Broadcast subscriptions carry no pane_id.
        assert all("pane_id" not in s for s in msg["params"]["subscriptions"])
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

    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_prunes_stale_pane(self, mock_meta, mock_delete, mock_snap):
        """Stale pane_ids (not in live herdr snapshot) are pruned from maps and DB."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-live")
        service.register_terminal("tid2", "pane-stale")

        mock_snap.return_value = {
            "panes": [{"pane_id": "pane-live"}],
            "tabs": [],
            "workspaces": [],
        }
        mock_meta.return_value = None  # No session tracking needed

        _run_async(service._reconcile())

        # pane-stale pruned; pane-live kept
        assert "pane-stale" not in service._pane_to_terminal
        assert "tid2" not in service._terminal_to_pane
        assert "pane-live" in service._pane_to_terminal
        assert "tid1" in service._terminal_to_pane
        # DB record for stale terminal deleted
        mock_delete.assert_called_once_with("tid2")

    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_no_op_when_all_panes_live(self, mock_snap):
        """No pruning when all registered panes are still live."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a")
        service.register_terminal("tid2", "pane-b")

        mock_snap.return_value = {
            "panes": [{"pane_id": "pane-a"}, {"pane_id": "pane-b"}],
            "tabs": [],
            "workspaces": [],
        }

        _run_async(service._reconcile())

        # Maps unchanged
        assert service._pane_to_terminal == {"pane-a": "tid1", "pane-b": "tid2"}

    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_continues_on_snapshot_failure(self, mock_snap):
        """When the snapshot fetch fails (None), reconcile logs and returns without pruning."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-a")

        mock_snap.return_value = None

        # Should not raise
        _run_async(service._reconcile())

        # Map unchanged
        assert "pane-a" in service._pane_to_terminal

    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_reconcile_deletes_ghost_db_terminals(
        self, mock_list_terminals, mock_delete, mock_snap
    ):
        """Ghost DB terminals (tab not in herdr) are deleted; live terminals are kept."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session = {"ws-abc": "my-session"}

        mock_snap.return_value = {
            "panes": [],
            "tabs": [{"label": "live-window", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"}],
            "workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}],
        }

        mock_list_terminals.return_value = [
            {"id": "tid-live", "tmux_window": "live-window"},
            {"id": "tid-ghost", "tmux_window": "ghost-window"},
        ]

        _run_async(service._reconcile())

        # Only the ghost terminal should be deleted
        mock_delete.assert_called_once_with("tid-ghost")

    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_reconcile_skips_db_check_when_snapshot_fails(self, mock_list_terminals, mock_snap):
        """When the snapshot is unavailable (None), the ghost-DB cross-check never runs.

        Tabs now come from the single snapshot, so "can't read live tab data" means
        the whole snapshot failed. reconcile must return before the DB cross-check so
        it never deletes terminals based on incomplete herdr state.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session = {"ws-abc": "my-session"}

        mock_snap.return_value = None

        # Should not raise
        _run_async(service._reconcile())

        mock_list_terminals.assert_not_called()

    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    def test_reconcile_no_ghost_when_all_tabs_match(
        self, mock_list_terminals, mock_delete, mock_snap
    ):
        """When all DB terminals have matching live tabs, delete_terminal is never called."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._workspace_to_session = {"ws-abc": "my-session"}

        mock_snap.return_value = {
            "panes": [],
            "tabs": [
                {"label": "window-one", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"},
                {"label": "window-two", "tab_id": "ws-abc:2", "workspace_id": "ws-abc"},
            ],
            "workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}],
        }

        mock_list_terminals.return_value = [
            {"id": "tid-1", "tmux_window": "window-one"},
            {"id": "tid-2", "tmux_window": "window-two"},
        ]

        _run_async(service._reconcile())

        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_revalidates_replacement_under_session_lock(
        self, mock_snap, mock_list_terminals, mock_delete
    ):
        """A replacement created after discovery must not be deleted from a stale snapshot.

        The first snapshot represents the old workspace just before a replacement
        session/window is created. The destructive DB cross-check must take the
        same per-session lifecycle lock as creation, re-read DB rows, then consult
        fresh Herdr state while still holding that lock.
        """

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        initial = {
            "panes": [],
            "tabs": [],
            "workspaces": [{"workspace_id": "ws-old", "label": "my-session"}],
        }
        replacement = {
            "panes": [
                {
                    "pane_id": "pane-new",
                    "terminal_id": "tid-new",
                    "workspace_id": "ws-new",
                }
            ],
            "tabs": [
                {
                    "label": "replacement-window",
                    "tab_id": "ws-new:1",
                    "workspace_id": "ws-new",
                }
            ],
            "workspaces": [{"workspace_id": "ws-new", "label": "my-session"}],
        }
        mock_snap.side_effect = [initial, replacement]

        lock_state = {"held": False}

        @contextmanager
        def fake_lifecycle_lock(session_name):
            assert session_name == "my-session"
            assert not lock_state["held"]
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

        def rows_for_replacement(session_name):
            assert session_name == "my-session"
            assert lock_state["held"], "DB worklist must be frozen under the lifecycle lock"
            return [{"id": "tid-new", "tmux_window": "replacement-window"}]

        mock_list_terminals.side_effect = rows_for_replacement

        with patch(
            "cli_agent_orchestrator.services.session_lock.session_lifecycle_lock",
            fake_lifecycle_lock,
        ):
            _run_async(service._reconcile())

        assert mock_snap.call_count >= 2
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_fresh_recheck_failure_never_proves_ghost(
        self, mock_snap, mock_list_terminals, mock_delete
    ):
        """An unreadable fresh snapshot must defer, never delete from stale evidence."""

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_snap.side_effect = [
            {
                "panes": [],
                "tabs": [],
                "workspaces": [{"workspace_id": "ws-old", "label": "my-session"}],
            },
            None,
        ]
        mock_list_terminals.return_value = [
            {"id": "tid-maybe-live", "tmux_window": "replacement-window"}
        ]

        _run_async(service._reconcile())

        assert mock_snap.call_count >= 2
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_survives_malformed_snapshot_records(
        self, mock_snap, mock_list, mock_delete, mock_meta
    ):
        """A pane missing pane_id or a workspace missing label must not raise
        (which would escape _reconcile and kill the socket loop)."""
        mock_list.return_value = []
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._pane_to_terminal = {"w1:p1": "tid1"}
        service._terminal_to_pane = {"tid1": "w1:p1"}
        mock_snap.return_value = {
            "panes": [{"agent_status": "idle"}, {"pane_id": "w1:p1"}],  # first missing pane_id
            "workspaces": [{"workspace_id": "w1"}],  # missing label
            "tabs": [{"workspace_id": "w1"}],  # missing label
        }
        # Must not raise; w1:p1 is live so nothing pruned.
        _run_async(service._reconcile())
        assert service._pane_to_terminal == {"w1:p1": "tid1"}


class TestHerdrInboxSnapshot:
    """_fetch_snapshot returns the parsed snapshot dict from `api snapshot`."""

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_parses_result(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock", herdr_session="cao")
        snap = {
            "result": {
                "snapshot": {
                    "panes": [
                        {
                            "pane_id": "w1:p1",
                            "terminal_id": "term_a",
                            "agent_status": "idle",
                            "tab_id": "w1:t1",
                            "workspace_id": "w1",
                        }
                    ],
                    "tabs": [{"tab_id": "w1:t1", "label": "conductor", "workspace_id": "w1"}],
                    "workspaces": [{"workspace_id": "w1", "label": "sess-a"}],
                }
            }
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(snap), stderr="")

        result = service._fetch_snapshot()

        assert [p["pane_id"] for p in result["panes"]] == ["w1:p1"]
        assert result["workspaces"][0]["label"] == "sess-a"
        # Invoked `api snapshot` for the configured session.
        args = mock_run.call_args[0][0]
        assert args[:2] == ["herdr", "--session"]
        assert args[-2:] == ["api", "snapshot"]

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_returns_none_on_failure(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="boom")
        assert service._fetch_snapshot() is None

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_returns_none_on_malformed_json(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_run.return_value = MagicMock(returncode=0, stdout="not json", stderr="")
        assert service._fetch_snapshot() is None

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_returns_none_on_non_object_json(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_run.return_value = MagicMock(returncode=0, stdout="null", stderr="")
        assert service._fetch_snapshot() is None

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_returns_none_on_timeout(self, mock_run):
        import subprocess as _sp

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_run.side_effect = _sp.TimeoutExpired(cmd="herdr", timeout=10)
        assert service._fetch_snapshot() is None

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    def test_fetch_snapshot_returns_none_when_snapshot_not_dict(self, mock_run):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        payload = {"result": {"snapshot": [1, 2, 3]}}  # snapshot is a list, not a dict
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")
        assert service._fetch_snapshot() is None


class TestHerdrInboxServiceStartupDbCleanup:
    """Test _startup_db_cleanup removes ghost terminals on server start."""

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[])
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_revalidates_replacement_under_session_lock(
        self, mock_snap, mock_list, mock_delete, _mock_all
    ):
        """Startup cleanup must not delete a replacement created after discovery."""

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_snap.side_effect = [
            {
                "panes": [],
                "workspaces": [{"workspace_id": "ws-old", "label": "my-session"}],
                "tabs": [],
            },
            {
                "panes": [
                    {
                        "pane_id": "pane-new",
                        "terminal_id": "tid-new",
                        "workspace_id": "ws-new",
                    }
                ],
                "workspaces": [{"workspace_id": "ws-new", "label": "my-session"}],
                "tabs": [
                    {
                        "label": "replacement-window",
                        "tab_id": "ws-new:1",
                        "workspace_id": "ws-new",
                    }
                ],
            },
        ]

        lock_state = {"held": False}

        @contextmanager
        def fake_lifecycle_lock(session_name):
            assert session_name == "my-session"
            lock_state["held"] = True
            try:
                yield
            finally:
                lock_state["held"] = False

        def replacement_rows(session_name):
            assert session_name == "my-session"
            assert lock_state["held"]
            return [{"id": "tid-new", "tmux_window": "replacement-window"}]

        mock_list.side_effect = replacement_rows
        with patch(
            "cli_agent_orchestrator.services.session_lock.session_lifecycle_lock",
            fake_lifecycle_lock,
        ):
            _run_async(service._startup_db_cleanup())

        assert mock_snap.call_count >= 2
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[])
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch(
        "cli_agent_orchestrator.clients.database.list_terminals_by_session",
        side_effect=RuntimeError("database is locked"),
    )
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_session_db_failure_is_deferred_not_fatal(
        self, mock_snap, _mock_list, mock_delete, mock_all
    ):
        """A transient per-session DB failure must not kill Herdr maintenance startup."""

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_snap.return_value = {
            "panes": [],
            "workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}],
            "tabs": [],
        }

        _run_async(service._startup_db_cleanup())

        mock_delete.assert_not_called()
        mock_all.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[])
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_deletes_ghost_from_snapshot(
        self, mock_snap, mock_list, mock_delete, _mock_all
    ):
        """Ghost terminals (window not in live herdr tabs) are deleted at startup."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        mock_snap.return_value = {
            "panes": [],
            "workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}],
            "tabs": [
                {"label": "live-window", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"},
            ],
        }
        mock_list.return_value = [
            {"id": "tid-live", "tmux_window": "live-window"},
            {"id": "tid-ghost", "tmux_window": "dead-window"},
        ]

        _run_async(service._startup_db_cleanup())

        mock_delete.assert_called_once_with("tid-ghost")

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[])
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_skips_on_snapshot_none(
        self, mock_snap, mock_list, mock_delete, _mock_all
    ):
        """When the snapshot is unavailable (None), no DB queries or deletes run."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        mock_snap.return_value = None
        _run_async(service._startup_db_cleanup())
        mock_list.assert_not_called()
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[])
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_no_deletes_when_all_live(
        self, mock_snap, mock_list, mock_delete, _mock_all
    ):
        """No deletions when all DB terminals have matching live tabs."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        mock_snap.return_value = {
            "panes": [],
            "workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}],
            "tabs": [
                {"label": "conductor-10e0", "tab_id": "ws-abc:1", "workspace_id": "ws-abc"},
            ],
        }
        mock_list.return_value = [{"id": "tid-1", "tmux_window": "conductor-10e0"}]

        _run_async(service._startup_db_cleanup())

        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals", return_value=[])
    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_retains_deferred_failure_tombstone(
        self, mock_snap, mock_list, mock_delete, mock_retain, _mock_all
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_snap.return_value = {
            "panes": [],
            "workspaces": [{"workspace_id": "ws-abc", "label": "my-session"}],
            "tabs": [],
        }
        mock_list.return_value = [{"id": "tid-failed", "tmux_window": "dead-window"}]
        mock_retain.return_value = True

        _run_async(service._startup_db_cleanup())

        mock_retain.assert_called_once_with("tid-failed", on_cleanup_deferred=ANY)
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.clients.database.list_all_terminals")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_startup_cleanup_discovers_tombstone_when_workspace_fully_absent(
        self, mock_snap, mock_retain, mock_all
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_snap.return_value = {"panes": [], "workspaces": [], "tabs": []}
        mock_all.return_value = [
            {"id": "tid-absent", "tmux_session": "cao-gone", "tmux_window": "gone-window"}
        ]
        mock_retain.return_value = True

        _run_async(service._startup_db_cleanup())

        mock_retain.assert_called_once_with("tid-absent", on_cleanup_deferred=ANY)

    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch("cli_agent_orchestrator.services.terminal_service.get_deferred_init_failure")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch(
        "cli_agent_orchestrator.clients.database."
        "list_pending_deferred_init_external_owner_terminal_ids"
    )
    def test_periodic_tombstone_rediscovery_recovers_after_initial_db_failure(
        self, mock_list, mock_meta, mock_failure, mock_retain
    ):
        """A DB outage must not make an absent-workspace tombstone undiscoverable forever."""

        service = HerdrInboxService(socket_path="/tmp/test.sock")
        mock_list.side_effect = [RuntimeError("database is locked"), ["tid-retry"]]
        mock_meta.return_value = {
            "id": "tid-retry",
            "deferred_init_external_owner": True,
            "deferred_init_failure": {
                "phase": "deferred_init",
                "kind": "interrupted_init",
                "message": "interrupted",
            },
            "deferred_init_runtime_reclaimed": False,
        }
        mock_failure.return_value = mock_meta.return_value["deferred_init_failure"]
        mock_retain.return_value = True

        assert service._rediscover_deferred_failure_tombstones() is False
        mock_retain.assert_not_called()

        assert service._rediscover_deferred_failure_tombstones() is True
        mock_retain.assert_called_once_with("tid-retry", on_cleanup_deferred=ANY)


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
    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    def test_tombstone_runtime_cleanup_retries_until_complete(self, mock_retain):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._pending_tombstone_runtime_cleanup.add("tid-retry")
        attempts = 0

        def retain(_terminal_id, *, on_cleanup_deferred=None):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                assert on_cleanup_deferred is not None
                on_cleanup_deferred("tid-retry")
            return True

        mock_retain.side_effect = retain

        service._retry_pending_tombstone_runtime_cleanup()
        assert service._pending_tombstone_runtime_cleanup == {"tid-retry"}

        service._retry_pending_tombstone_runtime_cleanup()
        assert service._pending_tombstone_runtime_cleanup == set()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    def test_closed_workspace_retry_never_deletes_replacement_live_label(
        self, mock_retain, mock_list, mock_delete
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._live_tab_labels = MagicMock(return_value={"developer-new1234"})
        mock_list.return_value = [
            {"id": "old00001", "tmux_window": "developer-old1234"},
            {"id": "new00001", "tmux_window": "developer-new1234"},
        ]
        mock_retain.return_value = False

        assert service._cleanup_closed_session("cao-reused") is True

        mock_delete.assert_called_once_with("old00001")
        assert call("new00001") not in mock_delete.call_args_list

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_lock.session_lifecycle_lock")
    def test_closed_workspace_cleanup_freezes_rows_then_reads_liveness_under_session_lock(
        self, mock_lock, mock_list, mock_delete
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        state = {"locked": False}
        order = []

        cm = MagicMock()
        cm.__enter__.side_effect = lambda: state.__setitem__("locked", True)
        cm.__exit__.side_effect = lambda *_args: state.__setitem__("locked", False)
        mock_lock.return_value = cm

        def list_rows(_session_name):
            assert state["locked"] is True
            order.append("rows")
            return []

        def live_labels():
            assert state["locked"] is True
            order.append("liveness")
            return set()

        mock_list.side_effect = list_rows
        service._live_tab_labels = MagicMock(side_effect=live_labels)

        assert service._cleanup_closed_session("cao-reused") is True

        assert order == ["rows", "liveness"]
        mock_lock.assert_called_once_with("cao-reused")
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_db_outage_ignores_replayed_live_pane(
        self, mock_meta, mock_snapshot, mock_retain
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-live", "pane-a")
        mock_meta.side_effect = RuntimeError("database is locked")
        mock_snapshot.return_value = {
            "panes": [{"pane_id": "pane-a", "terminal_id": "tid-live"}],
            "workspaces": [],
            "tabs": [],
        }

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-a"})

        assert service._terminal_to_pane["tid-live"] == "pane-a"
        mock_retain.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_db_outage_cleans_confirmed_dead_pane(
        self, mock_meta, mock_snapshot, mock_retain
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-dead", "pane-a")
        mock_meta.side_effect = RuntimeError("database is locked")
        mock_snapshot.return_value = {"panes": [], "workspaces": [], "tabs": []}
        mock_retain.return_value = True

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-a"})

        assert "tid-dead" not in service._terminal_to_pane
        mock_retain.assert_called_once_with("tid-dead", on_cleanup_deferred=ANY)

    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_db_outage_uses_live_terminal_id_to_remap(
        self, mock_snapshot, mock_meta, mock_retain
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-live", "pane-old")
        mock_snapshot.return_value = {
            "panes": [{"pane_id": "pane-new", "terminal_id": "tid-live"}],
            "workspaces": [],
            "tabs": [],
        }
        mock_meta.side_effect = RuntimeError("database is locked")

        _run_async(service._reconcile())

        assert service._terminal_to_pane["tid-live"] == "pane-new"
        assert service._pane_to_terminal["pane-new"] == "tid-live"
        assert "pane-old" not in service._pane_to_terminal
        mock_retain.assert_not_called()

    def test_pending_closed_session_retries_until_cleanup_succeeds(self):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._pending_closed_sessions["ws-closed"] = "cao-closed"
        service._cleanup_closed_session = MagicMock(side_effect=[False, True])

        service._retry_pending_closed_sessions()
        assert service._pending_closed_sessions == {"ws-closed": "cao-closed"}

        service._retry_pending_closed_sessions()
        assert service._pending_closed_sessions == {}

    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_retention_db_outage_preserves_runtime_for_retry(self, mock_meta, mock_dismantle):
        """Unknown ownership cannot authorize removing a possibly live provider's home."""

        mock_meta.side_effect = RuntimeError("database is locked")
        mock_dismantle.return_value = True

        deferred = []
        assert (
            _retain_deferred_failure_tombstone("tid-db-outage", on_cleanup_deferred=deferred.append)
            is True
        )

        mock_dismantle.assert_not_called()
        assert deferred == ["tid-db-outage"]

    def test_retention_db_outage_does_not_clean_a_cached_provider(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.services import terminal_service

        sentinel = tmp_path / "live-provider-config"
        sentinel.write_text("configuration still in use")
        provider = MagicMock()
        provider.cleanup.side_effect = sentinel.unlink
        monkeypatch.setattr(
            terminal_service.provider_manager, "_providers", {"tid-db-outage": provider}
        )
        monkeypatch.setattr(
            database,
            "get_terminal_metadata",
            MagicMock(side_effect=RuntimeError("database is locked")),
        )
        deferred = []

        assert _retain_deferred_failure_tombstone(
            "tid-db-outage", on_cleanup_deferred=deferred.append
        )

        assert sentinel.read_text() == "configuration still in use"
        provider.cleanup.assert_not_called()
        assert terminal_service.provider_manager._providers["tid-db-outage"] is provider
        assert deferred == ["tid-db-outage"]

    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.services.terminal_service.capture_terminal_snapshot")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch(
        "cli_agent_orchestrator.services.terminal_service.should_retain_deferred_failure_tombstone"
    )
    def test_pending_external_owner_dismantles_runtime_but_retains_row(
        self, mock_retain, mock_meta, mock_capture, mock_dismantle
    ):
        """Creation-time ownership closes pane.closed-before-failure persistence race."""

        mock_retain.return_value = True
        metadata = {
            "id": "tid-pending",
            "tmux_session": "cao-pending",
            "tmux_window": "pending-window",
            "deferred_init_external_owner": True,
            "deferred_init_failure": None,
        }
        mock_capture.return_value = metadata
        mock_meta.return_value = metadata
        mock_dismantle.return_value = True

        assert _retain_deferred_failure_tombstone("tid-pending") is True

        mock_retain.assert_called_once_with("tid-pending", metadata)
        mock_dismantle.assert_called_once_with("tid-pending", metadata, kill_window=False)

    @patch("cli_agent_orchestrator.services.terminal_service.dismantle_terminal_runtime")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch(
        "cli_agent_orchestrator.services.terminal_service."
        "_is_deferred_init_external_owner_active",
        return_value=True,
    )
    def test_current_process_pending_external_owner_is_retained_without_runtime_cleanup(
        self, _mock_active, mock_meta, mock_dismantle
    ):
        """Startup/reconcile discovery must not dismantle a live deferred initializer."""

        assert _retain_deferred_failure_tombstone("tid-active") is True

        mock_meta.assert_not_called()
        mock_dismantle.assert_not_called()

    @patch.object(HerdrInboxService, "_label_still_live", return_value=False)
    @patch("cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    def test_reconcile_stale_pane_retains_external_tombstone(
        self, mock_snapshot, mock_meta, mock_delete, mock_retain, mock_live
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-failed", "pane-old")
        mock_snapshot.return_value = {"panes": [], "workspaces": [], "tabs": []}
        mock_meta.return_value = {
            "id": "tid-failed",
            "tmux_session": "cao-failed",
            "tmux_window": "failed-window",
        }
        mock_retain.return_value = True

        _run_async(service._reconcile())

        mock_retain.assert_called_once_with("tid-failed", on_cleanup_deferred=ANY)
        mock_live.assert_called_once_with("failed-window")
        mock_delete.assert_not_called()
        assert "tid-failed" not in service._terminal_to_pane

    """Test _handle_lifecycle_event for pane.closed and workspace.closed."""

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
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
        mock_delete.assert_called_once_with("tid1")

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_unknown_pane_is_noop(self, mock_meta, mock_delete):
        """pane.closed for unregistered pane_id should be silent no-op."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")

        service._handle_lifecycle_event("pane.closed", {"pane_id": "unknown-pane"})

        mock_delete.assert_not_called()
        mock_meta.assert_not_called()

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
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

        mock_delete.assert_called_once_with("9d00610c")
        assert "pane-3" not in service._pane_to_terminal
        assert "9d00610c" not in service._terminal_to_pane

    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_defers_when_herdr_query_fails(self, mock_meta, mock_delete, mock_run):
        """Unknown liveness is not evidence of death for a replayable pane id."""
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
        assert service._terminal_to_pane["9d00610c"] == "pane-3"
        assert service._pane_to_terminal["pane-3"] == "9d00610c"

    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal", return_value=False)
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_retains_database_row_when_provider_cleanup_is_deferred(
        self, mock_meta, mock_teardown, mock_database_delete
    ):
        """A deferred Grok cleanup must retain the DB retry handle."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("retained-grok", "pane-retained")
        mock_meta.return_value = None

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-retained"})

        mock_teardown.assert_called_once_with("retained-grok")
        mock_database_delete.assert_not_called()

    @patch.object(HerdrInboxService, "_label_still_live", return_value=False)
    @patch(
        "cli_agent_orchestrator.services.herdr_inbox_service._retain_deferred_failure_tombstone",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_pane_closed_retains_deferred_failure_tombstone(
        self, mock_meta, mock_delete, mock_retain, mock_live
    ):
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid-failed", "pane-failed")
        mock_meta.return_value = {
            "tmux_session": "cao-failed",
            "tmux_window": "failed-window",
            "deferred_init_failure": {"message": "workspace trust required"},
        }

        service._handle_lifecycle_event("pane.closed", {"pane_id": "pane-failed"})

        assert "pane-failed" not in service._pane_to_terminal
        assert "tid-failed" not in service._terminal_to_pane
        mock_live.assert_called_once_with("failed-window")
        mock_retain.assert_called_once_with("tid-failed", on_cleanup_deferred=ANY)
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_workspace_closed_removes_all_terminals_for_session(
        self, mock_meta, mock_list_terminals, mock_delete_terminal
    ):
        """workspace.closed prunes terminals by their DB session, not by a
        pane_id/workspace_id string prefix.

        Uses compact pane_ids that do NOT start with the workspace_id (herdr
        renumbers panes and gives no prefix guarantee) to prove the prune keys
        off DB session ownership rather than the pane_id string.
        """
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service._live_tab_labels = MagicMock(return_value=set())
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
        mock_list_terminals.return_value = [{"id": "tid1"}, {"id": "tid2"}]

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
        # Each persisted terminal goes through the normal provider-aware
        # teardown rather than a bulk DB delete.
        assert mock_delete_terminal.call_args_list == [call("tid1"), call("tid2")]

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

        # Both lifecycle events must be routed (normalized to dotted names).
        assert "pane.closed" in handled
        assert "workspace.closed" in handled

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
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

        _run_async(run())

        assert "pane-x" not in service._pane_to_terminal
        assert "tid-x" not in service._terminal_to_pane
        mock_delete.assert_called_once_with("tid-x")

    def test_event_loop_agent_status_real_shape_delivers(self):
        """A real-shape broadcast pane_updated (event key, nested data.pane) triggers delivery."""
        callback = MagicMock()
        service = HerdrInboxService(socket_path="/tmp/test.sock", delivery_callback=callback)
        service.register_terminal("tid-a", "pane-a", is_kiro=False)

        idle_event = (
            json.dumps(
                {
                    "event": "pane_updated",
                    "data": {
                        "pane": {
                            "agent": "claude",
                            "agent_status": "idle",
                            "pane_id": "pane-a",
                            "workspace_id": "ws-a",
                        }
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
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_remaps_renumbered_but_live_pane(
        self, mock_meta, mock_delete, mock_snap, mock_run, mock_get_backend
    ):
        """Stored pane_id missing from live list but tab label live -> re-map, never delete."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-old")
        mock_meta.return_value = {"tmux_session": "sess", "tmux_window": "win-1"}

        # Snapshot: pane-old renumbered to pane-new; empty workspaces bypasses the
        # DB cross-check to isolate the stale-pane re-mapping logic.
        mock_snap.return_value = {
            "panes": [{"pane_id": "pane-new"}],
            "tabs": [{"label": "win-1", "workspace_id": "ws-1"}],
            "workspaces": [],
        }
        # _label_still_live() (unchanged, still shells out to `tab list`) sees win-1
        # as live -> pane was renumbered, not closed.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "win-1", "workspace_id": "ws-1"}]}}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=tab_list_response, stderr="")

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
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_deletes_when_tab_label_gone(
        self, mock_meta, mock_delete, mock_snap, mock_run, mock_get_backend
    ):
        """Stored pane_id missing AND tab label absent from herdr -> prune maps + delete."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-old")
        service._working_since["tid1"] = time.time()
        mock_meta.return_value = {"tmux_session": "sess", "tmux_window": "win-gone"}

        # Snapshot: pane-old is stale (only pane-other live); empty workspaces
        # bypasses the DB cross-check.
        mock_snap.return_value = {
            "panes": [{"pane_id": "pane-other"}],
            "tabs": [{"label": "some-other-window", "workspace_id": "ws-1"}],
            "workspaces": [],
        }
        # _label_still_live() (unchanged) sees win-gone absent -> genuinely closed.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "some-other-window", "workspace_id": "ws-1"}]}}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=tab_list_response, stderr="")
        mock_get_backend.return_value = MagicMock()

        _run_async(service._reconcile())

        # Genuinely-closed terminal is cleaned up.
        mock_delete.assert_called_once_with("tid1")
        assert "pane-old" not in service._pane_to_terminal
        assert "tid1" not in service._terminal_to_pane
        assert "tid1" not in service._working_since

    @patch("cli_agent_orchestrator.backends.registry.get_backend")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch.object(HerdrInboxService, "_fetch_snapshot")
    @patch("cli_agent_orchestrator.clients.database.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_reconcile_does_not_kill_live_workspace_on_pane_diff(
        self, mock_meta, mock_list, mock_delete, mock_snap, mock_run, mock_get_backend
    ):
        """A live workspace (label present) must NOT be killed merely because its
        pane failed the pane_id set diff. The renumbered pane is re-mapped and the
        workspace stays alive."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        service.register_terminal("tid1", "pane-old")
        mock_meta.return_value = {"tmux_session": "sess", "tmux_window": "win-1"}

        # Snapshot: pane-old renumbered to pane-new; workspace "sess" and tab
        # label "win-1" both LIVE.
        mock_snap.return_value = {
            "panes": [{"pane_id": "pane-new"}],
            "tabs": [{"label": "win-1", "workspace_id": "ws-1"}],
            "workspaces": [{"workspace_id": "ws-1", "label": "sess"}],
        }
        # _label_still_live() (unchanged) sees win-1 as live.
        tab_list_response = json.dumps(
            {"result": {"tabs": [{"label": "win-1", "workspace_id": "ws-1"}]}}
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=tab_list_response, stderr="")
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

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.clients.database.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.herdr_inbox_service.subprocess.run")
    @patch("cli_agent_orchestrator.clients.database.get_terminal_metadata")
    def test_workspace_closed_resolves_uncached_workspace_from_live_herdr(
        self, mock_meta, mock_run, mock_list_terminals, mock_delete_terminal
    ):
        """workspace_id NOT in the in-memory map is resolved via herdr workspace
        list, then the session's terminals are deleted."""
        service = HerdrInboxService(socket_path="/tmp/test.sock")
        # Map is empty — the workspace closed before any reconcile cached it.
        assert service._workspace_to_session == {}
        service.register_terminal("tid1", "p-1")
        mock_meta.side_effect = lambda tid: {"tmux_session": "sess-new"} if tid == "tid1" else None
        mock_list_terminals.return_value = [{"id": "tid1"}]

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

        # Resolved live -> persisted terminal gets provider-aware teardown.
        mock_delete_terminal.assert_called_once_with("tid1")
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
