"""Kiro launches run the agent CAO asked for, or nothing (#1074).

kiro-cli does not refuse an ``--agent`` it cannot load: it prints ``agent "<name>"
not found, using "kiro_default"`` and starts its built-in default agent with every
built-in tool (shell, write, use_aws). ``create_terminal`` therefore refuses a Kiro
launch, before any window or row exists, when no agent directory kiro-cli reads
defines the agent; and ``KiroCliProvider.initialize`` refuses a pane whose header
names another agent before any task is typed.

A Kiro terminal takes typed input only once its row records that this check
passed (``terminals.identity_verified``); a Kiro runtime whose init ended
without that record has its window closed, even for an external owner.
"""

import asyncio
import json
import threading
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome, TerminalCleanupResult
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    terminal_id_in_use,
    update_terminal_deferred_init_failure,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import MessageStatus, OrchestrationType
from cli_agent_orchestrator.models.terminal import (
    ProviderExitedInputBlockedError,
    TerminalInputBlockedError,
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
    # A healthy Kiro launch: the provider's header check passed, and the row it
    # is recorded on is the patched db_create_terminal's, so the record write is
    # patched too.
    provider.identity_verified = True
    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch(f"{_MODULE}.get_backend") as backend,
        patch(f"{_MODULE}.db_create_terminal") as db_create,
        patch(f"{_MODULE}.mark_terminal_identity_verified", return_value=True),
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
    async def test_a_missing_agent_is_refused_before_any_allocation(
        self, agent_dirs, allocation, caplog
    ):
        with (
            caplog.at_level("WARNING", logger="cli_agent_orchestrator.providers.kiro_identity"),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await _create(agent_dirs)

        assert excinfo.value.kind == "agent_not_found"
        message = str(excinfo.value)
        assert "kiro_default" in message
        assert "cao install developer --provider kiro_cli" in message
        # The searched directories are in the server log, never in the message.
        assert str(agent_dirs.root) not in message
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert str(agent_dirs.installed) in logged and str(agent_dirs.cwd) in logged
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
_HEALTHY_SCREEN = (
    "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer\n"
    "developer · auto · ◔ 4% · Midway: 10h 20m\n"
    "›  ask a question or describe a task ↵\n"
)
_LAUNCH = "kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer"
_TID = "1e293d67"


@pytest.fixture
def pane(tmp_path, monkeypatch):
    """One backend double for the provider, the service and the inbox, so every key
    typed into the pane and every window close is recorded in one place."""
    logs = tmp_path / "logs"
    logs.mkdir()
    monkeypatch.setattr(terminal_service, "TERMINAL_LOG_DIR", logs)
    monkeypatch.setattr(
        "cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0
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
        yield MagicMock(backend=backend, notify=notify, logs=logs)


def _row(terminal_id=_TID, *, identity_verified, provider="kiro_cli", external_owner=True, **kw):
    """A real registry row with its identity record set explicitly (#1074)."""
    from cli_agent_orchestrator.clients.database import create_terminal as db_create

    db_create(
        terminal_id,
        "cao-session",
        "developer-abcd",
        provider,
        "developer",
        deferred_init_external_owner=external_owner,
        identity_verified=identity_verified,
        **kw,
    )
    return terminal_id


@pytest.fixture
def external_owner_row(pane):
    """A deferred Kiro launch with no caller (``cao launch --headless … "task"``,
    ``POST /sessions`` with ``initial_message``), as create_terminal writes it: the
    external-owner bit set and ``identity_verified`` False."""
    _row(identity_verified=False)
    pane.id = _TID
    return pane


@pytest.fixture
def kiro_create(pane, agent_dirs):
    """A real create_terminal of an installed ``developer`` against the real
    registry, with a real KiroCliProvider on the pane double."""
    (agent_dirs.installed / "developer.json").write_text(json.dumps({"name": "developer"}))
    providers = []

    def _provider(provider, terminal_id, session_name, window_name, agent_profile, *a, **k):
        providers.append(KiroCliProvider(terminal_id, session_name, window_name, agent_profile))
        return providers[-1]

    # Event-inbox backend: no FIFO and no pipe-pane Enter nudge, so the keys the
    # pane receives are exactly the launch command and whatever is typed later.
    pane.backend.supports_event_inbox.return_value = True
    pane.backend.session_exists.return_value = False
    _a_launched_kiro(pane.backend)

    async def _create_one(**kwargs):
        return await create_terminal(
            provider="kiro_cli",
            agent_profile="developer",
            new_session=True,
            working_directory=str(agent_dirs.cwd),
            kiro_capability_probe=_probe(),
            **kwargs,
        )

    with (
        patch(
            f"{_MODULE}.agent_profiles.load_agent_profile",
            return_value=AgentProfile(name="developer", description="Developer"),
        ),
        patch.object(terminal_service.provider_manager, "create_provider", side_effect=_provider),
        patch(f"{_MODULE}.generate_terminal_id", return_value=_TID),
    ):
        yield MagicMock(create=_create_one, providers=providers)


def _typed(backend):
    return [c.args[2] for c in backend.send_keys.call_args_list]


def _a_launched_kiro(backend):
    """The pane runs zsh when the provider takes its shell baseline, and kiro-cli
    after that, so the bare-shell guard sees a running provider."""
    commands = iter(["zsh"])
    backend.get_pane_current_command.side_effect = lambda *a, **k: next(commands, "kiro-cli")


@contextmanager
def _a_typing_provider():
    """A registered provider that dispatch_input types into without prompts."""
    provider = MagicMock()
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0
    provider.assume_processing_on_dispatch = False
    provider.shell_baseline = None
    with (
        patch.object(terminal_service.provider_manager, "get_provider", return_value=provider),
        patch.object(terminal_service, "_dialog_blocks_orchestrated_input", return_value=False),
    ):
        yield provider


def _types(terminal_id, backend, text="SEND-CANARY"):
    """dispatch_input accepts the text and it reaches the pane."""
    with _a_typing_provider():
        terminal_service.dispatch_input(terminal_id, text, frozen_memory="")
    assert text in _typed(backend)


def _refused(terminal_id, reason=TerminalInputBlockedReason.INIT_FAILED):
    """dispatch_input and send_special_key both refuse, for ``reason``."""
    with _a_typing_provider():
        with pytest.raises(TerminalInputBlockedError) as typed:
            terminal_service.dispatch_input(terminal_id, "SEND-CANARY", frozen_memory="")
        with pytest.raises(TerminalInputBlockedError) as keyed:
            terminal_service.send_special_key(terminal_id, "Enter")
    for excinfo in (typed, keyed):
        assert excinfo.value.reason == reason
        # A permanent refusal is the subclass the deferred path and the inbox
        # treat as final; a pending one is not.
        permanent = isinstance(excinfo.value, ProviderExitedInputBlockedError)
        assert permanent is (reason == TerminalInputBlockedReason.INIT_FAILED)
    return typed.value


def _inbox_status(terminal_id):
    """Queue one message for the terminal, run inbox delivery once, read its status."""
    from cli_agent_orchestrator.clients.database import create_inbox_message, get_inbox_messages
    from cli_agent_orchestrator.services.inbox_service import InboxService

    create_inbox_message("0000aaaa", terminal_id, "INBOX-CANARY")
    InboxService().deliver_pending(terminal_id)
    [message] = get_inbox_messages(terminal_id)
    return message.status


def _failure(terminal_id):
    return terminal_service.get_terminal_metadata(terminal_id)["deferred_init_failure"]


async def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        await asyncio.sleep(0.01)


def _blocking_header_read(backend, screen=_HEALTHY_SCREEN):
    """Hold the provider's header read (Step 4) until released."""
    reached, release = threading.Event(), threading.Event()

    def history(*args, **kwargs):
        if kwargs.get("visible_only"):
            reached.set()
            release.wait(10)
        return screen

    backend.get_history.side_effect = history
    return reached, release


async def _refused_deferred_launch(row, dispatch=None):
    provider = KiroCliProvider(row.id, "cao-session", "developer-abcd", "developer")
    with patch.object(terminal_service, "dispatch_input", dispatch or Mock()):
        task = terminal_service._schedule_deferred_init(
            provider, row.id, "TASK-CANARY do the work", None, None, delete_on_failure=False
        )
        await task


async def _failed_deferred_launch(terminal_id, error, provider="kiro_cli"):
    """A deferred launch with no caller whose initialize() raises ``error``."""
    double = MagicMock()
    double.initialize = AsyncMock(side_effect=error)
    task = terminal_service._schedule_deferred_init(
        double,
        terminal_id,
        "TASK-CANARY",
        None,
        None,
        delete_on_failure=False,
        provider=provider,
    )
    await task


class TestHealthyLaunchesRecordTheCheckBeforeAnyTask:
    """A healthy Kiro launch is unchanged: one launch command, the task typed once,
    and the row records the passed check before that task is typed (#1074)."""

    @pytest.mark.asyncio
    async def test_a_healthy_deferred_launch_is_verified_before_its_task(self, pane):
        tid = _row(identity_verified=False)
        pane.backend.get_history.return_value = _HEALTHY_SCREEN
        _a_launched_kiro(pane.backend)
        seen = []
        real_dispatch = terminal_service.dispatch_input

        def spy(terminal_id, message, **kwargs):
            seen.append(terminal_service.get_terminal_metadata(terminal_id)["identity_verified"])
            return real_dispatch(terminal_id, message, **kwargs)

        provider = KiroCliProvider(tid, "cao-session", "developer-abcd", "developer")
        with (
            _a_typing_provider(),
            patch.object(terminal_service, "dispatch_input", spy),
            patch.object(
                terminal_service, "inject_memory_context", side_effect=lambda m, *a, **k: m
            ),
            patch.object(
                terminal_service,
                "_confirm_worker_started_or_resubmit",
                AsyncMock(return_value=True),
            ),
        ):
            task = terminal_service._schedule_deferred_init(
                provider, tid, "TASK-CANARY do the work", None, None, delete_on_failure=False
            )
            await task

        assert seen == [True]
        assert _typed(pane.backend) == [_LAUNCH, "TASK-CANARY do the work"]
        pane.backend.send_special_key.assert_not_called()
        assert _failure(tid) is None
        assert not terminal_service._is_identity_check_in_flight(tid)

    @pytest.mark.asyncio
    async def test_a_healthy_synchronous_create_is_verified_when_it_returns(
        self, kiro_create, pane
    ):
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry

        pane.backend.get_history.return_value = _HEALTHY_SCREEN

        terminal = await kiro_create.create()

        assert terminal_service.get_terminal_metadata(terminal.id)["identity_verified"] is True
        assert not terminal_service._is_identity_check_in_flight(terminal.id)
        app.state.plugin_registry = PluginRegistry()
        with _a_typing_provider():
            response = TestClient(app).post(
                f"/terminals/{terminal.id}/input",
                params={"message": "API-CANARY"},
                headers={"Host": "localhost"},
            )
        assert response.status_code == 200
        assert _typed(pane.backend) == [_LAUNCH, "API-CANARY"]
        pane.backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_production_create_writes_false_for_kiro_and_null_otherwise(
        self, agent_dirs, allocation
    ):
        (agent_dirs.installed / "developer.json").write_text("{}")
        await _create(agent_dirs)
        await create_terminal(
            provider="claude_code",
            agent_profile="developer",
            new_session=True,
            working_directory=str(agent_dirs.cwd),
        )

        kiro, other = allocation.db_create.call_args_list
        assert kiro.kwargs["identity_verified"] is False
        assert other.kwargs["identity_verified"] is None


class TestTheIdIsAllocatedBeforeItIsMarkedInFlight:
    """A Kiro create marks its identity check in flight only on the id the
    allocator accepted (#1088, #1074). A candidate it rejects is still another
    terminal's, live or retained, so marking it would leave a stale mark on that
    id, and releasing one would end the other terminal's pending check early."""

    @staticmethod
    @contextmanager
    def _marks_recorded():
        marked = []
        real_mark = terminal_service._mark_identity_check_in_flight

        def _spy(terminal_id):
            # Recorded with whether the row was already visible at the mark.
            marked.append((terminal_id, terminal_service.get_terminal_metadata(terminal_id)))
            real_mark(terminal_id)

        with patch.object(terminal_service, "_mark_identity_check_in_flight", _spy):
            yield marked

    @pytest.mark.asyncio
    async def test_a_first_id_still_referenced_is_redrawn_and_never_marked(self, kiro_create, pane):
        # A deleted terminal whose idempotency mapping is retained still holds its id.
        db_create_terminal(
            "aaaaaaaa",
            "cao-old",
            "developer-old",
            "kiro_cli",
            "developer",
            idempotency_key="retained-key",
            request_fingerprint="retained-fp",
        )
        assert db_delete_terminal("aaaaaaaa", end_reason="deleted") is True
        assert terminal_id_in_use("aaaaaaaa") is True
        pane.backend.get_history.return_value = _HEALTHY_SCREEN

        with (
            patch(f"{_MODULE}.generate_terminal_id", side_effect=["aaaaaaaa", "bbbbbbbb"]),
            self._marks_recorded() as marked,
        ):
            terminal = await kiro_create.create()

        assert terminal.id == "bbbbbbbb"
        assert marked == [("bbbbbbbb", None)]
        assert not terminal_service._is_identity_check_in_flight("aaaaaaaa")
        assert not terminal_service._is_identity_check_in_flight("bbbbbbbb")
        assert terminal_service.get_terminal_metadata("aaaaaaaa") is None
        assert terminal_service.get_terminal_metadata("bbbbbbbb")["identity_verified"] is True
        assert pane.backend.create_session.call_args.args[2] == "bbbbbbbb"

    @pytest.mark.asyncio
    async def test_a_first_id_held_by_a_terminal_mid_check_keeps_that_terminals_mark(
        self, kiro_create, pane
    ):
        held = _row("aaaaaaaa", identity_verified=False)
        terminal_service._mark_identity_check_in_flight(held)
        pane.backend.get_history.return_value = _HEALTHY_SCREEN
        try:
            with patch(f"{_MODULE}.generate_terminal_id", side_effect=[held, "bbbbbbbb"]):
                terminal = await kiro_create.create()

            assert terminal.id == "bbbbbbbb"
            assert terminal_service._is_identity_check_in_flight(held)
            assert terminal_service.get_terminal_metadata(held)["identity_verified"] is False
            _refused(held, TerminalInputBlockedReason.IDENTITY_PENDING)
        finally:
            terminal_service._clear_identity_check_in_flight(held)

    @pytest.mark.asyncio
    async def test_running_out_of_ids_fails_before_any_mark_or_window(self, kiro_create, pane):
        held = _row("aaaaaaaa", identity_verified=True)

        with (
            patch(f"{_MODULE}.generate_terminal_id", return_value=held),
            self._marks_recorded() as marked,
            pytest.raises(RuntimeError, match="Could not allocate a terminal id"),
        ):
            await kiro_create.create()

        assert marked == []
        assert not terminal_service._is_identity_check_in_flight(held)
        pane.backend.create_session.assert_not_called()
        pane.backend.create_window.assert_not_called()
        # The failed create rolls back nothing of the terminal that holds the id.
        assert terminal_service.get_terminal_metadata(held)["identity_verified"] is True

    @pytest.mark.asyncio
    async def test_running_out_of_ids_answers_500_on_the_create_route(self, kiro_create, pane):
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry

        held = _row("aaaaaaaa", identity_verified=True)
        app.state.plugin_registry = PluginRegistry()

        with (
            patch(f"{_MODULE}.generate_terminal_id", return_value=held),
            self._marks_recorded() as marked,
        ):
            response = TestClient(app).post(
                "/sessions",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
                headers={"Host": "localhost"},
            )

        assert response.status_code == 500
        assert "Could not allocate a terminal id" in response.json()["detail"]
        assert marked == []
        assert not terminal_service._is_identity_check_in_flight(held)
        pane.backend.create_session.assert_not_called()


class TestAKiroInitThatEndsBeforeItsCheckIsUntrusted:
    """A Kiro launch whose init ends before its header check passed is reclaimed
    and refused input, whatever ended it (#1074): CAO never confirmed which agent
    that pane is running."""

    @pytest.mark.asyncio
    async def test_a_timeout_before_the_header_check(self, external_owner_row):
        row = external_owner_row
        provider = KiroCliProvider(row.id, "cao-session", "developer-abcd", "developer")
        with patch(
            "cli_agent_orchestrator.providers.kiro_cli.wait_until_status", return_value=False
        ):
            task = terminal_service._schedule_deferred_init(
                provider, row.id, "TASK-CANARY", None, None, delete_on_failure=False
            )
            await task

        failure = _failure(row.id)
        assert failure["exception_type"] == "TimeoutError"
        assert failure["untrusted_runtime"] is True
        row.backend.cleanup_terminal_exact.assert_called_once_with(
            row.id, "cao-session", "developer-abcd", close=True
        )
        assert terminal_service.get_terminal_metadata(row.id)["deferred_init_runtime_reclaimed"]
        notification = row.notify.call_args.args[1]
        assert "had not verified its Kiro agent identity" in notification
        assert "window was closed" in notification
        _refused(row.id)
        assert _inbox_status(row.id) == MessageStatus.FAILED
        assert _typed(row.backend) == [_LAUNCH]
        row.backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_sidecar_records_the_decision_when_sqlite_is_down(self, external_owner_row):
        from sqlalchemy.exc import OperationalError

        row = external_owner_row
        with patch.object(
            terminal_service,
            "update_terminal_deferred_init_failure",
            side_effect=OperationalError("UPDATE", {}, Exception("database is locked")),
        ):
            await _failed_deferred_launch(row.id, TimeoutError("kiro-cli did not start"))

        assert _failure(row.id) is None
        sidecar = terminal_service._read_deferred_failure_fallback(row.id)
        assert sidecar["untrusted_runtime"] is True
        assert sidecar["exception_type"] == "TimeoutError"
        row.backend.cleanup_terminal_exact.assert_called_once()
        _refused(row.id)

    @pytest.mark.asyncio
    async def test_a_restart_during_init(self, pane):
        """The reviewer's live case E: a Kiro row still reading False, owned by an
        external observer and not being initialized by this process."""
        tid = _row(identity_verified=False)

        assert await terminal_service.recover_interrupted_deferred_init_external_owners()

        failure = _failure(tid)
        assert (failure["kind"], failure["exception_type"]) == ("interrupted_init", "ServerRestart")
        assert failure["untrusted_runtime"] is True
        pane.backend.cleanup_terminal_exact.assert_called_once_with(
            tid, "cao-session", "developer-abcd", close=True
        )
        assert terminal_service.get_terminal_metadata(tid)["deferred_init_runtime_reclaimed"]
        _refused(tid)
        assert _typed(pane.backend) == []

    @pytest.mark.asyncio
    async def test_a_subclass_of_the_identity_error_is_reclaimed_and_refused(
        self, external_owner_row
    ):
        """No exception class name decides the reclaim or the gate."""

        class RenamedIdentityError(KiroAgentIdentityError):
            pass

        row = external_owner_row
        await _failed_deferred_launch(
            row.id, RenamedIdentityError("identity_mismatch", "developer", "wrong agent")
        )

        assert _failure(row.id)["exception_type"] == "RenamedIdentityError"
        row.backend.cleanup_terminal_exact.assert_called_once()
        _refused(row.id)

    def test_a_renamed_exception_type_on_an_unverified_row_is_still_refused(self, pane):
        tid = _row(identity_verified=False)
        assert update_terminal_deferred_init_failure(
            tid,
            {"phase": "deferred_init", "kind": "provider_init_error", "exception_type": "Renamed"},
        )

        _refused(tid)

    @pytest.mark.asyncio
    async def test_a_cancelled_init_is_refused_permanently_not_as_pending(self, pane):
        tid = _row(identity_verified=False)
        started = asyncio.Event()

        async def _initialize():
            started.set()
            await asyncio.Event().wait()

        double = MagicMock()
        double.initialize = _initialize
        task = terminal_service._schedule_deferred_init(
            double, tid, None, None, None, delete_on_failure=False, provider="kiro_cli"
        )
        await started.wait()
        _refused(tid, TerminalInputBlockedReason.IDENTITY_PENDING)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert _failure(tid) is None
        assert "(identity_unverified)" in str(_refused(tid))


class TestRestartsAroundTheRecord:
    @pytest.mark.asyncio
    async def test_a_restart_during_delivery_after_verification_accepts_the_resend(self, pane):
        """Verified before the restart: the pane is the agent CAO asked for, so the
        interrupted_init row keeps its runtime and accepts the re-send, and "re-send
        the task" stays true."""
        tid = _row(identity_verified=True, initial_delivery={"state": "pending"})

        assert await terminal_service.recover_interrupted_initial_deliveries()

        failure = _failure(tid)
        assert failure["kind"] == "interrupted_init"
        assert failure["untrusted_runtime"] is False
        pane.backend.cleanup_terminal_exact.assert_not_called()
        _types(tid, pane.backend, "RESEND-CANARY")

    @pytest.mark.asyncio
    async def test_a_restart_during_delivery_before_verification_is_reclaimed(self, pane):
        tid = _row(
            identity_verified=False,
            external_owner=False,
            caller_id="0000aaaa",
            initial_delivery={"state": "pending"},
        )

        assert await terminal_service.recover_interrupted_initial_deliveries()

        assert _failure(tid)["untrusted_runtime"] is True
        pane.backend.cleanup_terminal_exact.assert_called_once_with(
            tid, "cao-session", "developer-abcd", close=True
        )
        assert terminal_service.get_terminal_metadata(tid) is not None  # never deleted
        _refused(tid)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("external_owner", [True, False], ids=["no-caller", "caller"])
    async def test_a_legacy_row_keeps_its_window_at_upgrade(self, pane, external_owner):
        """Provisional (#1074): a Kiro pane launched before the column, whose initial
        delivery had not settled at the upgrade restart, is grandfathered like any
        live legacy row. Its window stays open; the sweep's failure is still
        recorded, so input to it is refused."""
        kw = {} if external_owner else {"caller_id": "0000aaaa"}
        tid = _row(
            identity_verified=None,
            external_owner=external_owner,
            initial_delivery={"state": "pending"},
            **kw,
        )

        assert await terminal_service.recover_interrupted_initial_deliveries()

        failure = _failure(tid)
        assert (failure["kind"], failure["untrusted_runtime"]) == ("interrupted_init", False)
        pane.backend.cleanup_terminal_exact.assert_not_called()
        assert "(interrupted_init)" in str(_refused(tid))
        assert _typed(pane.backend) == []

    def test_only_a_row_this_version_wrote_false_is_untrusted(self):
        decide = terminal_service._runtime_untrusted_from_row
        assert decide({"provider": "kiro_cli", "identity_verified": False}) is True
        assert decide({"provider": "kiro_cli", "identity_verified": None}) is False
        assert decide({"provider": "kiro_cli", "identity_verified": True}) is False
        assert decide({"provider": "claude_code", "identity_verified": False}) is False


class TestInputBeforeOrWithoutTheRecord:
    @pytest.mark.asyncio
    async def test_input_during_init_waits_and_is_delivered_once_verified(self, kiro_create, pane):
        """The race the inbox could hit: Kiro reaches its prompt before CAO reads
        the header. Input is refused as pending, the inbox message stays pending,
        and it is delivered once the check is recorded."""
        reached, release = _blocking_header_read(pane.backend)
        try:
            terminal = await kiro_create.create(defer_init=True)
            await _until(reached.is_set)

            assert terminal_service.get_terminal_metadata(terminal.id)["identity_verified"] is False
            _refused(terminal.id, TerminalInputBlockedReason.IDENTITY_PENDING)
            assert _inbox_status(terminal.id) == MessageStatus.PENDING
        finally:
            release.set()
        await asyncio.gather(*list(terminal_service._deferred_init_tasks))

        from cli_agent_orchestrator.clients.database import get_inbox_messages
        from cli_agent_orchestrator.services.inbox_service import InboxService

        assert terminal_service.get_terminal_metadata(terminal.id)["identity_verified"] is True
        with _a_typing_provider():
            InboxService().deliver_pending(terminal.id)
        [message] = get_inbox_messages(terminal.id)
        assert message.status == MessageStatus.DELIVERED
        assert _typed(pane.backend) == [_LAUNCH, "INBOX-CANARY"]

    @pytest.mark.asyncio
    async def test_input_during_a_synchronous_create_waits_not_fails(self, kiro_create, pane):
        """The same race as the deferred launch above, but for a synchronous
        create: the pre-insert in-flight mark is this path's only fence (the
        deferred path also re-marks when its background task starts). While
        the create is still inside ``initialize()``, input must be pending,
        not a permanent failure."""
        reached, release = _blocking_header_read(pane.backend)
        try:
            create = asyncio.ensure_future(kiro_create.create())
            await _until(reached.is_set)

            assert terminal_service.get_terminal_metadata(_TID)["identity_verified"] is False
            _refused(_TID, TerminalInputBlockedReason.IDENTITY_PENDING)
            assert _inbox_status(_TID) == MessageStatus.PENDING
        finally:
            release.set()
        await create

        assert terminal_service.get_terminal_metadata(_TID)["identity_verified"] is True

    def test_a_stranded_row_is_refused_and_its_inbox_message_fails(self, pane):
        tid = _row(identity_verified=False)

        assert "(identity_unverified)" in str(_refused(tid))
        assert _inbox_status(tid) == MessageStatus.FAILED
        assert _typed(pane.backend) == []

    @pytest.mark.asyncio
    async def test_a_synchronous_create_stopped_mid_init_leaves_a_refused_row(
        self, kiro_create, pane
    ):
        """What a restart during a synchronous create leaves behind: a row written
        by the create, reading False, that no process is initializing any more."""
        reached, release = _blocking_header_read(pane.backend)
        try:
            create = asyncio.ensure_future(kiro_create.create())
            await _until(reached.is_set)
            create.cancel()
            with pytest.raises(asyncio.CancelledError):
                await create
        finally:
            release.set()

        assert terminal_service.get_terminal_metadata(_TID)["identity_verified"] is False
        assert "(identity_unverified)" in str(_refused(_TID))
        assert _typed(pane.backend) == [_LAUNCH]


class TestARecordThatLandsBetweenTheRowReadAndTheGate:
    """The caller reads the row, then the record commits True and releases the
    in-flight mark, then the gate runs on the caller's stale row. The pane is
    verified and healthy, so the refusal must not be the permanent one."""

    @staticmethod
    def _verified_provider():
        return Mock(identity_verified=True)

    @pytest.mark.asyncio
    async def test_the_gate_accepts_the_stale_row(self, pane):
        tid = _row(identity_verified=False)
        terminal_service._mark_identity_check_in_flight(tid)
        stale = terminal_service.get_terminal_metadata(tid)

        await terminal_service._record_kiro_identity_verified(tid, self._verified_provider())

        assert not terminal_service._is_identity_check_in_flight(tid)
        assert stale["identity_verified"] is False
        assert terminal_service.terminal_input_refusal(tid, stale) is None

    @pytest.mark.asyncio
    async def test_dispatch_input_and_send_special_key_type_into_the_pane(self, pane):
        tid = _row(identity_verified=False)
        terminal_service._mark_identity_check_in_flight(tid)
        stale = terminal_service.get_terminal_metadata(tid)
        await terminal_service._record_kiro_identity_verified(tid, self._verified_provider())
        real = terminal_service.get_terminal_metadata
        reads = []

        def _stale_first(terminal_id):
            reads.append(terminal_id)
            return dict(stale) if len(reads) in (1, 3) else real(terminal_id)

        with (
            _a_typing_provider(),
            patch.object(terminal_service, "get_terminal_metadata", _stale_first),
        ):
            terminal_service.dispatch_input(tid, "SEND-CANARY", frozen_memory="")
            terminal_service.send_special_key(tid, "Enter")

        assert "SEND-CANARY" in _typed(pane.backend)
        pane.backend.send_special_key.assert_called_once_with(
            "cao-session", "developer-abcd", "Enter"
        )

    def test_a_row_still_unverified_when_read_again_stays_refused(self, pane):
        """The re-read only accepts a recorded check: init ended without one."""
        tid = _row(identity_verified=False)
        stale = terminal_service.get_terminal_metadata(tid)

        refusal = terminal_service.terminal_input_refusal(tid, stale)

        assert isinstance(refusal, ProviderExitedInputBlockedError)
        assert "(identity_unverified)" in str(refusal)

    def test_a_re_read_that_fails_keeps_the_refusal(self, pane):
        tid = _row(identity_verified=False)
        stale = terminal_service.get_terminal_metadata(tid)

        with patch.object(
            terminal_service, "get_terminal_metadata", side_effect=RuntimeError("database is down")
        ):
            refusal = terminal_service.terminal_input_refusal(tid, stale)

        assert isinstance(refusal, ProviderExitedInputBlockedError)

    @pytest.mark.asyncio
    async def test_a_reader_racing_the_real_record_is_never_refused_for_good(self, pane):
        """A thread repeats dispatch_input's order (read the row, then the gate)
        while the event loop runs the real record, once per trial."""
        trials, permanent, unfinished = 100, [], []
        for n in range(trials):
            tid = _row(f"{0xA0000000 + n:08x}", identity_verified=False)
            terminal_service._mark_identity_check_in_flight(tid)
            stop = threading.Event()

            def _reader(tid=tid, stop=stop):
                while not stop.is_set():
                    row = terminal_service.get_terminal_metadata(tid)
                    refusal = terminal_service.terminal_input_refusal(tid, row)
                    if refusal is None:
                        return
                    if isinstance(refusal, ProviderExitedInputBlockedError):
                        permanent.append(tid)
                        return

            reader = threading.Thread(target=_reader, daemon=True)
            reader.start()
            try:
                await terminal_service._record_kiro_identity_verified(
                    tid, self._verified_provider()
                )
                await asyncio.to_thread(reader.join, 5)
            finally:
                stop.set()
                terminal_service._clear_identity_check_in_flight(tid)
            if reader.is_alive():
                unfinished.append(tid)

        assert (len(permanent), unfinished) == (0, [])


class TestLegacyAndOtherProvidersRows:
    def test_a_legacy_kiro_row_without_a_failure_is_grandfathered(self, pane):
        """Provisional (#1074): a Kiro pane launched before the column keeps taking
        input across the upgrade."""
        tid = _row(identity_verified=None)

        _types(tid, pane.backend)
        with _a_typing_provider():
            assert terminal_service.send_special_key(tid, "Enter") is True

    def test_a_legacy_kiro_row_with_a_failure_is_refused(self, pane):
        tid = _row(identity_verified=None)
        assert update_terminal_deferred_init_failure(
            tid,
            {
                "phase": "deferred_init",
                "kind": "interrupted_init",
                "exception_type": "ServerRestart",
            },
        )

        _refused(tid)

    def test_a_legacy_kiro_row_with_only_a_sidecar_failure_is_refused(self, pane):
        """The durable record is the DB column or, after a DB outage, the sidecar,
        and one written before untrusted_runtime existed lacks that field."""
        tid = _row(identity_verified=None)
        assert terminal_service._write_deferred_failure_fallback(
            tid, {"phase": "deferred_init", "kind": "provider_init_error"}
        )

        assert "provider_init_error" in str(_refused(tid))
        pane.backend.send_keys.assert_not_called()

    @pytest.mark.parametrize(
        "kind,exception_type",
        [("interrupted_init", "ServerRestart"), ("provider_init_error", "TimeoutError")],
    )
    def test_a_non_kiro_failure_accepts_input_again(self, pane, kind, exception_type):
        tid = _row(identity_verified=None, provider="claude_code")
        assert terminal_service._persist_deferred_init_failure(
            tid,
            kind=kind,
            message="init ended",
            exception_type=exception_type,
            untrusted_runtime=False,
        )

        _types(tid, pane.backend, "RESEND-CANARY")

    @pytest.mark.asyncio
    async def test_a_non_kiro_init_failure_keeps_its_runtime_for_the_owner(self, pane):
        """Only an untrusted Kiro runtime is reclaimed: other providers' failures
        keep the runtime for inspection, as before, and accept input."""
        tid = _row(identity_verified=None, provider="claude_code")

        await _failed_deferred_launch(tid, TimeoutError("did not start"), provider="claude_code")

        pane.backend.cleanup_terminal_exact.assert_not_called()
        pane.backend.kill_window.assert_not_called()
        metadata = terminal_service.get_terminal_metadata(tid)
        assert metadata["deferred_init_failure"]["exception_type"] == "TimeoutError"
        assert metadata["deferred_init_failure"]["untrusted_runtime"] is False
        assert metadata["deferred_init_runtime_reclaimed"] is False
        _types(tid, pane.backend)


class TestThePayloadIsNotTheGate:
    def test_an_unverified_row_is_refused_whatever_its_payload_says(self, pane):
        tid = _row(identity_verified=False)
        assert update_terminal_deferred_init_failure(
            tid, {"phase": "deferred_init", "kind": "interrupted_init", "untrusted_runtime": False}
        )

        _refused(tid)

    def test_a_verified_row_accepts_input_whatever_its_payload_says(self, pane):
        tid = _row(identity_verified=True)
        assert update_terminal_deferred_init_failure(
            tid, {"phase": "deferred_init", "kind": "task_not_started", "untrusted_runtime": True}
        )

        _types(tid, pane.backend)


class TestTheRecordAndItsFailures:
    @pytest.mark.asyncio
    async def test_a_deferred_record_that_cannot_be_written_fails_untrusted(self, pane):
        from sqlalchemy.exc import OperationalError

        tid = _row(identity_verified=False)
        pane.backend.get_history.return_value = _HEALTHY_SCREEN
        provider = KiroCliProvider(tid, "cao-session", "developer-abcd", "developer")
        with patch.object(
            terminal_service,
            "mark_terminal_identity_verified",
            side_effect=OperationalError("UPDATE", {}, Exception("database is locked")),
        ) as mark:
            task = terminal_service._schedule_deferred_init(
                provider, tid, "TASK-CANARY", None, None, delete_on_failure=False
            )
            await task

        assert mark.call_count == terminal_service._DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS
        failure = _failure(tid)
        assert failure["exception_type"] == "KiroIdentityNotRecordedError"
        assert failure["untrusted_runtime"] is True
        pane.backend.cleanup_terminal_exact.assert_called_once()
        assert _typed(pane.backend) == [_LAUNCH]
        _refused(tid)

    @pytest.mark.asyncio
    async def test_a_synchronous_record_that_cannot_be_written_fails_the_create(
        self, kiro_create, pane
    ):
        from sqlalchemy.exc import OperationalError

        pane.backend.get_history.return_value = _HEALTHY_SCREEN
        with (
            patch.object(
                terminal_service,
                "mark_terminal_identity_verified",
                side_effect=OperationalError("UPDATE", {}, Exception("database is locked")),
            ),
            pytest.raises(terminal_service.KiroIdentityNotRecordedError),
        ):
            await kiro_create.create()

        assert terminal_service.get_terminal_metadata(_TID) is None
        pane.backend.kill_session.assert_called_once()
        assert _typed(pane.backend) == [_LAUNCH]

    @pytest.mark.asyncio
    async def test_losing_every_failure_record_still_refuses_input(self, external_owner_row):
        """The DB write, the sidecar and the reclaim all fail; the row's own False,
        written with it, still refuses input once they work again."""
        from sqlalchemy.exc import OperationalError

        row = external_owner_row
        with (
            patch.object(
                terminal_service,
                "update_terminal_deferred_init_failure",
                side_effect=OperationalError("UPDATE", {}, Exception("database is locked")),
            ),
            patch.object(
                terminal_service, "_write_deferred_sidecar", side_effect=OSError("disk full")
            ),
        ):
            row.backend.cleanup_terminal_exact.side_effect = RuntimeError("tmux is gone")
            await _failed_deferred_launch(row.id, TimeoutError("kiro-cli did not start"))
        row.backend.cleanup_terminal_exact.side_effect = None

        assert terminal_service.get_deferred_init_failure(row.id) is None
        assert (
            terminal_service.get_terminal_metadata(row.id)["deferred_init_runtime_reclaimed"]
            is False
        )
        _refused(row.id)
        assert _typed(row.backend) == []

    @pytest.mark.asyncio
    async def test_the_evidence_comes_from_the_provider(self, external_owner_row):
        """initialize() returning is not evidence: a Kiro provider that did not
        report a passed check leaves the row False."""
        row = external_owner_row
        double = MagicMock()
        double.initialize = AsyncMock(return_value=True)
        double.identity_verified = False
        dispatch = Mock()

        with patch.object(terminal_service, "dispatch_input", dispatch):
            task = terminal_service._schedule_deferred_init(
                double,
                row.id,
                "TASK-CANARY",
                None,
                None,
                delete_on_failure=False,
                provider="kiro_cli",
            )
            await task

        dispatch.assert_not_called()
        assert terminal_service.get_terminal_metadata(row.id)["identity_verified"] is False
        assert _failure(row.id)["untrusted_runtime"] is True
        row.backend.cleanup_terminal_exact.assert_called_once()
        _refused(row.id)

    @pytest.mark.asyncio
    async def test_a_synchronous_create_without_the_evidence_fails(self, kiro_create, pane):
        double = MagicMock()
        double.initialize = AsyncMock(return_value=True)
        double.identity_verified = False
        double.shell_baseline = None
        with (
            patch.object(terminal_service.provider_manager, "create_provider", return_value=double),
            pytest.raises(terminal_service.KiroIdentityNotRecordedError),
        ):
            await kiro_create.create()

        assert terminal_service.get_terminal_metadata(_TID) is None
        pane.backend.kill_session.assert_called_once()


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
        assert metadata["deferred_init_failure"]["untrusted_runtime"] is True
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
        assert _inbox_status(row.id) == MessageStatus.FAILED
        assert _typed(row.backend) == [_LAUNCH]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "outcome",
        [TerminalCleanupOutcome.DELETED, TerminalCleanupOutcome.STILL_PRESENT],
    )
    async def test_a_refusal_refuses_input_and_keys_whether_or_not_the_close_was_confirmed(
        self, external_owner_row, outcome
    ):
        row = external_owner_row
        row.backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(outcome, "result")
        await _refused_deferred_launch(row)

        metadata = terminal_service.get_terminal_metadata(row.id)
        assert metadata["deferred_init_runtime_reclaimed"] is (
            outcome == TerminalCleanupOutcome.DELETED
        )
        _refused(row.id)
        row.backend.send_special_key.assert_not_called()


class TestVerifiedTerminalsTakeInputAndKeys:
    def test_a_verified_terminal_without_a_failure_is_typed_into(self, pane):
        """The gate is the identity record, not the external-owner bit."""
        tid = _row(identity_verified=True)

        _types(tid, pane.backend)

    def test_a_verified_terminal_takes_the_key(self, pane):
        tid = _row(identity_verified=True)

        assert terminal_service.send_special_key(tid, "Enter") is True

        pane.backend.send_special_key.assert_called_once_with(
            "cao-session", "developer-abcd", "Enter"
        )
