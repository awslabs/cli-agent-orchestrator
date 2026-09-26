"""Where a worker lands when the agent that asked for it runs elsewhere (#745).

``POST /sessions/{name}/terminals`` is the one endpoint in-session assign and
handoff call. Before this, it always added a tmux window in the container
serving the request -- correct when every agent shared that container, and a
404 ("Session '<name>' not found") once the supervisor's session lives in a
runtime pod instead. The session is not missing; it is somewhere else, and so is
where the worker belongs.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from cli_agent_orchestrator.api.main import create_terminal_in_session
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.security.principal import LOCAL_PRINCIPAL

CALLER = "sup12345"
RUNTIME = "cao-supervisor-0"
SESSION = "cao-analysis"


def _remote_terminal() -> Terminal:
    return Terminal(
        id="wrk09876",
        name="data_analyst-1",
        provider="claude_code",
        session_name=SESSION,
        agent_profile="data_analyst",
        caller_id=CALLER,
        status=TerminalStatus.IDLE,
    )


async def _call(**overrides):
    kwargs = dict(
        request=MagicMock(),
        session_name=SESSION,
        agent_profile="data_analyst",
        provider="claude_code",
        caller_id=CALLER,
        defer_init=True,
        principal=LOCAL_PRINCIPAL,
    )
    kwargs.update(overrides)
    return await create_terminal_in_session(**kwargs)


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", new_callable=AsyncMock)
@patch("cli_agent_orchestrator.api.main.terminal_service")
@patch("cli_agent_orchestrator.api.main.runtime_registry")
async def test_worker_is_placed_in_the_callers_runtime(mock_registry, mock_service, mock_launch):
    mock_registry.is_remote.return_value = True
    mock_registry.runtime_for_terminal.return_value = RUNTIME
    mock_registry.placement.return_value = (True, RUNTIME)
    # placement() answers both in one read; the pair must agree with the two above.
    mock_registry.placement.return_value = (True, RUNTIME)
    mock_launch.return_value = _remote_terminal()

    terminal = await _call()

    assert terminal.id == "wrk09876"
    mock_launch.assert_awaited_once()
    runtime_id, body = mock_launch.await_args.args
    assert runtime_id == RUNTIME
    # Joining the caller's session, not starting a parallel one beside it: the
    # pair have to be siblings for send_message/inbox routing to mean anything.
    assert body.new_session is False
    assert body.session_name == SESSION
    assert body.caller_id == CALLER
    # The local path is not also taken -- one worker, not two.
    mock_service.create_terminal.assert_not_called()


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", new_callable=AsyncMock)
@patch("cli_agent_orchestrator.api.main.terminal_service")
@patch("cli_agent_orchestrator.api.main.runtime_registry")
async def test_assigns_deferred_init_contract_survives_the_hop(
    mock_registry, mock_service, mock_launch
):
    """assign returns before provider startup finishes, or it blows the calling
    agent's per-tool timeout. The runtime has to be told to do the same."""
    mock_registry.is_remote.return_value = True
    mock_registry.runtime_for_terminal.return_value = RUNTIME
    mock_registry.placement.return_value = (True, RUNTIME)
    mock_launch.return_value = _remote_terminal()

    body = MagicMock()
    body.initial_message = "analyze sales_q1.csv"
    body.initial_message_orchestration_type = "assign"

    await _call(body=body)

    sent = mock_launch.await_args.args[1]
    assert sent.defer_init is True
    assert sent.initial_message == "analyze sales_q1.csv"
    assert sent.initial_message_orchestration_type == "assign"


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", new_callable=AsyncMock)
@patch("cli_agent_orchestrator.api.main.terminal_service")
@patch("cli_agent_orchestrator.api.main.runtime_registry")
async def test_a_local_caller_still_gets_a_local_window(mock_registry, mock_service, mock_launch):
    """The forwarding is keyed on the caller's recorded runtime, so a
    single-host install -- where nothing is bound to a runtime -- is untouched."""
    mock_registry.is_remote.return_value = False
    mock_registry.runtime_for_terminal.return_value = None
    mock_registry.placement.return_value = (False, None)
    mock_service.create_terminal = AsyncMock(return_value=_remote_terminal())

    await _call()

    mock_launch.assert_not_awaited()
    mock_service.create_terminal.assert_awaited_once()
    assert mock_service.create_terminal.await_args.kwargs["caller_id"] == CALLER


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", new_callable=AsyncMock)
@patch("cli_agent_orchestrator.api.main.terminal_service")
@patch("cli_agent_orchestrator.api.main.runtime_registry")
async def test_idempotency_key_is_refused_rather_than_quietly_dropped(
    mock_registry, mock_service, mock_launch
):
    """Accepting the key here would promise a retry returns the first worker.
    The remote launch has no key table behind it, so a second worker is exactly
    what a retry would get -- say no instead."""
    mock_registry.is_remote.return_value = True
    mock_registry.runtime_for_terminal.return_value = RUNTIME
    mock_registry.placement.return_value = (True, RUNTIME)

    with pytest.raises(HTTPException) as exc:
        await _call(idempotency_key="retry-1")

    assert exc.value.status_code == 400
    assert "idempotency_key" in exc.value.detail
    mock_launch.assert_not_awaited()
    mock_service.create_terminal.assert_not_called()


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", new_callable=AsyncMock)
@patch("cli_agent_orchestrator.api.main.terminal_service")
@patch("cli_agent_orchestrator.api.main.runtime_registry")
async def test_a_remote_caller_whose_placement_is_unreadable_is_not_launched_locally(
    mock_registry, mock_service, mock_launch
):
    """runtime_for_terminal returns None both for a local caller and for a remote
    one whose placement lookup transiently failed. is_remote fails closed to True
    on the latter, so the endpoint must refuse with 503 rather than create the
    worker in the central container (Copilot follow-up on #802)."""
    mock_registry.is_remote.return_value = True  # fail-closed remote/unknown
    mock_registry.runtime_for_terminal.return_value = None  # but placement unreadable
    mock_registry.placement.return_value = (True, None)

    with pytest.raises(HTTPException) as exc:
        await _call()

    assert exc.value.status_code == 503
    mock_launch.assert_not_awaited()
    mock_service.create_terminal.assert_not_called()
