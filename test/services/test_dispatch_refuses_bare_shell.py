"""The delivery path must never type a task into a pane that is a bare shell (#1018).

``dispatch_input`` already refused when the cached status was ERROR. The
reporter's log showed why that is not enough: readiness was declared at
:57.655, the monitor flipped the terminal to ERROR at :57.831, and the task was
typed at :58.570 anyway -- the guard read a status that was one memory-service
call and one pane capture old, and it never looked at the pane itself. bash
then executed every line of the task text, including its backtick and ``$(...)``
substitutions.

The guard now re-reads the status immediately before the keystroke AND compares
the pane's foreground process with the shell captured before the CLI launched
(``shell_baseline``, persisted as the terminal's ``shell_command``). That second
check is the structural one: it holds for every provider that records a
baseline, whatever its own ``get_status`` says, and it does not depend on the
provider having finished initializing.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import (
    ProviderExitedInputBlockedError,
    TerminalInputBlockedError,
    TerminalStatus,
)
from cli_agent_orchestrator.providers.manager import ProviderManager
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.terminal_service import dispatch_input, send_input

METADATA = {
    "tmux_session": "cao-session",
    "tmux_window": "developer-abcd",
    "provider": "codex",
}

# The task text from the report: innocuous here, but bash ran it.
TASK = (
    "Code is in clone /home/u/markdown.dev/3, branch `fix-1244`. Review it and "
    "extract it e.g. `git -C <clone> archive fix-1244 | tar -x -C /tmp/out`"
)

PATCHES = (
    patch("cli_agent_orchestrator.services.terminal_service.MemoryService"),
    patch("cli_agent_orchestrator.services.terminal_service.status_monitor"),
    patch("cli_agent_orchestrator.services.terminal_service.update_last_active"),
    patch("cli_agent_orchestrator.services.terminal_service.provider_manager"),
    patch("cli_agent_orchestrator.backends.registry._backend"),
    patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata"),
)


@pytest.fixture
def harness():
    """Patch the collaborators ``dispatch_input`` touches and hand back the mocks."""
    started = [p.start() for p in PATCHES]
    memory_service, status_monitor, update_last_active, provider_manager, backend, metadata = (
        started
    )
    memory_service.return_value.get_curated_memory_context.return_value = ""
    metadata.return_value = dict(METADATA)
    # Real providers' dispatch bookkeeping reads the pane; give them text, not a mock.
    backend.get_history.return_value = ""
    status_monitor.get_status.return_value = TerminalStatus.IDLE
    provider = provider_manager.get_provider.return_value
    provider.paste_enter_count = 1
    provider.paste_submit_delay = 0.3
    provider.shell_baseline = "bash"
    provider.blocks_orchestrated_input_while_waiting_user_answer = False
    try:
        yield MagicMock(
            backend=backend,
            provider=provider,
            provider_manager=provider_manager,
            status_monitor=status_monitor,
            update_last_active=update_last_active,
            metadata=metadata,
        )
    finally:
        for p in reversed(PATCHES):
            p.stop()


class TestDispatchRefusesBareShell:
    def test_refuses_when_pane_is_back_at_the_shell(self, harness):
        """Cached status says IDLE; the pane's foreground process is the pre-launch shell."""
        harness.backend.get_pane_current_command.return_value = "bash"

        with pytest.raises(TerminalInputBlockedError, match="shell"):
            dispatch_input("1e293d67", TASK)

        harness.backend.send_keys.assert_not_called()
        harness.provider.mark_input_received.assert_not_called()
        harness.provider.record_dispatched_message.assert_not_called()
        harness.update_last_active.assert_not_called()

    def test_refusals_are_the_provider_exited_subclass(self, harness):
        """Both guards raise the subclass, so handlers that catch the base class are unchanged
        and the deferred-init path can tell a dead worker from a parked one."""
        harness.backend.get_pane_current_command.return_value = "bash"
        with pytest.raises(ProviderExitedInputBlockedError):
            dispatch_input("1e293d67", TASK)

        harness.backend.get_pane_current_command.return_value = "codex"
        harness.status_monitor.get_status.return_value = TerminalStatus.ERROR
        with pytest.raises(ProviderExitedInputBlockedError):
            dispatch_input("1e293d67", TASK)

        assert issubclass(ProviderExitedInputBlockedError, TerminalInputBlockedError)

    def test_refuses_when_status_flips_to_error_before_typing(self, harness):
        """The reporter's 150 ms: IDLE at the first read, ERROR by the time we would type."""
        harness.backend.get_pane_current_command.return_value = "codex"
        harness.status_monitor.get_status.side_effect = [
            TerminalStatus.IDLE,
            TerminalStatus.ERROR,
        ]

        with pytest.raises(TerminalInputBlockedError, match="ERROR state"):
            dispatch_input("1e293d67", TASK)

        harness.backend.send_keys.assert_not_called()

    def test_refusal_does_not_depend_on_initialized(self, harness):
        """A provider still mid-startup gets the same refusal as an initialized one."""
        harness.provider._initialized = False
        harness.backend.get_pane_current_command.return_value = "bash"

        with pytest.raises(TerminalInputBlockedError):
            send_input("1e293d67", TASK)

        harness.backend.send_keys.assert_not_called()

    def test_uses_persisted_shell_command_when_provider_lost_its_baseline(self, harness):
        """After a cao-server restart the baseline lives in the terminal's metadata."""
        harness.provider.shell_baseline = None
        harness.metadata.return_value = dict(METADATA, shell_command="zsh")
        harness.backend.get_pane_current_command.return_value = "zsh"

        with pytest.raises(TerminalInputBlockedError, match="zsh"):
            dispatch_input("1e293d67", TASK)

        harness.backend.send_keys.assert_not_called()

    def test_delivers_when_the_cli_is_in_the_foreground(self, harness):
        """The check is a veto, not a new requirement: a live codex is still delivered to."""
        harness.backend.get_pane_current_command.return_value = "codex"

        assert send_input("1e293d67", TASK) is True

        harness.backend.send_keys.assert_called_once()
        assert harness.backend.send_keys.call_args.args[2].endswith(TASK)

    def test_no_baseline_anywhere_falls_back_to_the_status_guard(self, harness):
        """A provider that never recorded a baseline keeps today's behaviour.

        Nothing to compare against means no pane check; the cached-status ERROR
        guard still applies. Documented as the remaining gap for providers that
        do not record ``shell_baseline``.
        """
        harness.provider.shell_baseline = None
        harness.backend.get_pane_current_command.return_value = "bash"

        assert send_input("1e293d67", TASK) is True

        harness.backend.send_keys.assert_called_once()

    def test_pane_lookup_failure_does_not_block_delivery(self, harness):
        """A failed foreground lookup is not evidence of an exit; the status guard remains."""
        harness.backend.get_pane_current_command.side_effect = RuntimeError("tmux gone")

        assert send_input("1e293d67", TASK) is True

        harness.backend.send_keys.assert_called_once()


