"""Unit tests for the herdr inbox registration/unregistration wiring in terminal_service.

These tests verify the wiring between terminal lifecycle functions and the herdr inbox
service, in isolation from real tmux/herdr:

- create_terminal: registers the new terminal with the herdr inbox service when one is
  available (herdr path), skips registration when the service is None (tmux path), and
  never lets a registration failure tear down an otherwise-successful terminal.
- delete_terminal: unregisters from the herdr inbox service when available and is a
  no-op against the service when it is None.

Note on signature: create_terminal's real signature is
    create_terminal(provider, agent_profile, session_name=None, new_session=False,
                    working_directory=None, allowed_tools=None, registry=None)
so the keyword arguments below follow the implementation, not the (stale) task brief.
"""

import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    delete_terminal,
)

# Fixed identifiers used across the tests so assertions can be exact.
TERMINAL_ID = "term-abc123"
SESSION_NAME = "cao-test-session"
WINDOW_NAME = "developer-wxyz"
PANE_ID = "%42"

# Module path prefix for patch targets (all dependencies are imported into the
# terminal_service namespace, so they are patched there, not at their origin).
_TS = "cli_agent_orchestrator.services.terminal_service."


@pytest.fixture
def create_mocks():
    """Patch every external dependency of create_terminal and yield the mocks.

    get_backend() returns a process-wide singleton in production, so a single MagicMock
    backs every get_backend() call here; session_exists/create_window/get_pane_id/pipe_pane
    are configured on that one object via ``get_backend.return_value``.

    Defaults model the happy path on the herdr branch:
    - existing tmux session (session_exists -> True), so new_session=False succeeds
    - load_agent_profile -> None, which skips allowed-tools resolution and model lookup
    - provider creates cleanly; shell_baseline is a MagicMock (not str) so it is treated
      as None and update_terminal_shell_command is never called
    - get_herdr_inbox_service -> a live mock service
    """
    with contextlib.ExitStack() as stack:

        def p(name):
            return stack.enter_context(patch(_TS + name))

        m = SimpleNamespace(
            get_herdr_inbox_service=p("get_herdr_inbox_service"),
            get_backend=p("get_backend"),
            db_create_terminal=p("db_create_terminal"),
            provider_manager=p("provider_manager"),
            generate_terminal_id=p("generate_terminal_id"),
            generate_session_name=p("generate_session_name"),
            generate_window_name=p("generate_window_name"),
            load_agent_profile=p("load_agent_profile"),
            build_skill_catalog=p("build_skill_catalog"),
            dispatch_plugin_event=p("dispatch_plugin_event"),
            update_terminal_shell_command=p("update_terminal_shell_command"),
            # TERMINAL_LOG_DIR is a Path; a MagicMock supports `/` (__truediv__),
            # .touch(), and str(), so log-file setup becomes a no-op.
            TERMINAL_LOG_DIR=p("TERMINAL_LOG_DIR"),
        )

        m.generate_terminal_id.return_value = TERMINAL_ID
        m.generate_window_name.return_value = "developer-base"
        m.load_agent_profile.return_value = None

        backend = m.get_backend.return_value
        backend.session_exists.return_value = True
        backend.create_window.return_value = WINDOW_NAME
        backend.get_pane_id.return_value = PANE_ID

        service = MagicMock()
        m.get_herdr_inbox_service.return_value = service
        m.service = service
        m.backend = backend

        yield m


