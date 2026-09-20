"""Who a remote terminal works for, and who is never told (#745, criterion 14).

The executor pod is the least trusted party in this topology. So the owner of a
remote launch travels server -> database -> server, and never server -> runtime
-> server: an identity handed to the runtime is an identity the runtime could
re-present, which is exactly the "agent-supplied IDs are not authorization" rule
this issue is built on.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.runtime_channel.api import (
    CreateRemoteTerminalBody,
    create_remote_terminal,
)
from cli_agent_orchestrator.runtime_channel.protocol import (
    CommandOutcome,
    CommandResultFrame,
    CommandType,
)
from cli_agent_orchestrator.security.principal import LOCAL_PRINCIPAL, Principal

TID = "abcd1234"
OWNER = Principal(subject="auth0|member", issuer="https://idp.example/")

LAUNCHED = {
    "id": TID,
    "session_name": "cao-worker-abcd1234",
    "name": "worker-0",
    "provider": "kiro_cli",
    "agent_profile": "developer",
    "allowed_tools": ["fs_read"],
    "shell_command": "kiro chat",
    "status": "idle",
}


def _connected_runtime():
    conn = MagicMock()
    conn.send_command = AsyncMock(
        return_value=CommandResultFrame(
            op_id="op-1",
            terminal_id=TID,
            outcome=CommandOutcome.OK,
            payload={"terminal": LAUNCHED},
        )
    )
    return conn


def _body():
    return CreateRemoteTerminalBody(
        provider="kiro_cli",
        agent_profile="developer",
        working_directory="/workspace",
    )


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.runtime_registry")
@patch("cli_agent_orchestrator.runtime_channel.api.db_create_terminal")
async def test_the_server_records_the_callers_owner_on_the_row(mock_create, mock_registry):
    conn = _connected_runtime()
    mock_registry.get_runtime.return_value = conn

    terminal = await create_remote_terminal("worker-1", _body(), principal=OWNER)

    assert mock_create.call_args.kwargs["owner"] == OWNER.id
    assert terminal.owner == OWNER.id


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.runtime_registry")
@patch("cli_agent_orchestrator.runtime_channel.api.db_create_terminal")
async def test_the_owner_is_not_in_the_launch_payload(mock_create, mock_registry):
    """The load-bearing assertion of this file.

    If the owner ever appears in what is sent over the channel, a compromised or
    merely buggy runtime can echo a different one back, and every later
    authorization decision is reading the executor's opinion of who it works for.
    """
    conn = _connected_runtime()
    mock_registry.get_runtime.return_value = conn

    await create_remote_terminal("worker-1", _body(), principal=OWNER)

    command_type, payload = conn.send_command.call_args.args[:2]
    assert command_type is CommandType.LAUNCH
    assert "owner" not in payload
    assert OWNER.id not in repr(payload)
    assert OWNER.subject not in repr(payload)


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.runtime_registry")
@patch("cli_agent_orchestrator.runtime_channel.api.db_create_terminal")
async def test_the_owner_is_not_written_into_the_agent_writable_metadata(
    mock_create, mock_registry
):
    """``PATCH /terminals/{id}/metadata`` lets the running agent replace that
    dict wholesale, so an owner stored in it would be an owner the agent picks."""
    conn = _connected_runtime()
    mock_registry.get_runtime.return_value = conn

    terminal = await create_remote_terminal("worker-1", _body(), principal=OWNER)

    assert mock_create.call_args.kwargs["metadata"] == {"runtime_id": "worker-1"}
    assert "owner" not in terminal.metadata


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.runtime_channel.api.runtime_registry")
@patch("cli_agent_orchestrator.runtime_channel.api.db_create_terminal")
async def test_auth_disabled_records_the_named_local_owner(mock_create, mock_registry):
    """Not None.

    With auth off there is still an answer to "whose work is this" -- the local
    user -- and recording it keeps the criterion's "deferred work never becomes
    anonymous" true on the default single-user install too.
    """
    conn = _connected_runtime()
    mock_registry.get_runtime.return_value = conn

    await create_remote_terminal("worker-1", _body(), principal=LOCAL_PRINCIPAL)

    assert mock_create.call_args.kwargs["owner"] == LOCAL_PRINCIPAL.id
