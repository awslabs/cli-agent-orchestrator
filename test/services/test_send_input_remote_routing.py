"""send_input is the one place that decides local pane vs remote runtime (#745).

Below the branch, send_input is entirely about a pane on this host: the provider
instance, the memory injection, the status arming, the tmux paste buffer. For a
terminal a runtime executes, none of that is this process's business — and the
paste is worse than useless, because a local session that happens to share the
name would receive another agent's message.

Routing here rather than at each call site is deliberate: POST
/terminals/{id}/input was already remote-aware, but the synchronous senders
(inbox delivery of a delegated result, agent steps, memory recall) were not, and
each of them typed into local tmux.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)
from cli_agent_orchestrator.services import terminal_service

REMOTE = "ef38cd1c"


def _result(outcome=CommandOutcome.OK, **payload):
    return CommandResultFrame(op_id="op-1", outcome=outcome, payload=payload or {"success": True})


@pytest.fixture
def remote():
    """A terminal with a DB row here, executed by a runtime elsewhere."""
    with (
        patch.object(terminal_service, "get_terminal_metadata") as metadata,
        patch.object(terminal_service, "get_backend") as backend,
        patch.object(terminal_service, "provider_manager") as providers,
        patch("cli_agent_orchestrator.runtime_channel.registry.runtime_registry") as registry,
    ):
        metadata.return_value = {
            "id": REMOTE,
            "provider": "claude_code",
            "tmux_session": "cao-2b412d60",
            "tmux_window": "code_supervisor-8cd0",
        }
        registry.is_remote.return_value = True
        registry.send_terminal_command_blocking.return_value = _result()
        yield MagicMock(metadata=metadata, backend=backend, providers=providers, registry=registry)


class TestARemoteTerminalIsNotTypedIntoLocally:
    def test_the_local_tmux_socket_is_never_touched(self, remote):
        assert terminal_service.send_input(REMOTE, "757") is True

        remote.backend.assert_not_called()

    def test_the_send_goes_to_the_runtime_as_an_input_command(self, remote):
        terminal_service.send_input(REMOTE, "757")

        terminal_id, command_type, payload = (
            remote.registry.send_terminal_command_blocking.call_args.args
        )
        assert (terminal_id, command_type) == (REMOTE, CommandType.INPUT)
        assert payload["message"] == "757"

    def test_orchestration_type_crosses_as_its_wire_value(self, remote):
        """The frame is JSON; an enum member would not survive it."""
        terminal_service.send_input(
            REMOTE,
            "757",
            sender_id="15c1e8e0",
            orchestration_type=OrchestrationType.SEND_MESSAGE,
        )

        payload = remote.registry.send_terminal_command_blocking.call_args.args[2]
        assert payload["orchestration_type"] == OrchestrationType.SEND_MESSAGE.value
        assert payload["sender_id"] == "15c1e8e0"

    def test_no_local_provider_is_consulted(self, remote):
        """Provider state for a remote terminal lives in its runtime; asking the
        local manager would answer None and silently skip the input guards."""
        terminal_service.send_input(REMOTE, "757")

        remote.providers.get_provider.assert_not_called()

    def test_a_runtime_that_reports_failure_raises(self, remote):
        remote.registry.send_terminal_command_blocking.return_value = _result(
            outcome=CommandOutcome.FAILED, error="provider exited"
        )

        with pytest.raises(RuntimeError, match="provider exited"):
            terminal_service.send_input(REMOTE, "757")

    def test_a_runtime_that_reports_no_send_returns_false(self, remote):
        remote.registry.send_terminal_command_blocking.return_value = _result(success=False)

        assert terminal_service.send_input(REMOTE, "757") is False

    def test_an_unknown_terminal_still_fails_before_any_routing(self, remote):
        remote.metadata.return_value = None

        with pytest.raises(ValueError, match="not found"):
            terminal_service.send_input(REMOTE, "757")
        remote.registry.send_terminal_command_blocking.assert_not_called()


class TestALocalTerminalIsUnaffected:
    def test_it_still_pastes_into_its_own_pane(self, remote):
        remote.registry.is_remote.return_value = False
        remote.providers.get_provider.return_value = None

        with (
            patch.object(terminal_service, "status_monitor"),
            patch.object(
                terminal_service, "inject_memory_context", side_effect=lambda m, *a, **k: m
            ),
            patch.object(terminal_service, "update_last_active"),
        ):
            assert terminal_service.send_input(REMOTE, "757") is True

        remote.backend.return_value.send_keys.assert_called_once()
        remote.registry.send_terminal_command_blocking.assert_not_called()
