"""Tests for the session service."""

from contextlib import contextmanager
from unittest.mock import ANY, MagicMock, patch

import pytest

from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import session_service
from cli_agent_orchestrator.services.session_service import (
    create_session,
    delete_session,
    get_session,
    list_sessions,
)

STOP_SESSION = "cao-stop-probe"


class TestCreateSession:
    """Tests for create_session function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_resolves_provider_when_omitted(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is None, resolve_provider is called and its result forwarded."""
        mock_resolve.return_value = "claude_code"
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider=None, agent_profile="my_agent")

        mock_resolve.assert_called_once_with("my_agent", fallback_provider="kiro_cli")
        assert mock_create_terminal.call_args.kwargs["provider"] == "claude_code"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.session_service.dispatch_plugin_event")
    @patch("cli_agent_orchestrator.services.session_service.create_terminal")
    @patch("cli_agent_orchestrator.services.session_service.resolve_provider")
    async def test_create_session_uses_explicit_provider(
        self, mock_resolve, mock_create_terminal, mock_dispatch
    ):
        """When provider is explicitly passed, resolve_provider is NOT called."""
        mock_terminal = MagicMock()
        mock_terminal.session_name = "cao-test"
        mock_create_terminal.return_value = mock_terminal

        await create_session(provider="kiro_cli", agent_profile="my_agent")

        mock_resolve.assert_not_called()
        assert mock_create_terminal.call_args.kwargs["provider"] == "kiro_cli"


