"""Where a scheduled flow's AGENT runs (#745).

Relocating the pre-script moves the flow's first user code out of the server
container; the session the flow then launches is the rest of it. In the cluster
topology the server has no tmux at all, so a flow that fires there must place its
agent in an execution runtime — and must tear down the previous run's session in
that runtime, not ask this host's tmux about a session it cannot see.

The unhappy cases carry the weight again: a placement that cannot be honoured
fails the run rather than quietly launching the agent in the container this
boundary exists to keep user code out of.
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.flow_service import execute_flow

RUNTIME = "cao-worker-3"
SESSION = "cao-flow-nightly-report"


@pytest.fixture
def flow_file(tmp_path):
    path = tmp_path / "nightly-report.md"
    path.write_text("""---
name: nightly-report
schedule: "0 2 * * *"
agent_profile: developer
---

Summarise yesterday's build failures.
""")
    return path


def _flow(path, *, engine=None, owner="cao:local#local"):
    return Flow(
        name="nightly-report",
        file_path=str(path),
        schedule="0 2 * * *",
        agent_profile="developer",
        provider="kiro_cli",
        engine=engine,
        script="",
        enabled=True,
        next_run=datetime.now(),
        owner=owner,
    )


def _patches(fn):
    """Applied innermost-first, so this order is the mock-argument order."""
    for decorator in (
        patch("cli_agent_orchestrator.services.flow_service.db_get_flow"),
        patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times"),
        patch("cli_agent_orchestrator.services.flow_service.get_backend"),
        patch("cli_agent_orchestrator.services.flow_service.list_terminals_by_session"),
        patch("cli_agent_orchestrator.services.flow_service.create_terminal"),
        patch("cli_agent_orchestrator.services.flow_service.send_input"),
    ):
        fn = decorator(fn)
    return fn


def _no_previous_session(mock_backend, mock_list):
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []


def _remote_launch(terminal_id="t-remote"):
    """Stand in for the shared launch path, which the HTTP route also uses."""
    terminal = MagicMock(id=terminal_id, session_name=SESSION)
    return AsyncMock(return_value=terminal)


# --- placement --------------------------------------------------------------


@pytest.mark.asyncio
@_patches
async def test_without_a_flow_runtime_the_agent_launches_here(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    monkeypatch.delenv("CAO_FLOW_RUNTIME", raising=False)
    mock_db_get.return_value = _flow(flow_file)
    _no_previous_session(mock_backend, mock_list)

    assert await execute_flow("nightly-report") is True
    kwargs = mock_create_terminal.call_args.kwargs
    assert kwargs["session_name"] == SESSION
    assert kwargs["new_session"] is True
    assert kwargs["owner"] == "cao:local#local"


@pytest.mark.asyncio
@_patches
async def test_a_flow_runtime_places_the_agent_there_and_not_here(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    _no_previous_session(mock_backend, mock_list)
    launch = _remote_launch()
    monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", launch)

    assert await execute_flow("nightly-report") is True

    mock_create_terminal.assert_not_called()
    runtime_id, body = launch.call_args.args
    assert runtime_id == RUNTIME
    assert body.session_name == SESSION
    assert (body.provider, body.agent_profile) == ("kiro_cli", "developer")
    # Server-written owner, exactly as the HTTP route writes the caller's
    # principal — and still absent from the payload the runtime receives.
    assert launch.call_args.kwargs["owner_id"] == "cao:local#local"
    assert "owner" not in body.model_dump(exclude_none=True)


@pytest.mark.asyncio
@_patches
async def test_the_prompt_reaches_the_remote_terminal(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    """`send_input` is already remote-aware, so the launch is the only new hop."""
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    _no_previous_session(mock_backend, mock_list)
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal",
        _remote_launch("t-remote-1"),
    )

    assert await execute_flow("nightly-report") is True
    terminal_id, prompt = mock_send_input.call_args.args[:2]
    assert terminal_id == "t-remote-1"
    assert "build failures" in prompt


@pytest.mark.asyncio
@_patches
async def test_a_disconnected_flow_runtime_does_not_fall_back_to_this_host(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    """Degrading to local would start the agent in the server container.

    This goes through the real shared launch path, whose "not connected" 404 is
    the thing being translated — not a stubbed exception.
    """
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    _no_previous_session(mock_backend, mock_list)
    registry = MagicMock()
    registry.get_runtime.return_value = None
    monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.runtime_registry", registry)

    with pytest.raises(ValueError, match="not connected"):
        await execute_flow("nightly-report")
    mock_create_terminal.assert_not_called()
    mock_send_input.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_a_non_default_engine_flow_forwards_the_engine_remotely(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    """LAUNCH now carries the engine, so the earlier blanket refusal was stale.

    CreateRemoteTerminalBody.engine is forwarded in the payload, honored by the
    bridge, and persisted centrally by launch_remote_terminal, so a kas flow can
    run remotely with the engine it asked for rather than being refused
    (guojing1217 on #802).
    """
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    flow_file.write_text(
        flow_file.read_text().replace(
            "agent_profile: developer", "agent_profile: developer\nengine: kas"
        )
    )
    mock_db_get.return_value = _flow(flow_file, engine="kas")
    _no_previous_session(mock_backend, mock_list)
    launch = _remote_launch()
    monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", launch)

    assert await execute_flow("nightly-report") is True
    launch.assert_awaited_once()
    body = launch.call_args.args[1]
    assert body.engine == "kas"


# --- recycling a session that lives in another pod ---------------------------


def _previous_remote_run(monkeypatch, *, status=TerminalStatus.IDLE, ids=("t-old",)):
    registry = MagicMock()
    registry.is_remote.side_effect = lambda tid: tid in ids
    registry.get_status.return_value = status
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry
    )
    return registry


@pytest.mark.asyncio
@_patches
async def test_the_previous_remote_session_is_torn_down_in_its_runtime(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    # First read sees last night's remote rows; the teardown removes them.
    mock_list.side_effect = [[{"id": "t-old"}], []]
    mock_backend.return_value.session_exists.return_value = False
    _previous_remote_run(monkeypatch)
    teardown = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal", teardown
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", _remote_launch()
    )

    assert await execute_flow("nightly-report") is True
    teardown.assert_awaited_once_with("t-old")
    # Nothing was asked of this host's tmux about a session in another pod.
    mock_backend.return_value.kill_session.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_a_busy_remote_conductor_still_blocks_recycling(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    """The status the registry holds for a remote terminal is the busy signal.

    `status_monitor` only knows about panes on this host, so consulting it would
    read every remote conductor as idle and kill a session mid-run.
    """
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    mock_list.return_value = [{"id": "t-old"}]
    _previous_remote_run(monkeypatch, status=TerminalStatus.PROCESSING)
    teardown = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal", teardown
    )
    launch = _remote_launch()
    monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", launch)

    assert await execute_flow("nightly-report") is False
    teardown.assert_not_called()
    launch.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_a_teardown_that_did_not_confirm_defers_the_run(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    """An unconfirmed teardown may have left last night's agent running."""
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    mock_list.return_value = [{"id": "t-old"}]
    _previous_remote_run(monkeypatch)
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal",
        AsyncMock(side_effect=HTTPException(status_code=504, detail="outcome unknown")),
    )
    launch = _remote_launch()
    monkeypatch.setattr("cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", launch)

    assert await execute_flow("nightly-report") is False
    launch.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_local_leftovers_are_still_cleaned_when_placement_changed(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    monkeypatch,
):
    """An operator who sets CAO_FLOW_RUNTIME leaves a local session behind.

    The remote arm handles the rows it owns and then hands the rest to the local
    arm, so yesterday's tmux session is not orphaned on the server host.
    """
    monkeypatch.setenv("CAO_FLOW_RUNTIME", RUNTIME)
    mock_db_get.return_value = _flow(flow_file)
    mock_list.side_effect = [
        [{"id": "t-old-remote"}, {"id": "t-old-local"}],
        [{"id": "t-old-local"}],
    ]
    mock_backend.return_value.session_exists.return_value = True
    _previous_remote_run(monkeypatch, ids=("t-old-remote",))
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.remote_delete_terminal",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.api.launch_remote_terminal", _remote_launch()
    )

    with (
        patch("cli_agent_orchestrator.services.flow_service.delete_terminals_by_session"),
        patch("cli_agent_orchestrator.services.flow_service.provider_manager") as mock_providers,
    ):
        mock_providers.cleanup_provider.return_value = True
        assert await execute_flow("nightly-report") is True

    mock_backend.return_value.kill_session.assert_called_once_with(SESSION)
