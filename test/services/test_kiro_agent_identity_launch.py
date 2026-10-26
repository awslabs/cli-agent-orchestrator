"""Kiro launches run the agent CAO asked for, or nothing (#1074).

kiro-cli does not refuse an ``--agent`` it cannot load: it prints ``agent "<name>"
not found, using "kiro_default"`` and starts its built-in default agent with every
built-in tool (shell, write, use_aws). ``create_terminal`` therefore refuses a Kiro
launch, before any window or row exists, when no agent directory kiro-cli reads
defines the agent; and ``KiroCliProvider.initialize`` refuses a pane whose header
names another agent before any task is typed.
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome, TerminalCleanupResult
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import (
    ProviderExitedInputBlockedError,
    TerminalInputBlockedReason,
    TerminalStatus,
)
from cli_agent_orchestrator.providers.kiro_capabilities import KiroCapabilities
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.kiro_identity import KiroAgentIdentityError
from cli_agent_orchestrator.services import install_service, terminal_service
from cli_agent_orchestrator.services.terminal_service import create_terminal

_MODULE = "cli_agent_orchestrator.services.terminal_service"

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


def _probe():
    return Mock(
        return_value=KiroCapabilities(
            version="2.32.0",
            flags=frozenset({"--agent-engine", "--agent", "--trust-all-tools"}),
            agent_engines=frozenset({"v2"}),
        )
    )


@pytest.fixture
def agent_dirs(tmp_path, monkeypatch):
    """An empty install directory, an empty home and a launch cwd, all under tmp_path."""
    installed = tmp_path / "installed-agents"
    installed.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", installed)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("KIRO_HOME", raising=False)
    return MagicMock(installed=installed, cwd=cwd, root=tmp_path)


@pytest.fixture
def allocation():
    """Patch every allocation create_terminal can make; keep the agent check real."""
    provider = MagicMock()
    provider.initialize = AsyncMock(return_value=True)
    provider.shell_baseline = None
    provider.runtime_variant = None
    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.delete_terminals_by_session"),
        patch(f"{_MODULE}.fifo_manager") as fifo,
        patch(f"{_MODULE}.provider_manager") as providers,
        patch(f"{_MODULE}.generate_terminal_id", return_value="test1234"),
        patch(f"{_MODULE}.generate_session_name", return_value="session"),
        patch(f"{_MODULE}.generate_window_name", return_value="developer-window"),
        patch(f"{_MODULE}.get_herdr_inbox_service", return_value=None),
    ):
        backend.return_value.session_exists.return_value = False
        backend.return_value.supports_event_inbox.return_value = True
        providers.create_provider.return_value = provider
        yield MagicMock(
            backend=backend.return_value,
            db_create=db_create,
            fifo=fifo,
            providers=providers,
            provider=provider,
        )


def _assert_nothing_allocated(allocation):
    allocation.backend.create_session.assert_not_called()
    allocation.backend.create_window.assert_not_called()
    allocation.db_create.assert_not_called()
    allocation.fifo.create_reader.assert_not_called()
    allocation.providers.create_provider.assert_not_called()


async def _create(agent_dirs, agent="developer", **kwargs):
    return await create_terminal(
        provider="kiro_cli",
        agent_profile=agent,
        new_session=True,
        working_directory=str(agent_dirs.cwd),
        kiro_capability_probe=_probe(),
        **kwargs,
    )


class TestBeforeLaunch:
    @pytest.mark.asyncio
    async def test_a_missing_agent_is_refused_before_any_allocation(self, agent_dirs, allocation):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            await _create(agent_dirs)

        assert excinfo.value.kind == "agent_not_found"
        message = str(excinfo.value)
        assert "kiro_default" in message
        assert "cao install developer --provider kiro_cli" in message
        assert str(agent_dirs.installed) in message
        _assert_nothing_allocated(allocation)

    @pytest.mark.asyncio
    async def test_an_installed_agent_launches_as_before(self, agent_dirs, allocation):
        """Back-compat: the same create, with the file `cao install` writes, is untouched."""
        (agent_dirs.installed / "developer.json").write_text(json.dumps({"name": "developer"}))

        terminal = await _create(agent_dirs)

        assert terminal.agent_profile == "developer"
        allocation.backend.create_session.assert_called_once()
        kwargs = allocation.providers.create_provider.call_args
        assert kwargs.args[4] == "developer"
        allocation.provider.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_workspace_agent_resolves_from_the_launch_cwd(self, agent_dirs, allocation):
        workspace = agent_dirs.cwd / ".kiro" / "agents"
        workspace.mkdir(parents=True)
        (workspace / "developer.json").write_text("{}")

        await _create(agent_dirs)

        allocation.providers.create_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_kiro_home_in_the_launch_env_resolves(self, agent_dirs, allocation):
        kiro_home = agent_dirs.root / "pane-kiro-home"
        (kiro_home / "agents").mkdir(parents=True)
        (kiro_home / "agents" / "developer.json").write_text("{}")

        await _create(agent_dirs, env_vars={"KIRO_HOME": str(kiro_home)})

        allocation.providers.create_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_kiro_home_in_the_session_env_resolves_for_a_new_window(
        self, agent_dirs, allocation
    ):
        """A window added to an existing session launches with the session env under
        its own, so a KIRO_HOME forwarded at session creation counts."""
        from cli_agent_orchestrator.services.session_env import (
            clear_session_env,
            set_session_env,
        )

        kiro_home = agent_dirs.root / "session-kiro-home"
        (kiro_home / "agents").mkdir(parents=True)
        (kiro_home / "agents" / "developer.json").write_text("{}")
        allocation.backend.session_exists.return_value = True
        allocation.backend.create_window.return_value = "developer-window"

        async def _add_window():
            return await create_terminal(
                provider="kiro_cli",
                agent_profile="developer",
                session_name="cao-existing",
                new_session=False,
                working_directory=str(agent_dirs.cwd),
                kiro_capability_probe=_probe(),
            )

        with pytest.raises(KiroAgentIdentityError):
            await _add_window()
        set_session_env("cao-existing", {"KIRO_HOME": str(kiro_home)})
        try:
            await _add_window()
        finally:
            clear_session_env("cao-existing")

        allocation.providers.create_provider.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_agent_store_scan_runs_off_the_event_loop(
        self, agent_dirs, allocation, monkeypatch
    ):
        """A miss parses every agent file in up to four directories; the create's
        other blocking calls beside it already run in a worker thread."""
        import threading

        (agent_dirs.installed / "developer.json").write_text("{}")
        threads = []
        real = terminal_service.require_kiro_agent_resolves

        def spy(*args, **kwargs):
            threads.append(threading.current_thread())
            return real(*args, **kwargs)

        monkeypatch.setattr(terminal_service, "require_kiro_agent_resolves", spy)

        await _create(agent_dirs)

        assert threads and threading.main_thread() not in threads

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent", ["kiro_default", "cao-null"])
    async def test_a_fallback_agent_name_is_refused_even_when_a_file_exists(
        self, agent_dirs, allocation, agent
    ):
        (agent_dirs.installed / f"{agent}.json").write_text(json.dumps({"name": agent}))

        with pytest.raises(KiroAgentIdentityError) as excinfo:
            await _create(agent_dirs, agent=agent)

        assert excinfo.value.kind == "fallback_agent"
        _assert_nothing_allocated(allocation)

    @pytest.mark.asyncio
    async def test_other_providers_are_not_checked(self, agent_dirs, allocation):
        terminal = await create_terminal(
            provider="claude_code",
            agent_profile="developer",
            new_session=True,
            working_directory=str(agent_dirs.cwd),
        )
        assert terminal.agent_profile == "developer"


class TestDeferredLaunchTypesNothingIntoAWrongAgent:
    """The deferred path (assign) runs initialize() then types the task; a refused
    identity must end the launch before dispatch_input is ever reached."""

    @pytest.mark.asyncio
    async def test_a_fallback_header_ends_the_launch_before_the_task(self, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0
        )
        surface = AsyncMock()
        dispatch = Mock()
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend") as backend,
            patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell", return_value=True),
            patch("cli_agent_orchestrator.providers.kiro_cli.wait_until_status", return_value=True),
            patch(
                "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
                side_effect=FileNotFoundError("no profile"),
            ),
            patch("cli_agent_orchestrator.services.status_monitor.status_monitor") as monitor,
            patch.object(terminal_service, "dispatch_input", dispatch),
            patch.object(terminal_service, "_surface_deferred_init_failure", surface),
            patch.object(terminal_service, "_clear_deferred_init_external_owner", AsyncMock()),
            patch.object(terminal_service, "update_terminal_shell_command"),
        ):
            monitor.get_status.return_value = TerminalStatus.IDLE
            backend.return_value.get_history.return_value = (
                "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
                '● agent "developer" not found, using "kiro_default"\n'
                "kiro_default · auto · ◔ 4% · Midway: 10h 20m\n"
                "›  ask a question or describe a task ↵\n"
            )
            provider = KiroCliProvider("1e293d67", "cao-session", "developer-abcd", "developer")

            task = terminal_service._schedule_deferred_init(
                provider, "1e293d67", "TASK-CANARY do the work", None, None
            )
            await task

        dispatch.assert_not_called()
        # The launch command is the only text the pane received.
        assert [c.args[2] for c in backend.return_value.send_keys.call_args_list] == [
            "kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer"
        ]
        backend.return_value.send_special_key.assert_not_called()
        surface.assert_awaited_once()
        kwargs = surface.await_args.kwargs
        assert kwargs["exception_type"] == "KiroAgentIdentityError"
        assert "kiro_default" in kwargs["message"] and "No task was sent" in kwargs["message"]


_FALLBACK_SCREEN = (
    "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
    '● agent "developer" not found, using "kiro_default"\n'
    "kiro_default · auto · ◔ 4% · Midway: 10h 20m\n"
    "›  ask a question or describe a task ↵\n"
)
_LAUNCH = "kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer"
_TID = "1e293d67"


@pytest.fixture
def external_owner_row(tmp_path, monkeypatch):
    """A real registry row for a deferred Kiro launch with no caller (``cao launch
    --headless … "task"``, ``POST /sessions`` with ``initial_message``), and one
    backend double for both the provider and the service, so every key typed into
    the pane and every window close is recorded in one place."""
    from cli_agent_orchestrator.clients.database import create_terminal as db_create

    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", logs)
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0
    )
    db_create(
        _TID,
        "cao-session",
        "developer-abcd",
        "kiro_cli",
        "developer",
        deferred_init_external_owner=True,
    )
    backend = MagicMock()
    backend.get_history.return_value = _FALLBACK_SCREEN
    backend.get_pane_working_directory.return_value = None
    backend.get_pane_current_command.return_value = "kiro-cli"
    backend.supports_event_inbox.return_value = False
    backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(
        TerminalCleanupOutcome.DELETED, "closed the exact window"
    )
    notify = Mock(wraps=terminal_service._notify_caller_of_deferred_failure)
    with (
        patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
        patch.object(terminal_service, "get_backend", return_value=backend),
        patch("cli_agent_orchestrator.services.inbox_service.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.providers.kiro_cli.wait_for_shell", return_value=True),
        patch("cli_agent_orchestrator.providers.kiro_cli.wait_until_status", return_value=True),
        patch(
            "cli_agent_orchestrator.providers.kiro_cli.load_agent_profile",
            side_effect=FileNotFoundError("no profile"),
        ),
        patch.object(terminal_service, "get_herdr_inbox_service", return_value=None),
        patch.object(terminal_service, "fifo_manager") as fifo,
        patch.object(terminal_service, "_notify_caller_of_deferred_failure", notify),
        patch.object(
            terminal_service.status_monitor, "get_status", return_value=TerminalStatus.IDLE
        ),
    ):
        fifo.stop_reader.return_value = True
        yield MagicMock(id=_TID, backend=backend, notify=notify)


def _typed(backend):
    return [c.args[2] for c in backend.send_keys.call_args_list]


async def _refused_deferred_launch(row, dispatch=None):
    provider = KiroCliProvider(row.id, "cao-session", "developer-abcd", "developer")
    with patch.object(terminal_service, "dispatch_input", dispatch or Mock()):
        task = terminal_service._schedule_deferred_init(
            provider, row.id, "TASK-CANARY do the work", None, None, delete_on_failure=False
        )
        await task


class TestARefusedIdentityIsNeverLeftRunning:
    """With no caller the failure row is kept for its external owner, but a refused
    identity is a live kiro_default with trust-all-tools accepted: its window is
    always closed, and nothing typed afterwards reaches it (#1074)."""

    @pytest.mark.asyncio
    async def test_the_window_is_closed_and_the_failure_row_is_kept(self, external_owner_row):
        row = external_owner_row
        dispatch = Mock()

        await _refused_deferred_launch(row, dispatch)

        dispatch.assert_not_called()
        assert _typed(row.backend) == [_LAUNCH]
        row.backend.send_special_key.assert_not_called()
        # Closed by exact terminal identity, never by the reusable window name.
        row.backend.cleanup_terminal_exact.assert_called_once_with(
            row.id, "cao-session", "developer-abcd", close=True
        )
        row.backend.kill_window.assert_not_called()
        metadata = terminal_service.get_terminal_metadata(row.id)
        assert metadata is not None
        assert metadata["deferred_init_failure"]["exception_type"] == "KiroAgentIdentityError"
        assert metadata["deferred_init_runtime_reclaimed"] is True
        assert terminal_service.should_retain_deferred_failure_tombstone(row.id)
        notification = row.notify.call_args.args[1]
        assert row.notify.call_args.args[3] is False  # the row stays with its owner
        assert "window was closed" in notification
        assert "retained for its external lifecycle owner" in notification

    @pytest.mark.asyncio
    async def test_no_input_reaches_the_refused_terminal_afterwards(self, external_owner_row):
        row = external_owner_row
        await _refused_deferred_launch(row)

        with pytest.raises(ProviderExitedInputBlockedError) as excinfo:
            terminal_service.dispatch_input(
                row.id, "SEND-CANARY", orchestration_type=OrchestrationType.SEND_MESSAGE
            )

        assert excinfo.value.reason == TerminalInputBlockedReason.INIT_FAILED
        assert _typed(row.backend) == [_LAUNCH]

    @pytest.mark.asyncio
    async def test_an_unconfirmed_close_still_refuses_input_and_says_so(self, external_owner_row):
        """Defense in depth: when the close cannot be proven, the pane may still be
        kiro_default at IDLE. dispatch_input and inbox delivery refuse it anyway."""
        from cli_agent_orchestrator.clients.database import create_inbox_message, get_inbox_messages
        from cli_agent_orchestrator.models.inbox import MessageStatus
        from cli_agent_orchestrator.services.inbox_service import InboxService

        row = external_owner_row
        row.backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(
            TerminalCleanupOutcome.UNKNOWN, "could not list tmux sessions"
        )

        await _refused_deferred_launch(row)

        metadata = terminal_service.get_terminal_metadata(row.id)
        assert metadata["deferred_init_runtime_reclaimed"] is False
        assert "could not be confirmed" in row.notify.call_args.args[1]
        assert "input to it is refused" in row.notify.call_args.args[1]

        with pytest.raises(ProviderExitedInputBlockedError):
            terminal_service.dispatch_input(row.id, "SEND-CANARY")
        create_inbox_message("0000aaaa", row.id, "INBOX-CANARY")
        InboxService().deliver_pending(row.id)

        assert _typed(row.backend) == [_LAUNCH]
        [message] = get_inbox_messages(row.id)
        assert message.status == MessageStatus.FAILED

    @pytest.mark.asyncio
    async def test_other_init_failures_keep_their_runtime_for_the_owner(self, external_owner_row):
        """Only a refused identity is reclaimed: other external-owner failures keep
        the runtime for inspection, as before."""
        row = external_owner_row
        provider = MagicMock()
        provider.initialize = AsyncMock(side_effect=TimeoutError("kiro-cli did not start"))

        task = terminal_service._schedule_deferred_init(
            provider, row.id, "TASK-CANARY", None, None, delete_on_failure=False
        )
        await task

        row.backend.cleanup_terminal_exact.assert_not_called()
        row.backend.kill_window.assert_not_called()
        metadata = terminal_service.get_terminal_metadata(row.id)
        assert metadata["deferred_init_failure"]["exception_type"] == "TimeoutError"
        assert metadata["deferred_init_runtime_reclaimed"] is False


class TestDispatchRefusesADurableInitFailure:
    def test_a_failure_only_in_the_fallback_sidecar_also_refuses(self, external_owner_row):
        """The durable record is the DB column or, after a DB outage, the sidecar."""
        row = external_owner_row
        assert terminal_service._write_deferred_failure_fallback(
            row.id, {"phase": "deferred_init", "kind": "provider_init_error"}
        )

        with pytest.raises(ProviderExitedInputBlockedError) as excinfo:
            terminal_service.dispatch_input(row.id, "SEND-CANARY")

        assert "provider_init_error" in str(excinfo.value)
        row.backend.send_keys.assert_not_called()

    def test_a_terminal_without_a_failure_is_still_typed_into(self, external_owner_row):
        """The gate is the failure record, not the external-owner bit."""
        row = external_owner_row
        provider = MagicMock()
        provider.paste_enter_count = 1
        provider.paste_submit_delay = 0
        provider.assume_processing_on_dispatch = False
        provider.shell_baseline = None
        with (
            patch.object(terminal_service.provider_manager, "get_provider", return_value=provider),
            patch.object(terminal_service, "_dialog_blocks_orchestrated_input", return_value=False),
        ):
            terminal_service.dispatch_input(row.id, "SEND-CANARY", frozen_memory="")

        assert _typed(row.backend) == ["SEND-CANARY"]