class TestListSessions:
    """Tests for list_sessions function."""

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_success(self, mock_get_backend):
        """Test listing sessions successfully."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-session1", "name": "Session 1"},
            {"id": "cao-session2", "name": "Session 2"},
            {"id": "other-session", "name": "Other"},
        ]

        result = list_sessions()

        assert len(result) == 2
        assert all(s["id"].startswith("cao-") for s in result)

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_empty(self, mock_get_backend):
        """Test listing sessions when none exist."""
        mock_get_backend.return_value.list_sessions.return_value = []

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_no_cao_sessions(self, mock_get_backend):
        """Test listing sessions when no CAO sessions exist."""
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "other-session1", "name": "Other 1"},
            {"id": "other-session2", "name": "Other 2"},
        ]

        result = list_sessions()

        assert result == []

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_list_sessions_error(self, mock_get_backend):
        """Test listing sessions with error."""
        mock_get_backend.return_value.list_sessions.side_effect = Exception("Tmux error")

        result = list_sessions()

        assert result == []


class TestGetSession:
    """Tests for get_session function."""

    @patch("cli_agent_orchestrator.services.terminal_projection.project_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_success(self, mock_get_backend, mock_project):
        """Test getting session successfully."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [
            {"id": "cao-test", "name": "Test Session"}
        ]
        mock_project.return_value = [{"terminal_id": "terminal1", "tmux_session": "cao-test"}]

        result = get_session("cao-test")

        assert result["session"]["id"] == "cao-test"
        assert len(result["terminals"]) == 1
        mock_get_backend.return_value.session_exists.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.terminal_projection.project_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_serves_the_projection_both_human_views_read(
        self, mock_get_backend, mock_project
    ):
        """This route feeds the dashboard and ``conduct status``.

        While it returned raw DB rows, a terminal whose window had been
        deleted rendered as provider ``Unknown`` forever — indistinguishable
        from a healthy worker awaiting detection — and a managed v2 worker
        appeared in neither view because its row lives in another table.
        Meanwhile ``cao session status`` *was* projected, so the two views
        disagreed by construction.
        """
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-test"}]
        mock_project.return_value = [
            {"terminal_id": "term-a", "status": "processing", "lifecycle_state": "live"},
            {"terminal_id": "term-b", "status": "dead", "lifecycle_state": "dead"},
            {"terminal_id": "term-c", "protocol_vintage": "v2", "lifecycle_state": "live"},
        ]

        result = get_session("cao-test")

        mock_project.assert_called_once_with("cao-test")
        # The lifecycle reaches the view rather than being flattened into a
        # provider status, and the v2 row is present at all.
        assert result["terminals"][0]["status"] == "processing"
        assert result["terminals"][1]["lifecycle_state"] == "dead"
        assert result["terminals"][2]["protocol_vintage"] == "v2"

    @patch("cli_agent_orchestrator.services.terminal_projection.project_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_does_not_re_derive_status_over_the_projection(
        self, mock_get_backend, mock_project
    ):
        """The projection reports provider status for a live pane only.

        Enriching on top of it here would put ``unknown`` back on a dead
        row — the phantom card this whole change removes.
        """
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-test"}]
        mock_project.return_value = [{"terminal_id": "gone", "status": "dead"}]

        with patch(
            "cli_agent_orchestrator.services.status_monitor.status_monitor.get_status"
        ) as mock_status:
            result = get_session("cao-test")

        mock_status.assert_not_called()
        assert result["terminals"][0]["status"] == "dead"

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_found(self, mock_get_backend):
        """Test getting non-existent session."""
        mock_get_backend.return_value.session_exists.return_value = False

        with pytest.raises(ValueError, match="Session 'cao-nonexistent' not found"):
            get_session("cao-nonexistent")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_not_in_list(self, mock_get_backend):
        """Test getting session that exists but not in list."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = []

        with pytest.raises(ValueError, match="Session 'cao-test' not found"):
            get_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_get_session_error(self, mock_get_backend):
        """Test getting session with error."""
        mock_get_backend.return_value.session_exists.side_effect = Exception("Tmux error")

        with pytest.raises(Exception, match="Tmux error"):
            get_session("cao-test")


class TestDeleteSession:
    """Tests for delete_session function."""

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_success(
        self,
        mock_get_backend,
        mock_list_terminals,
        mock_delete_terminal,
        mock_clear_session_env,
    ):
        """Test deleting session successfully.

        delete_session holds every generation lifecycle claim while delegating
        per-terminal teardown to the claimed terminal-service path.
        """
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
        ]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        # Each terminal is torn down via the event-driven delete_terminal path.
        assert mock_delete_terminal.call_count == 2
        mock_delete_terminal.assert_any_call("terminal1", registry=ANY)
        mock_delete_terminal.assert_any_call("terminal2", registry=ANY)
        # The forwarded-env mapping is dropped via the (separately tested,
        # strict) store helper — mocked here so this orchestration test does
        # not depend on a migrated DB.
        mock_clear_session_env.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_when_backend_session_already_gone(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal, mock_clear_session_env
    ):
        """Backend session already gone — delete_session should not raise and not
        call kill_session, but still tear down each claimed terminal."""
        mock_get_backend.return_value.session_exists.return_value = False
        mock_list_terminals.return_value = [{"id": "terminal1"}]

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_not_called()
        mock_delete_terminal.assert_called_once_with("terminal1", registry=ANY)
        mock_clear_session_env.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_no_terminals(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal, mock_clear_session_env
    ):
        """Test deleting session with no terminals."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = []

        result = delete_session("cao-test")

        assert result == {"deleted": ["cao-test"], "errors": []}
        mock_get_backend.return_value.kill_session.assert_called_once_with("cao-test")
        mock_delete_terminal.assert_not_called()
        mock_clear_session_env.assert_called_once_with("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_error(self, mock_get_backend, mock_list_terminals):
        """Test deleting session with error."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.side_effect = Exception("Database error")

        with pytest.raises(Exception, match="Database error"):
            delete_session("cao-test")

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_holds_when_terminal_cleanup_fails(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal, mock_clear_session_env
    ):
        """A terminal refusal prevents the enclosing backend-session kill."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "terminal1"},
            {"id": "terminal2"},
            {"id": "terminal3"},
        ]

        # First terminal teardown fails, others succeed
        mock_delete_terminal.side_effect = [
            Exception("Terminal teardown error for terminal1"),
            None,  # terminal2 succeeds
            None,  # terminal3 succeeds
        ]

        with pytest.raises(RuntimeError, match="session deletion held"):
            delete_session("cao-test")

        mock_get_backend.return_value.kill_session.assert_not_called()
        mock_clear_session_env.assert_not_called()
        # All terminal teardowns are inspected, but no enclosing session
        # destruction is allowed after any one refuses.
        assert mock_delete_terminal.call_count == 3

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_delete_session_cleans_up_each_terminal(
        self, mock_get_backend, mock_list_terminals, mock_delete_terminal, mock_clear_session_env
    ):
        """Test that delete_session tears down every terminal in the session via delete_terminal."""
        mock_get_backend.return_value.session_exists.return_value = True
        mock_list_terminals.return_value = [
            {"id": "term-aaa"},
            {"id": "term-bbb"},
            {"id": "term-ccc"},
            {"id": "term-ddd"},
        ]

        result = delete_session("cao-multi-terminal")

        assert result == {"deleted": ["cao-multi-terminal"], "errors": []}
        # Verify delete_terminal was called for each terminal with the correct ID
        assert mock_delete_terminal.call_count == 4
        mock_delete_terminal.assert_any_call("term-aaa", registry=ANY)
        mock_delete_terminal.assert_any_call("term-bbb", registry=ANY)
        mock_delete_terminal.assert_any_call("term-ccc", registry=ANY)
        mock_delete_terminal.assert_any_call("term-ddd", registry=ANY)

    def test_delete_normalizes_a_bare_name_before_every_effect(
        self, monkeypatch, isolated_memory_db
    ):
        """The destructive name-release path and its physical target agree."""
        from cli_agent_orchestrator.services import callback_recovery, terminal_service

        sl.declare("cao-repair", sl.COMPLETE, declared_by="supervisor")
        seen = {}

        def _list(name):
            seen["list"] = name
            return [{"id": "t1", "generation": "g1"}]

        def _delete(_terminal_id, *, registry=None, **kwargs):
            seen["delete_expected_session"] = kwargs.get("expected_session")
            seen["delete_expected_generation"] = kwargs.get("expected_generation")
            return True

        backend = MagicMock()
        backend.session_exists.side_effect = lambda name: (
            seen.setdefault("exists", []).append(name) or True
        )
        backend.kill_session.side_effect = lambda name: seen.update(kill=name)
        monkeypatch.setattr(session_service, "get_backend", lambda: backend)
        monkeypatch.setattr(session_service, "list_terminals_by_session", _list)
        monkeypatch.setattr(terminal_service, "delete_terminal", _delete)
        monkeypatch.setattr(
            session_service,
            "clear_session_env",
            lambda name: seen.update(clear_env=name),
        )

        def _dispatch(_registry, _event_type, event):
            seen["event_session_name"] = event.session_name
            seen["event_session_id"] = event.session_id

        monkeypatch.setattr(session_service, "dispatch_plugin_event", _dispatch)
        real_claim = callback_recovery.session_lifecycle_claim

        def _claim_spy(backend_kind, name):
            seen["claim"] = name
            return real_claim(backend_kind, name)

        monkeypatch.setattr(callback_recovery, "session_lifecycle_claim", _claim_spy)

        result = delete_session("repair")

        assert result["deleted"] == ["cao-repair"]
        assert seen == {
            "claim": "cao-repair",
            "list": "cao-repair",
            "delete_expected_session": "cao-repair",
            "delete_expected_generation": "g1",
            "exists": ["cao-repair", "cao-repair"],
            "kill": "cao-repair",
            "clear_env": "cao-repair",
            "event_session_name": "cao-repair",
            "event_session_id": "cao-repair",
        }
        assert sl.describe("cao-repair")["declared"] is False