def _every_provider_class():
    return [
        pytest.param(ProviderManager.provider_class(provider_type.value), id=provider_type.value)
        for provider_type in ProviderType
    ]


def _construct(provider_cls):
    """Build a real provider instance with the minimum each constructor accepts."""
    try:
        return provider_cls("1e293d67", "cao-session", "developer-abcd")
    except TypeError:
        return provider_cls("1e293d67", "cao-session", "developer-abcd", "developer")


class TestEveryProviderIsRefusedIntoABareShell:
    """Structural guard: the refusal is provider-agnostic.

    Each real provider class, whatever its own ``get_status`` would say about the
    pane and whether or not it has finished initializing, is refused delivery
    when its pane's foreground process is the shell it recorded as the baseline.
    Adding a provider cannot opt out of this by accident: the check lives in the
    shared delivery path and keys only on ``shell_baseline``.
    """

    @pytest.mark.parametrize("provider_cls", _every_provider_class())
    def test_refused_before_initialization_completes(self, harness, provider_cls):
        provider = _construct(provider_cls)
        provider.shell_baseline = "zsh"
        if hasattr(provider, "_initialized"):
            provider._initialized = False
        harness.provider_manager.get_provider.return_value = provider
        harness.backend.get_pane_current_command.return_value = "zsh"

        with pytest.raises(TerminalInputBlockedError):
            dispatch_input("1e293d67", TASK)

        harness.backend.send_keys.assert_not_called()

    @pytest.mark.parametrize("provider_cls", _every_provider_class())
    def test_delivered_while_the_cli_runs(self, harness, provider_cls):
        provider = _construct(provider_cls)
        provider.shell_baseline = "zsh"
        harness.provider_manager.get_provider.return_value = provider
        harness.backend.get_pane_current_command.return_value = "some-cli"

        with patch.object(type(provider), "record_dispatched_message", lambda *a, **k: None):
            dispatch_input("1e293d67", TASK)

        harness.backend.send_keys.assert_called_once()


