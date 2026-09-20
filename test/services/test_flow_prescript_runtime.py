"""Where a flow's pre-script runs (#745).

The issue names this path explicitly: a scheduled flow can execute a pre-script
through `subprocess.run` before it creates any terminal, so moving terminal
operations alone would leave user code running in the central server container,
beside the database. These tests pin the relocation and — just as important — that
the local path is untouched when no script runtime is configured.

The interesting cases are the unhappy ones. An unusable pre-script must raise: its
contract is a JSON verdict, and a runtime that vanished has not told us
`execute: false`, it has told us nothing.
"""

import subprocess
import tempfile
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)
from cli_agent_orchestrator.runtime_channel.registry import RuntimeUnavailableError
from cli_agent_orchestrator.services import flow_service
from cli_agent_orchestrator.services.flow_service import execute_flow

RUNTIME = "cao-supervisor-0"
GOOD_JSON = '{"execute": true, "output": {"url": "https://svc", "status_code": "503"}}'


@pytest.fixture
def flow_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("""---
name: monitor-service
schedule: "*/5 * * * *"
agent_profile: developer
---

The service at [[url]] is down (status: [[status_code]]).
""")
        f.flush()
        return f.name


@pytest.fixture
def pre_script(tmp_path):
    script = tmp_path / "health-check.sh"
    script.write_text(f"#!/bin/sh\necho '{GOOD_JSON}'\n")
    script.chmod(0o755)
    return script


def _flow(path, script):
    return Flow(
        name="monitor-service",
        file_path=path,
        schedule="*/5 * * * *",
        agent_profile="developer",
        provider="kiro_cli",
        script=str(script),
        enabled=True,
        next_run=datetime.now(),
        owner=None,
    )


def _runtime(payload):
    conn = MagicMock()
    conn.send_command = AsyncMock(
        return_value=CommandResultFrame(
            op_id="op-1",
            terminal_id=None,
            outcome=CommandOutcome.OK,
            payload=payload,
        )
    )
    return conn


def _ok(stdout=GOOD_JSON):
    return {"returncode": 0, "stdout": stdout, "stderr": "", "timed_out": False}


def _remote(monkeypatch, conn):
    """Point flow_service at a connected script runtime."""
    monkeypatch.setattr(flow_service, "remote_script_runtime", lambda: RUNTIME)
    registry = MagicMock()
    registry.get_runtime.return_value = conn
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry
    )
    return registry


def _patches(fn):
    """The launch machinery every dispatch test stubs identically.

    Applied innermost-first, so this order is the order of the mock arguments.
    """
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


# --- the local path stays the local path -----------------------------------


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
@_patches
async def test_no_script_runtime_runs_the_pre_script_here(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    mock_run,
    flow_file,
    pre_script,
    monkeypatch,
):
    monkeypatch.setattr(flow_service, "remote_script_runtime", lambda: None)
    mock_db_get.return_value = _flow(flow_file, pre_script)
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []
    mock_run.return_value = subprocess.CompletedProcess([], 0, GOOD_JSON, "")

    assert await execute_flow("monitor-service") is True
    assert mock_run.call_args.args[0] == [str(pre_script)]
    assert mock_run.call_args.kwargs["timeout"] == flow_service.PRE_SCRIPT_TIMEOUT
    mock_create_terminal.assert_called_once()


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
@_patches
async def test_a_named_runtime_that_is_not_connected_falls_back_to_local(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    mock_run,
    flow_file,
    pre_script,
    monkeypatch,
):
    """`remote_script_runtime` already resolves "named but absent" to None.

    Reading the env directly here instead would make a flow fail whenever the
    runtime is briefly down, where the workflow path degrades to local execution.
    One decision, one place.
    """
    monkeypatch.setenv("CAO_SCRIPT_RUNTIME", "cao-worker-gone")
    registry = MagicMock()
    registry.get_runtime.return_value = None
    monkeypatch.setattr(
        "cli_agent_orchestrator.runtime_channel.registry.runtime_registry", registry
    )
    mock_db_get.return_value = _flow(flow_file, pre_script)
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []
    mock_run.return_value = subprocess.CompletedProcess([], 0, GOOD_JSON, "")

    assert await execute_flow("monitor-service") is True
    mock_run.assert_called_once()


# --- relocation ------------------------------------------------------------


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
@_patches
async def test_the_pre_script_runs_in_the_runtime_not_here(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    mock_run,
    flow_file,
    pre_script,
    monkeypatch,
):
    conn = _runtime(_ok())
    _remote(monkeypatch, conn)
    mock_db_get.return_value = _flow(flow_file, pre_script)
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []

    assert await execute_flow("monitor-service") is True

    mock_run.assert_not_called()
    command_type, payload = conn.send_command.call_args.args[:2]
    assert command_type is CommandType.RUN_SCRIPT
    assert payload["script"] == pre_script.read_text()
    # The shebang decides the interpreter. `python` would hand a documented
    # `#!/bin/bash` health check to the Python parser.
    assert payload["mode"] == "executable"
    assert payload["timeout"] == flow_service.PRE_SCRIPT_TIMEOUT


