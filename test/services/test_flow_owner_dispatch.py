"""A schedule's owner, and what a revoked one may still do (#745, criterion 14).

A flow is the clearest case the criterion describes: the request that registered
it is long gone when it fires, and the only other candidate for "who is this
for" is whoever the server process runs as. These tests pin that the owner is
recorded at registration, consulted at dispatch, and that revoking it stops new
runs without taking away the ability to disable or remove the flow.
"""

import tempfile
from datetime import datetime
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.security.principal import (
    LOCAL_PRINCIPAL,
    REVOKED_ENV,
    Principal,
    revocation,
)
from cli_agent_orchestrator.services.flow_service import add_flow, execute_flow

OWNER = Principal(subject="auth0|member", issuer="https://idp.example/")


@pytest.fixture(autouse=True)
def _clean_revocations(monkeypatch):
    monkeypatch.delenv(REVOKED_ENV, raising=False)
    revocation.reset()
    yield
    revocation.reset()


@pytest.fixture
def flow_file():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write("""---
name: owned-flow
schedule: "* * * * *"
agent_profile: developer
---

Do the nightly thing.
""")
        f.flush()
        return f.name


def _flow(path, owner):
    return Flow(
        name="owned-flow",
        file_path=path,
        schedule="* * * * *",
        agent_profile="developer",
        provider="kiro_cli",
        script="",
        enabled=True,
        next_run=datetime.now(),
        owner=owner,
    )


# --- registration ---------------------------------------------------------


@patch("cli_agent_orchestrator.services.flow_service.db_create_flow")
def test_registration_records_the_owner(mock_create, flow_file):
    mock_create.return_value = _flow(flow_file, OWNER.id)
    add_flow(flow_file, owner=OWNER.id)
    assert mock_create.call_args.kwargs["owner"] == OWNER.id


@patch("cli_agent_orchestrator.services.flow_service.db_create_flow")
def test_registration_without_an_owner_records_none_not_local(mock_create, flow_file):
    """An omitted owner stays unrecorded.

    Defaulting to the local principal here would make every API-registered flow
    claim the single-user owner, which is the anonymisation inverted rather than
    fixed.
    """
    mock_create.return_value = _flow(flow_file, None)
    add_flow(flow_file)
    assert mock_create.call_args.kwargs["owner"] is None


# --- dispatch gate --------------------------------------------------------


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_revoked_owner_flow_is_not_dispatched(
    mock_db_get, mock_update_times, mock_backend, mock_create_terminal, mock_send_input, flow_file
):
    mock_db_get.return_value = _flow(flow_file, OWNER.id)
    revocation.revoke(OWNER)

    assert await execute_flow("owned-flow") is False
    mock_create_terminal.assert_not_called()
    mock_send_input.assert_not_called()


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.subprocess.run")
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_the_gate_precedes_the_pre_script(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_create_terminal,
    mock_send_input,
    mock_run,
    flow_file,
    tmp_path,
):
    """The poll script is the owner's code too.

    Running it and only then refusing to launch would still execute a removed
    member's program on the server, which is starting work on their behalf.
    """
    script = tmp_path / "poll.sh"
    script.write_text('#!/bin/sh\necho \'{"execute": true, "output": {}}\'\n')
    script.chmod(0o755)
    flow = _flow(flow_file, OWNER.id)
    mock_db_get.return_value = Flow.model_validate({**flow.model_dump(), "script": str(script)})
    revocation.revoke(OWNER)

    assert await execute_flow("owned-flow") is False
    mock_run.assert_not_called()


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_a_held_flow_still_advances_its_schedule(
    mock_db_get, mock_update_times, mock_backend, mock_create_terminal, mock_send_input, flow_file
):
    """Otherwise a revoked owner's minute-cron is re-attempted every minute
    forever, and the refusal becomes a log flood instead of a decision."""
    mock_db_get.return_value = _flow(flow_file, OWNER.id)
    revocation.revoke(OWNER)

    await execute_flow("owned-flow")
    assert mock_update_times.called
    assert mock_update_times.call_args.kwargs["next_run"] is not None


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_an_unrevoked_owner_dispatches_and_carries_onto_the_terminal(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list_terminals,
    mock_create_terminal,
    mock_send_input,
    flow_file,
):
    """The owner reaches the terminal row, not just the dispatch decision.

    Without this the agent the schedule launches is ownerless, so anything it
    later does -- a message to a supervisor, a delegation -- is back to being
    the server's anonymous work.
    """
    mock_db_get.return_value = _flow(flow_file, OWNER.id)
    mock_backend.return_value.session_exists.return_value = False
    mock_list_terminals.return_value = []

    assert await execute_flow("owned-flow") is True
    assert mock_create_terminal.call_args.kwargs["owner"] == OWNER.id


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_a_flow_registered_before_the_column_existed_still_fires(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list_terminals,
    mock_create_terminal,
    mock_send_input,
    flow_file,
):
    """NULL owner is unknown, not revoked -- upgrades must not strand schedules."""
    mock_db_get.return_value = _flow(flow_file, None)
    mock_backend.return_value.session_exists.return_value = False
    mock_list_terminals.return_value = []

    assert await execute_flow("owned-flow") is True
    assert mock_create_terminal.call_args.kwargs["owner"] is None


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_an_unreadable_owner_fails_closed(
    mock_db_get, mock_update_times, mock_backend, mock_create_terminal, mock_send_input, flow_file
):
    """A row written by a newer version must not launch as the local user.

    ``Principal.parse`` raises on a malformed id and ``execute_flow`` re-raises,
    so the run fails loudly rather than dispatching for an owner nobody resolved.
    """
    mock_db_get.return_value = _flow(flow_file, "no-separator-here")

    with pytest.raises(Exception):
        await execute_flow("owned-flow")
    mock_create_terminal.assert_not_called()


@pytest.mark.asyncio
@patch("cli_agent_orchestrator.services.flow_service.send_input")
@patch("cli_agent_orchestrator.services.flow_service.create_terminal")
@patch("cli_agent_orchestrator.services.flow_service.list_terminals_by_session")
@patch("cli_agent_orchestrator.services.flow_service.get_backend")
@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_run_times")
@patch("cli_agent_orchestrator.services.flow_service.db_get_flow")
async def test_revoking_one_owner_does_not_hold_anothers_flow(
    mock_db_get,
    mock_update_times,
    mock_backend,
    mock_list_terminals,
    mock_create_terminal,
    mock_send_input,
    flow_file,
):
    mock_db_get.return_value = _flow(flow_file, LOCAL_PRINCIPAL.id)
    mock_backend.return_value.session_exists.return_value = False
    mock_list_terminals.return_value = []
    revocation.revoke(OWNER)

    assert await execute_flow("owned-flow") is True


# --- the other half of the criterion: stopping still works ----------------


@patch("cli_agent_orchestrator.services.flow_service.db_update_flow_enabled")
@patch("cli_agent_orchestrator.services.flow_service.db_delete_flow")
def test_a_revoked_owners_flow_can_still_be_disabled_and_removed(
    mock_delete, mock_set_enabled, flow_file
):
    """ "Authorized cancellation/cleanup still functions after revocation."

    Gating these on ``may_start_work`` would leave a removed member's schedule
    permanently un-disableable -- the failure mode worse than the access itself.
    """
    from cli_agent_orchestrator.services.flow_service import disable_flow, remove_flow

    revocation.revoke(OWNER)
    mock_set_enabled.return_value = True
    mock_delete.return_value = True

    assert disable_flow("owned-flow") is True
    assert remove_flow("owned-flow") is True