class TestDeferredInitTearsDownAnExitedProvider:
    """Review of #1018 (P2-2): assign must not report a bare shell as "waiting on a prompt".

    ``_schedule_deferred_init`` catches ``TerminalInputBlockedError`` to leave a
    worker that is parked on a real prompt alive for ``answer_user_prompt``. The
    delivery guards raise for a different reason -- the provider process is gone
    -- and on that path the same handling kept a dead worker registered, recorded
    the outcome as ``waiting_user_answer`` and told the supervisor to answer a
    prompt that does not exist. The guards now raise
    ``ProviderExitedInputBlockedError`` and the deferred path tears the worker
    down like any other startup failure.
    """

    METADATA = dict(METADATA, provider="kiro_cli", caller_id="supervisor1")

    @pytest.fixture
    def deferred(self):
        provider = MagicMock()
        provider.initialize = AsyncMock(return_value=True)
        provider.shell_baseline = "bash"
        provider.runtime_variant = None
        provider.paste_enter_count = 1
        provider.blocks_orchestrated_input_while_waiting_user_answer = False

        persist = MagicMock()
        notify = MagicMock()
        surface = AsyncMock()
        with (
            patch.object(terminal_service, "MemoryService") as memory_service,
            patch.object(terminal_service, "status_monitor") as status_monitor,
            patch.object(terminal_service, "provider_manager") as provider_manager,
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
            patch.object(
                terminal_service, "get_terminal_metadata", return_value=dict(self.METADATA)
            ),
            patch.object(terminal_service, "update_terminal_shell_command"),
            patch.object(terminal_service, "update_last_active"),
            patch.object(terminal_service, "_clear_deferred_init_external_owner", AsyncMock()),
            patch.object(terminal_service, "_persist_initial_delivery", persist),
            patch.object(terminal_service, "_notify_caller_of_deferred_failure", notify),
            patch.object(terminal_service, "_surface_deferred_init_failure", surface),
        ):
            memory_service.return_value.get_curated_memory_context.return_value = ""
            status_monitor.get_status.return_value = TerminalStatus.IDLE
            provider_manager.get_provider.return_value = provider
            backend.supports_event_inbox.return_value = False
            backend.get_history.return_value = ""
            yield MagicMock(
                provider=provider,
                backend=backend,
                status_monitor=status_monitor,
                persist=persist,
                notify=notify,
                surface=surface,
            )

    @pytest.mark.asyncio
    async def test_pane_back_at_shell_after_init_is_torn_down(self, deferred):
        """initialize() succeeded; by delivery time the pane's foreground process is bash again."""
        deferred.backend.get_pane_current_command.return_value = "bash"

        task = terminal_service._schedule_deferred_init(
            deferred.provider, "1e293d67", TASK, None, None
        )
        await task

        deferred.backend.send_keys.assert_not_called()
        deferred.surface.assert_awaited_once()
        kwargs = deferred.surface.await_args.kwargs
        assert kwargs["kind"] == "provider_exited"
        assert kwargs["exception_type"] == "ProviderExitedInputBlockedError"
        assert "exited" in kwargs["message"]
        # The "parked on a prompt" branch did not run.
        assert not any(
            call.kwargs.get("kind") == "waiting_user_answer"
            for call in deferred.persist.call_args_list
        )
        assert not any(
            call.kwargs.get("delete_worker") is False for call in deferred.notify.call_args_list
        )

    @pytest.mark.asyncio
    async def test_error_status_at_delivery_is_torn_down(self, deferred):
        """The pre-existing ERROR guard takes the same path: the provider is gone, not asking."""
        deferred.backend.get_pane_current_command.return_value = "kiro-cli"
        deferred.status_monitor.get_status.return_value = TerminalStatus.ERROR

        await terminal_service._schedule_deferred_init(
            deferred.provider, "1e293d67", TASK, None, None
        )

        deferred.backend.send_keys.assert_not_called()
        deferred.surface.assert_awaited_once()
        assert deferred.surface.await_args.kwargs["kind"] == "provider_exited"

    @pytest.mark.asyncio
    async def test_a_real_prompt_is_still_left_alive(self, deferred):
        """The WAITING_USER_ANSWER branch is untouched: a parked worker is kept for the operator."""
        deferred.provider.blocks_orchestrated_input_while_waiting_user_answer = True
        deferred.backend.get_pane_current_command.return_value = "kiro-cli"
        deferred.status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER

        await terminal_service._schedule_deferred_init(
            deferred.provider, "1e293d67", TASK, None, None
        )

        deferred.backend.send_keys.assert_not_called()
        deferred.surface.assert_not_awaited()
        assert any(
            call.kwargs.get("kind") == "waiting_user_answer"
            for call in deferred.persist.call_args_list
        )
        assert deferred.notify.call_args.kwargs.get("delete_worker") is False