@pytest.mark.asyncio
@_patches
async def test_the_rendered_prompt_uses_the_remote_scripts_output(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    """The verdict and the template values both come back across the boundary."""
    _remote(monkeypatch, _runtime(_ok()))
    mock_db_get.return_value = _flow(flow_file, pre_script)
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []
    mock_create_terminal.return_value = MagicMock(id="t1")

    assert await execute_flow("monitor-service") is True
    prompt = mock_send_input.call_args.args[1]
    assert "https://svc" in prompt and "503" in prompt


@pytest.mark.asyncio
@_patches
async def test_a_remote_skip_is_still_a_skip(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    _remote(monkeypatch, _runtime(_ok('{"execute": false, "output": {}}')))
    mock_db_get.return_value = _flow(flow_file, pre_script)

    assert await execute_flow("monitor-service") is False
    mock_create_terminal.assert_not_called()
    # A skip still advances the schedule, exactly as the local path does.
    mock_update_times.assert_called_once()


@pytest.mark.asyncio
@_patches
async def test_the_server_never_forwards_its_own_environment(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    """A health check does not need the server's secrets to reach a pod.

    The local path inherits `os.environ`; forwarding that would hand every
    execution runtime the shared channel token and whatever credentials the
    operator set, to run a curl.
    """
    monkeypatch.setenv("CAO_RUNTIME_TOKEN", "s3cret-channel-token")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cret-aws-key")
    conn = _runtime(_ok())
    _remote(monkeypatch, conn)
    mock_db_get.return_value = _flow(flow_file, pre_script)
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []

    await execute_flow("monitor-service")

    env = conn.send_command.call_args.args[1]["env"]
    assert "s3cret-channel-token" not in repr(env)
    assert "s3cret-aws-key" not in repr(env)
    assert set(env) <= {"PATH", "HOME", "CAO_API_BASE_URL", "CAO_FLOW_NAME"}
    assert env["CAO_FLOW_NAME"] == "monitor-service"


@pytest.mark.asyncio
@_patches
async def test_callbacks_resolve_to_the_advertised_url(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    """A pre-script in another pod cannot reach the server on 127.0.0.1."""
    monkeypatch.setenv("CAO_ADVERTISED_URL", "http://cao-server:9889/")
    conn = _runtime(_ok())
    _remote(monkeypatch, conn)
    mock_db_get.return_value = _flow(flow_file, pre_script)
    mock_backend.return_value.session_exists.return_value = False
    mock_list.return_value = []

    await execute_flow("monitor-service")

    assert (
        conn.send_command.call_args.args[1]["env"]["CAO_API_BASE_URL"] == "http://cao-server:9889"
    )


# --- unhappy outcomes stay loud -------------------------------------------


@pytest.mark.asyncio
@_patches
async def test_a_nonzero_remote_exit_fails_the_flow(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    _remote(
        monkeypatch,
        _runtime({"returncode": 3, "stdout": "", "stderr": "curl: (6)", "timed_out": False}),
    )
    mock_db_get.return_value = _flow(flow_file, pre_script)

    with pytest.raises(ValueError, match="exit code 3"):
        await execute_flow("monitor-service")
    mock_create_terminal.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_a_remote_timeout_is_not_a_skip(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    _remote(
        monkeypatch,
        _runtime({"returncode": None, "stdout": "", "stderr": "", "timed_out": True}),
    )
    mock_db_get.return_value = _flow(flow_file, pre_script)

    with pytest.raises(ValueError, match="exceeded"):
        await execute_flow("monitor-service")
    mock_create_terminal.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_a_runtime_that_dies_mid_script_is_an_unknown_outcome(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    conn = MagicMock()
    conn.send_command = AsyncMock(side_effect=RuntimeUnavailableError("gone"))
    _remote(monkeypatch, conn)
    mock_db_get.return_value = _flow(flow_file, pre_script)

    with pytest.raises(ValueError, match="outcome unknown"):
        await execute_flow("monitor-service")
    mock_create_terminal.assert_not_called()


@pytest.mark.asyncio
@_patches
async def test_unparseable_remote_output_still_fails_the_flow(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list,
    mock_create_terminal,
    mock_send_input,
    flow_file,
    pre_script,
    monkeypatch,
):
    """The JSON contract is the server's, and it is enforced where it always was."""
    _remote(monkeypatch, _runtime(_ok("not json at all")))
    mock_db_get.return_value = _flow(flow_file, pre_script)

    with pytest.raises(ValueError, match="not valid JSON"):
        await execute_flow("monitor-service")
    mock_create_terminal.assert_not_called()
