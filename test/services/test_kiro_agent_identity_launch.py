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

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
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