class TestCreateTerminalHerdrRegistration:
    """create_terminal -> herdr inbox registration wiring."""

    def test_create_terminal_registers_with_herdr_inbox(self, create_mocks):
        """When a herdr inbox service exists, the new terminal is registered with it."""
        # Arrange
        m = create_mocks

        # Act
        terminal = create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert: pane id resolved for the created window, then registered with is_kiro=False
        m.backend.get_pane_id.assert_called_once_with(TERMINAL_ID, SESSION_NAME, WINDOW_NAME)
        m.service.register_terminal.assert_called_once_with(TERMINAL_ID, PANE_ID, False)
        assert isinstance(terminal, Terminal)
        assert terminal.id == TERMINAL_ID

    def test_create_terminal_no_registration_when_service_none(self, create_mocks):
        """On the tmux path (service is None) no registration is attempted."""
        # Arrange
        m = create_mocks
        m.get_herdr_inbox_service.return_value = None

        # Act
        terminal = create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert: guard short-circuits before pane lookup or registration
        m.backend.get_pane_id.assert_not_called()
        m.service.register_terminal.assert_not_called()
        assert isinstance(terminal, Terminal)
        assert terminal.id == TERMINAL_ID

    def test_create_terminal_registration_failure_does_not_kill_terminal(self, create_mocks):
        """A registration failure is swallowed; the terminal is still created and returned."""
        # Arrange: pane id lookup blows up (e.g. TerminalNotFoundError) inside the
        # registration block. The inner try/except must contain it.
        m = create_mocks
        m.backend.get_pane_id.side_effect = RuntimeError("pane not found")

        # Act
        terminal = create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert: creation succeeded and no exception propagated
        assert isinstance(terminal, Terminal)
        assert terminal.id == TERMINAL_ID
        # registration never completed (failed before the register call)
        m.service.register_terminal.assert_not_called()
        # the outer failure-cleanup path was NOT triggered -> terminal not torn down
        m.backend.kill_session.assert_not_called()
        m.provider_manager.cleanup_provider.assert_not_called()

    def test_create_terminal_kiro_provider_sets_is_kiro_true(self, create_mocks):
        """A kiro_cli provider registers with is_kiro=True."""
        # Arrange
        m = create_mocks

        # Act
        create_terminal(
            provider=ProviderType.KIRO_CLI.value,
            agent_profile="developer",
            session_name=SESSION_NAME,
        )

        # Assert
        m.service.register_terminal.assert_called_once_with(TERMINAL_ID, PANE_ID, True)


@pytest.fixture
def delete_mocks():
    """Patch delete_terminal's dependencies and yield the mocks.

    get_terminal_metadata -> None so the scrollback-snapshot / stop-pipe-pane / kill-window
    block (which needs the backend) is skipped, keeping these tests focused on the herdr
    unregistration wiring. db_delete_terminal -> True so delete_terminal returns True.
    """
    with contextlib.ExitStack() as stack:

        def p(name):
            return stack.enter_context(patch(_TS + name))

        m = SimpleNamespace(
            get_herdr_inbox_service=p("get_herdr_inbox_service"),
            get_terminal_metadata=p("get_terminal_metadata"),
            provider_manager=p("provider_manager"),
            db_delete_terminal=p("db_delete_terminal"),
            dispatch_plugin_event=p("dispatch_plugin_event"),
        )

        m.get_terminal_metadata.return_value = None
        m.db_delete_terminal.return_value = True

        service = MagicMock()
        m.get_herdr_inbox_service.return_value = service
        m.service = service

        yield m


class TestDeleteTerminalHerdrUnregistration:
    """delete_terminal -> herdr inbox unregistration wiring."""

    def test_delete_terminal_unregisters_from_herdr_inbox(self, delete_mocks):
        """When a herdr inbox service exists, the terminal is unregistered from it."""
        # Arrange
        m = delete_mocks

        # Act
        result = delete_terminal(TERMINAL_ID)

        # Assert
        m.service.unregister_terminal.assert_called_once_with(TERMINAL_ID)
        assert result is True

    def test_delete_terminal_no_unregistration_when_service_none(self, delete_mocks):
        """On the tmux path (service is None) no unregister call is made.

        If the None-guard were missing, the code would call unregister_terminal on None
        and raise AttributeError; a clean True return proves the guard holds.
        """
        # Arrange
        m = delete_mocks
        m.get_herdr_inbox_service.return_value = None

        # Act
        result = delete_terminal(TERMINAL_ID)

        # Assert
        m.service.unregister_terminal.assert_not_called()
        assert result is True