class TestStopSession:
    """Row-preserving stop: collect the panes, keep the truth.

    Regression target. Today the lifecycle ``stop`` records ``stopped`` and
    leaves the panes running, while ``delete_session`` collects the panes and
    then forgets the row and clears the env. The first is a suppressed fleet
    still burning quota; the second is a teardown that erases the evidence a
    resume needs. A stop must instead collect *and* preserve — collect every
    pane, keep the lifecycle row in ``stopped``, keep the forwarded env and
    the snapshots, and stay readable once tmux is gone.
    """

    @pytest.fixture(autouse=True)
    def _db(self, isolated_memory_db):
        # stop_session records the lifecycle row through the real store, so the
        # lifecycle/env tables need to live in the per-test database.
        return isolated_memory_db

    def _backend(self, *, exists=False):
        backend = MagicMock()
        backend.session_exists.return_value = exists
        return backend

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_stop_collects_every_pane_and_preserves_the_stopped_row(
        self, mock_backend, mock_list, mock_delete, mock_clear
    ):
        """The core of the fix: collect the fleet, keep the row, keep the env.

        Under the old split this is impossible — recording ``stopped`` touched
        no panes, and the only thing that collected panes forgot the row.
        """
        mock_backend.return_value.session_exists.return_value = True
        mock_list.return_value = [{"id": "term-1"}, {"id": "term-2"}]

        record = session_service.stop_session(STOP_SESSION, declared_by="colin")

        # Every pane is collected through the same event-driven teardown as
        # delete_session (snapshot, kill window, FIFO, DB).
        assert mock_delete.call_count == 2
        mock_delete.assert_any_call("term-1", registry=None)
        mock_delete.assert_any_call("term-2", registry=None)
        # The enclosing backend session is torn down.
        mock_backend.return_value.kill_session.assert_called_once_with(STOP_SESSION)
        # The lifecycle row is recorded AND preserved (not forgotten).
        assert record["lifecycle"] == sl.STOPPED
        assert sl.describe(STOP_SESSION)["lifecycle"] == sl.STOPPED
        assert sl.describe(STOP_SESSION)["declared"] is True
        # The forwarded env is not cleared: a resume would relaunch against it.
        mock_clear.assert_not_called()

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_stop_is_idempotent_when_retried(
        self, mock_backend, mock_list, mock_delete, mock_clear
    ):
        """A second stop after a full collection collects nothing and keeps the row."""
        mock_backend.return_value.session_exists.return_value = True
        mock_list.return_value = [{"id": "term-1"}]
        first = session_service.stop_session(STOP_SESSION, declared_by="colin")
        assert first["restore_to"] == sl.WORKING

        # Retry: the fleet is already collected, so there is nothing to tear down.
        mock_list.return_value = []
        mock_backend.return_value.session_exists.return_value = False
        again = session_service.stop_session(STOP_SESSION, declared_by="colin")

        assert again["lifecycle"] == sl.STOPPED
        # restore_to is preserved across the retry, not recomputed.
        assert again["restore_to"] == sl.WORKING
        assert mock_delete.call_count == 1  # only the first stop collected

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_a_mid_collection_failure_preserves_state_and_retry_converges(
        self, mock_backend, mock_list, mock_delete, mock_clear
    ):
        """A pane that refuses collection must not erase the row, env, or snapshots.

        The lifecycle row is written before any pane is collected, so a partial
        failure leaves a visibly stopped (and divergent) session rather than a
        silently half-torn-down one, and a retry re-collects what remains.
        """
        sl.declare(STOP_SESSION, sl.COMPLETE, declared_by="supervisor")
        mock_backend.return_value.session_exists.return_value = True

        # First attempt: one of two panes refuses collection.
        mock_list.return_value = [{"id": "term-1"}, {"id": "term-2"}]
        mock_delete.side_effect = [Exception("boom on term-1"), None]
        with pytest.raises(session_service.SessionStopPartial, match="partially collected"):
            session_service.stop_session(STOP_SESSION, declared_by="colin")

        # The row is stopped despite the partial failure, restore_to is intact,
        # and the env was never cleared.
        after_partial = sl.describe(STOP_SESSION)
        assert after_partial["lifecycle"] == sl.STOPPED
        assert after_partial["restore_to"] == sl.COMPLETE
        mock_clear.assert_not_called()
        mock_backend.return_value.kill_session.assert_not_called()

        # Retry converges: the remaining pane is collected, restore_to is
        # preserved, and the enclosing session is torn down.
        mock_list.return_value = [{"id": "term-1"}]
        mock_delete.side_effect = None
        mock_delete.return_value = True
        record = session_service.stop_session(STOP_SESSION, declared_by="colin")
        assert record["lifecycle"] == sl.STOPPED
        assert record["restore_to"] == sl.COMPLETE
        mock_backend.return_value.kill_session.assert_called_once_with(STOP_SESSION)

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_open_callback_recovery_blocks_collection_and_the_stopped_row(
        self, mock_backend, mock_list, mock_delete
    ):
        """An open recovery refuses both collection and the declaration.

        Collecting a terminal mid-recovery would lose the one-shot refusal the
        recovery is adjudicating, and declaring stopped while one is open is a
        false state. So this fails closed: zero pane deletion, no stopped row.
        """
        mock_backend.return_value.session_exists.return_value = True
        mock_list.return_value = [{"id": "term-1"}]
        with patch(
            "cli_agent_orchestrator.services.callback_recovery.terminal_has_open_recovery",
            return_value=True,
        ):
            with pytest.raises(Exception, match="open callback-recovery"):
                session_service.stop_session(STOP_SESSION, declared_by="colin")

        mock_delete.assert_not_called()
        mock_backend.return_value.kill_session.assert_not_called()
        assert sl.describe(STOP_SESSION)["lifecycle"] != sl.STOPPED

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_a_lifecycle_write_failure_deletes_no_panes(self, mock_backend, mock_list, mock_delete):
        """The durable declaration is written before any collection.

        A write failure (here a stale epoch) must delete nothing — otherwise a
        transient store hiccup during stop would collect an unrecorded fleet.
        """
        mock_backend.return_value.session_exists.return_value = True
        mock_list.return_value = [{"id": "term-1"}]
        first = sl.declare(STOP_SESSION, sl.WORKING, declared_by="a")
        sl.declare(STOP_SESSION, sl.COMPLETE, declared_by="b")  # bump the epoch

        with pytest.raises(sl.SessionLifecycleConflict, match="moved to epoch"):
            session_service.stop_session(
                STOP_SESSION, declared_by="colin", expected_epoch=first["epoch"]
            )

        mock_delete.assert_not_called()
        mock_backend.return_value.kill_session.assert_not_called()
        assert sl.describe(STOP_SESSION)["lifecycle"] == sl.COMPLETE

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_collection_happens_under_the_session_claim(self, mock_backend, mock_list, mock_delete):
        """No pane can appear between the stopped check and teardown.

        Stop holds the same session-lifecycle claim create does, so a concurrent
        create cannot add a window while the panes are being collected.
        """
        from cli_agent_orchestrator.services import callback_recovery

        mock_backend.return_value.session_exists.return_value = False
        mock_list.return_value = [{"id": "term-1"}]
        mock_delete.return_value = True

        held = {"under_claim": False}

        @contextmanager
        def _spy(_backend_kind, _name):
            held["under_claim"] = True
            try:
                yield
            finally:
                held["under_claim"] = False

        captured = {}

        def _delete_spy(*args, **kwargs):
            captured["under_claim"] = held["under_claim"]
            return True

        mock_delete.side_effect = _delete_spy

        with patch(
            "cli_agent_orchestrator.services.callback_recovery.session_lifecycle_claim",
            _spy,
        ):
            session_service.stop_session(STOP_SESSION, declared_by="colin")

        assert captured["under_claim"] is True, "panes were collected outside the session claim"

    def test_stop_preserves_the_forwarded_env(self, monkeypatch):
        """A hibernated session's resume relaunches against its forwarded env."""
        from cli_agent_orchestrator.services import session_env

        # Isolate the module-level env cache so this test owns it.
        monkeypatch.setattr(session_env, "_session_forwarded_env", {})
        monkeypatch.setattr(session_service, "get_backend", lambda: self._backend())
        monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _name: [])

        session_env.set_session_env(STOP_SESSION, {"API_TOKEN": "sekret"})
        session_service.stop_session(STOP_SESSION, declared_by="colin")
        assert session_env.get_session_env(STOP_SESSION) == {"API_TOKEN": "sekret"}

    def test_stop_preserves_the_row_that_delete_forgets(self, monkeypatch):
        """The split the fix draws: stop keeps the truth, delete releases the name."""
        sl.declare(STOP_SESSION, sl.COMPLETE, declared_by="supervisor")
        monkeypatch.setattr(session_service, "get_backend", lambda: self._backend())
        monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _name: [])

        session_service.stop_session(STOP_SESSION, declared_by="colin")
        assert sl.describe(STOP_SESSION)["lifecycle"] == sl.STOPPED  # preserved

        monkeypatch.setattr(session_service, "clear_session_env", lambda _name: None)
        delete_session(STOP_SESSION)
        assert sl.describe(STOP_SESSION)["declared"] is False  # forgotten

    @patch("cli_agent_orchestrator.services.session_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_archive_collects_and_preserves_like_stop(
        self, mock_backend, mock_list, mock_delete, mock_clear
    ):
        """Archiving forces a stop, so it carries the same collecting obligation."""
        sl.declare(STOP_SESSION, sl.COMPLETE, declared_by="supervisor")
        mock_backend.return_value.session_exists.return_value = True
        mock_list.return_value = [{"id": "term-1"}]
        mock_delete.return_value = True

        record = session_service.stop_session(STOP_SESSION, declared_by="colin", archived=True)

        mock_delete.assert_called_once_with("term-1", registry=None)
        mock_backend.return_value.kill_session.assert_called_once_with(STOP_SESSION)
        assert record["lifecycle"] == sl.STOPPED
        assert record["archived"] is True
        assert record["restore_to"] == sl.COMPLETE
        mock_clear.assert_not_called()

    def test_a_concurrent_declare_cannot_leave_a_collected_fleet_working(self, monkeypatch):
        """P1 regression: a declare racing a stop's collection must not win.

        The stop writes ``stopped`` and then blocks mid-collection while a
        second thread declares ``working`` with no ``expected_epoch``. With no
        shared critical section the last-write-wins declare commits over the
        stop, and a fully collected fleet ends up declared ``working`` — the
        false state the marshal must never see. The stop's declaration/
        collection critical section makes the racer wait, and a stopped
        session cannot be declared live, so the racer leaves with a typed
        conflict and the row stays ``stopped``.
        """
        import threading

        from cli_agent_orchestrator.services import terminal_service

        sl.declare(STOP_SESSION, sl.WORKING, declared_by="boot")

        in_collection = threading.Event()
        release_collection = threading.Event()

        def _blocking_delete(*_args, **_kwargs):
            # The stop has already written ``stopped`` by the time collection
            # begins; block here so the racer runs against the stopped row.
            in_collection.set()
            release_collection.wait(timeout=10)
            return True

        monkeypatch.setattr(terminal_service, "delete_terminal", _blocking_delete)
        monkeypatch.setattr(session_service, "get_backend", lambda: self._backend())
        monkeypatch.setattr(
            session_service, "list_terminals_by_session", lambda _n: [{"id": "term-1"}]
        )

        outcome: dict = {}

        def _racer():
            try:
                sl.declare(STOP_SESSION, sl.WORKING, declared_by="racer")
                outcome["won"] = True
            except sl.SessionLifecycleError as exc:
                outcome["refused"] = exc

        stop_thread = threading.Thread(
            target=session_service.stop_session,
            args=(STOP_SESSION,),
            kwargs={"declared_by": "colin"},
        )
        stop_thread.start()
        assert in_collection.wait(timeout=10), "stop never reached collection"

        racer = threading.Thread(target=_racer)
        racer.start()
        release_collection.set()
        stop_thread.join(timeout=10)
        racer.join(timeout=10)

        assert not stop_thread.is_alive()
        assert not racer.is_alive()
        # The fleet was collected; the durable row must not say working.
        assert sl.describe(STOP_SESSION)["lifecycle"] == sl.STOPPED
        # The racer did not silently win — it left with a typed conflict.
        assert isinstance(outcome.get("refused"), sl.SessionLifecycleConflict), outcome
        assert "won" not in outcome

    def test_the_physical_session_claim_serializes_stop_against_create(self, monkeypatch):
        """Stop and create share the physical session claim; the second waits.

        A pane cannot appear between the stopped check and the teardown because
        the whole stop holds the same physical claim create takes. This is the
        mutual-exclusion proof the "collection happens under the claim" spy test
        could not give on its own.
        """
        import threading

        from cli_agent_orchestrator.services import callback_recovery

        stop_holding = threading.Event()
        release_stop = threading.Event()
        create_entered = threading.Event()

        def _create_attempt():
            stop_holding.wait(timeout=10)
            with callback_recovery.session_lifecycle_claim("TmuxBackend", STOP_SESSION):
                create_entered.set()

        def _stop_holds():
            with callback_recovery.session_lifecycle_claim("TmuxBackend", STOP_SESSION):
                stop_holding.set()
                release_stop.wait(timeout=10)

        stop_thread = threading.Thread(target=_stop_holds)
        stop_thread.start()
        stop_holding.wait(timeout=10)

        create_thread = threading.Thread(target=_create_attempt)
        create_thread.start()
        create_thread.join(timeout=2)
        # The stop still holds the physical claim, so create must be blocked.
        assert create_thread.is_alive(), "create entered while stop held the claim"
        assert not create_entered.is_set()

        release_stop.set()
        stop_thread.join(timeout=10)
        create_thread.join(timeout=10)
        assert create_entered.is_set(), "create never entered after the stop released"
        assert not stop_thread.is_alive() and not create_thread.is_alive()

    def test_a_racing_create_cannot_overwrite_a_won_stop(self, monkeypatch):
        """P1: a create that loses the physical claim to a stop must not then
        erase the preserved env or create a physical session under a stopped row.

        The create path runs for real. It passes its early stopped-check, then
        blocks before acquiring the physical claim (here at ``resolve_provider``).
        While it waits, ``stop_session`` wins the claim: writes ``stopped``,
        preserves the forwarded env, collects the panes, and releases. The create
        then resumes and must re-check ``stopped`` *under the claim* and refuse —
        zero physical session/window, the preserved env untouched, the lifecycle
        row ``stopped`` with its original ``restore_to``. On the pre-fix code the
        create instead clears the env and creates a physical session under the
        still-``stopped`` row.
        """
        import asyncio
        import threading
        from unittest.mock import AsyncMock

        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.services import session_env, terminal_service

        sl.declare(STOP_SESSION, sl.WORKING, declared_by="boot")
        monkeypatch.setattr(session_env, "_session_forwarded_env", {})
        session_env.set_session_env(STOP_SESSION, {"API_TOKEN": "sekret"})

        create_past_early_check = threading.Event()
        release_create = threading.Event()
        physical_created: list = []

        def _blocking_resolve(_profile, *, fallback_provider=None):
            # Production seam between create_session's early stopped-check and
            # create_terminal's claim. stop wins the claim while create waits.
            create_past_early_check.set()
            release_create.wait(timeout=15)
            return "mock_cli"

        monkeypatch.setattr(session_service, "resolve_provider", _blocking_resolve)

        backend = MagicMock()
        backend.session_exists.return_value = False
        backend.kill_session.return_value = None
        backend.create_session.side_effect = lambda *a, **k: physical_created.append(True)
        monkeypatch.setattr(session_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        # stop has one pane to collect; create's per-pane teardown is a no-op.
        monkeypatch.setattr(session_service, "list_terminals_by_session", lambda _n: [{"id": "t1"}])
        monkeypatch.setattr(terminal_service, "delete_terminal", lambda *a, **k: True)

        # Minimal seams so the real create_terminal can run past the claim on the
        # pre-fix code. Post-fix it refuses at the in-claim stopped-check before
        # any of these are reached.
        monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "deadbeef")
        monkeypatch.setattr(terminal_service, "generate_window_name", lambda *_a, **_k: "dev-abcd")
        monkeypatch.setattr(
            terminal_service,
            "load_agent_profile",
            lambda *_a, **_k: AgentProfile(name="developer", description="Developer"),
        )
        provider = MagicMock()
        provider.initialize = AsyncMock(return_value=True)
        provider_manager = MagicMock()
        provider_manager.create_provider.return_value = provider
        monkeypatch.setattr(terminal_service, "provider_manager", provider_manager)
        monkeypatch.setattr(terminal_service, "db_create_terminal", lambda *a, **k: None)
        monkeypatch.setattr(terminal_service, "fifo_manager", MagicMock())
        monkeypatch.setattr(terminal_service, "status_monitor", MagicMock())

        def stop_runner():
            create_past_early_check.wait(timeout=15)
            session_service.stop_session(STOP_SESSION, declared_by="colin")
            release_create.set()

        stop_thread = threading.Thread(target=stop_runner)
        stop_thread.start()

        outcome: dict = {}

        def create_runner():
            try:
                asyncio.run(
                    session_service.create_session(
                        provider=None, agent_profile="developer", session_name=STOP_SESSION
                    )
                )
                outcome["ok"] = True
            except BaseException as exc:  # noqa: BLE001 - capture the racing create's result
                outcome["exc"] = exc

        create_thread = threading.Thread(target=create_runner)
        create_thread.start()
        create_thread.join(timeout=20)
        stop_thread.join(timeout=15)

        assert not stop_thread.is_alive()
        assert not create_thread.is_alive()
        # The racing create was refused under the still-stopped row.
        assert isinstance(outcome.get("exc"), ValueError), outcome
        assert "is stopped" in str(outcome["exc"])
        # Zero physical effects: no tmux session was created.
        assert physical_created == [], physical_created
        # The forwarded env stop preserved was not erased by the racing create.
        assert session_env.get_session_env(STOP_SESSION) == {"API_TOKEN": "sekret"}
        # The lifecycle row stayed stopped with its restore target intact.
        record = sl.describe(STOP_SESSION)
        assert record["lifecycle"] == sl.STOPPED
        assert record["restore_to"] == sl.WORKING

    def test_stop_session_normalizes_a_bare_name_for_every_physical_effect(self, monkeypatch):
        """P1: a bare name must be canonicalized once, before any effect.

        ``sl.stop()`` normalizes ``repair`` -> ``cao-repair`` internally, but
        ``stop_session`` took the physical claim, listed terminals, deleted
        panes, and checked/killed the backend on the raw name — so a bare-name
        stop collected nothing while writing ``cao-repair=stopped``, leaving
        the real fleet running under a suppressing declaration. Every physical
        and admission effect must see the canonical ``cao-...`` name.
        """
        from cli_agent_orchestrator.services import callback_recovery, terminal_service

        seen: dict = {}

        def _list(name):
            seen["list"] = name
            # No tmux_session so expected_session falls back to the stop name.
            return [{"id": "t1", "generation": "g1"}]

        def _delete(terminal_id, *, registry=None, **kwargs):
            seen["delete_expected_session"] = kwargs.get("expected_session")
            return True

        def _exists(name):
            seen["exists"] = name
            return True

        def _kill(name):
            seen["kill"] = name

        backend = MagicMock()
        backend.session_exists.side_effect = _exists
        backend.kill_session.side_effect = _kill
        monkeypatch.setattr(session_service, "get_backend", lambda: backend)
        monkeypatch.setattr(session_service, "list_terminals_by_session", _list)
        monkeypatch.setattr(terminal_service, "delete_terminal", _delete)

        real_claim = callback_recovery.session_lifecycle_claim

        def _claim_spy(_backend_kind, name):
            seen["claim"] = name
            return real_claim(_backend_kind, name)

        monkeypatch.setattr(callback_recovery, "session_lifecycle_claim", _claim_spy)

        session_service.stop_session("repair", declared_by="colin")

        assert seen["claim"] == "cao-repair", seen
        assert seen["list"] == "cao-repair", seen
        assert seen["delete_expected_session"] == "cao-repair", seen
        assert seen["exists"] == "cao-repair", seen
        assert seen["kill"] == "cao-repair", seen
        # The durable row is written under the canonical name.
        assert sl.describe("cao-repair")["lifecycle"] == sl.STOPPED
