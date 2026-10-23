"""The ceiling forwarding through run_agent_step is load-bearing, checked on every CI run.

Two adversarial reviews showed that deleting one line,
``delegating_terminal_id=delegating_terminal_id`` in ``agent_step.run_agent_step``,
left the focused tests green. The manual perl/driver mutation that caught it is
corroboration in the PR report; this module is the permanent, CI-resident form:

1. ``run_agent_step`` must hand every ceiling argument it receives to
   ``create_terminal`` unchanged (a recorder stands in for ``create_terminal``).
2. End to end, a reviewer-bounded step is refused; and the SAME fixtures with the
   bound deliberately omitted launch the worker. The pair proves the refusal is
   caused by exactly the forwarded argument, so if a future change stops
   forwarding it, test 1 fails and the refusal in test 3 disappears.
3. The refusal, with the bound forwarded, through the real ``create_terminal``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.services import agent_step, terminal_service
from cli_agent_orchestrator.utils.delegation import DelegationCeilingError

pytestmark = pytest.mark.usefixtures("isolated_memory_db")
_TS = "cli_agent_orchestrator.services.terminal_service"
REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]


@pytest.fixture
def launch_capable(monkeypatch):
    """Real create_terminal; tmux/provider/FIFO/readiness mocked so an unbounded
    worker LAUNCHES rather than a mock raising first."""
    profiles = {"developer": AgentProfile(name="developer", description="d", role="developer")}
    monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda n: profiles[n])
    provider = AsyncMock()
    provider.initialize.return_value = True
    pm = MagicMock()
    pm.create_provider.return_value = provider
    monkeypatch.setattr(f"{_TS}.provider_manager", pm)
    monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
    monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "developer-win"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)
    monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_step, "_wait_for_completion", AsyncMock())
    monkeypatch.setattr(terminal_service, "send_input", MagicMock())
    monkeypatch.setattr(terminal_service, "get_output", lambda *a, **kw: "done")
    db_create_terminal(
        "0000beef", "cao-s", "rev", "claude_code", "reviewer", REVIEWER, delegate_tools=REVIEWER
    )
    return backend


def _developer_rows() -> int:
    with database.SessionLocal() as session:
        return session.query(database.TerminalModel).filter_by(agent_profile="developer").count()


@pytest.mark.asyncio
async def test_run_agent_step_forwards_every_ceiling_argument(monkeypatch):
    """The mutation-killer: the three ceiling kwargs reach create_terminal verbatim."""
    recorder = AsyncMock(
        return_value=Terminal(
            id="abcd1234",
            name="w",
            provider="claude_code",
            session_name="cao-s",
            agent_profile="developer",
            status=TerminalStatus.IDLE,
            last_active="2026-01-01T00:00:00Z",
        )
    )
    monkeypatch.setattr(agent_step.terminal_service, "create_terminal", recorder)
    monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_step, "_wait_for_completion", AsyncMock())
    monkeypatch.setattr(agent_step.terminal_service, "send_input", MagicMock())
    monkeypatch.setattr(agent_step.terminal_service, "get_output", lambda *a, **kw: "done")
    monkeypatch.setattr(agent_step.terminal_service, "delete_terminal", MagicMock())

    await agent_step.run_agent_step(
        "claude_code",
        "developer",
        "go",
        session_name="cao-s",
        caller_id="0000beef",
        delegating_terminal_id="0000cafe",
        caller_ceiling=["fs_read"],
        teardown=False,
    )

    kwargs = recorder.call_args.kwargs
    assert kwargs["caller_id"] == "0000beef"
    assert kwargs["delegating_terminal_id"] == "0000cafe"
    assert kwargs["caller_ceiling"] == ["fs_read"]


@pytest.mark.asyncio
async def test_bounded_step_is_refused_end_to_end(launch_capable):
    with pytest.raises(DelegationCeilingError) as excinfo:
        await agent_step.run_agent_step(
            "claude_code",
            "developer",
            "go",
            session_name="cao-s",
            delegating_terminal_id="0000beef",
            teardown=False,
        )
    assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]
    assert _developer_rows() == 0
    launch_capable.create_window.assert_not_called()


@pytest.mark.asyncio
async def test_the_same_step_without_the_bound_launches(launch_capable):
    """The simulated mutation: omit the forwarded argument and the worker launches.
    This is what the forwarding line prevents; with it present (previous test) the
    identical fixtures refuse."""
    result = await agent_step.run_agent_step(
        "claude_code", "developer", "go", session_name="cao-s", teardown=False
    )
    assert result.status == TerminalStatus.COMPLETED
    assert _developer_rows() == 1
    launch_capable.create_window.assert_called_once()
