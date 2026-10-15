"""create_terminal bounds every child by the caller's STORED delegation ceiling.

Real SQLite rows (``isolated_memory_db``) so the laundering test reads the
intermediate terminal's recorded ceiling the way the server does; tmux, the
provider and the FIFO pipeline are mocked.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.terminal_service import create_terminal
from cli_agent_orchestrator.utils.delegation import DelegationCeilingError

pytestmark = pytest.mark.usefixtures("isolated_memory_db")

_TS = "cli_agent_orchestrator.services.terminal_service"
REVIEWER = ROLE_TOOL_DEFAULTS["reviewer"]
SUPERVISOR = ROLE_TOOL_DEFAULTS["supervisor"]
DEVELOPER = ROLE_TOOL_DEFAULTS["developer"]

PROFILES = {
    "reviewer": AgentProfile(name="reviewer", description="r", role="reviewer"),
    "developer": AgentProfile(name="developer", description="d", role="developer"),
    # A user profile that CLAIMS the supervisor role (ceiling * by default).
    "sup": AgentProfile(name="sup", description="s", role="supervisor"),
    "widened_reviewer": AgentProfile(
        name="widened_reviewer",
        description="r",
        role="reviewer",
        delegateTools=["execute_bash", "fs_*"],
    ),
    "unrestricted": AgentProfile(name="unrestricted", description="u", allowedTools=["*"]),
    "shell_worker": AgentProfile(
        name="shell_worker", description="w", allowedTools=["fs_read", "execute_bash"]
    ),
}


@pytest.fixture
def launch(monkeypatch):
    """Patch out tmux, the provider, FIFOs and ids; keep the database real."""
    ids = iter(f"{n:08x}" for n in range(0xA0000000, 0xA00000FF))
    monkeypatch.setattr(f"{_TS}.generate_terminal_id", lambda: next(ids))
    monkeypatch.setattr(f"{_TS}.generate_window_name", lambda p: f"{p}-win")
    monkeypatch.setattr(f"{_TS}.generate_session_name", lambda: "cao-sess")
    monkeypatch.setattr(f"{_TS}.agent_profiles.load_agent_profile", lambda name: PROFILES[name])
    monkeypatch.setattr(f"{_TS}.status_monitor", MagicMock())
    monkeypatch.setattr(f"{_TS}.fifo_manager", MagicMock())
    fifo_dir = MagicMock()
    fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
    monkeypatch.setattr(f"{_TS}.FIFO_DIR", fifo_dir)
    provider = AsyncMock()
    provider.initialize.return_value = True
    pm = MagicMock()
    pm.create_provider.return_value = provider
    monkeypatch.setattr(f"{_TS}.provider_manager", pm)
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.create_window.return_value = "win"
    backend.supports_event_inbox.return_value = True
    monkeypatch.setattr("cli_agent_orchestrator.backends.registry._backend", backend)

    async def _create(profile, **kw):
        return await create_terminal(
            "claude_code", profile, session_name="cao-sess", new_session=False, **kw
        )

    return _create


def _seed(terminal_id, profile, allowed, ceiling):
    db_create_terminal(
        terminal_id,
        "cao-sess",
        f"{profile}-win",
        "claude_code",
        profile,
        allowed,
        delegate_tools=ceiling,
    )


@pytest.mark.asyncio
async def test_three_generation_laundering_is_bounded_by_the_stored_ceiling(launch):
    """reviewer -> supervisor-role profile -> developer must still be refused.

    The intermediate profile says ``role: supervisor`` (declared ceiling ``*``),
    but its ROW records the reviewer's ceiling, and the grandchild is bounded by
    that row, never by what the intermediate's profile claims.
    """
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)  # gen 1, operator-launched

    middle = await launch("sup", caller_id="0000beef")  # gen 2: fits (its tools ⊆ reviewer)
    assert middle.allowed_tools == SUPERVISOR
    assert middle.delegate_tools == REVIEWER, "declared * must be capped by the parent's row"
    assert get_terminal_metadata(middle.id)["delegate_tools"] == REVIEWER

    with pytest.raises(DelegationCeilingError) as excinfo:  # gen 3
        await launch("developer", caller_id=middle.id)
    message = str(excinfo.value)
    assert middle.id in message and "profile 'sup'" in message and "'developer'" in message
    assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]
    assert "delegateTools" in message


@pytest.mark.asyncio
async def test_reviewer_cannot_launch_developer_but_supervisor_can(launch):
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    _seed("0000cafe", "supervisor", SUPERVISOR, ["*"])

    with pytest.raises(DelegationCeilingError):
        await launch("developer", caller_id="0000beef")
    child = await launch("developer", caller_id="0000cafe")
    assert child.allowed_tools == DEVELOPER
    assert child.delegate_tools == DEVELOPER  # developer's default ceiling is its own list


@pytest.mark.asyncio
async def test_client_allowed_tools_string_is_bounded(launch):
    """The in-session API path stores the client's list; the server must not
    trust it beyond the caller's ceiling (``allowed_tools=*`` from a reviewer)."""
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)

    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("reviewer", caller_id="0000beef", allowed_tools=["*"])
    assert excinfo.value.excess == ["*"]

    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("reviewer", caller_id="0000beef", allowed_tools=["fs_read", "execute_bash"])
    assert excinfo.value.excess == ["execute_bash"]

    ok = await launch("reviewer", caller_id="0000beef", allowed_tools=["fs_read"])
    assert ok.allowed_tools == ["fs_read"]
    assert ok.delegate_tools == ["fs_read"]


@pytest.mark.asyncio
async def test_missing_caller_row_fails_closed(launch):
    with pytest.raises(DelegationCeilingError, match="has no registered row"):
        await launch("reviewer", caller_id="deadbeef")


@pytest.mark.asyncio
async def test_legacy_caller_row_is_bounded_by_its_own_allowlist(launch):
    """A supervisor row from before the column (delegate_tools NULL) cannot widen
    until relaunched: strict reading, documented in the migration note."""
    _seed("0000cafe", "supervisor", SUPERVISOR, None)
    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("developer", caller_id="0000cafe")
    assert "relaunch" in str(excinfo.value).lower()
    assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]


@pytest.mark.asyncio
async def test_declared_delegate_tools_widen_a_restricted_parent(launch):
    """The remedy works: a reviewer that declares delegateTools may launch a
    child holding tools the reviewer itself lacks, and the child's own ceiling
    is capped by the parent's."""
    widened = [*REVIEWER, "execute_bash", "fs_*"]
    _seed("0000beef", "widened_reviewer", REVIEWER, widened)
    child = await launch("shell_worker", caller_id="0000beef")
    assert child.allowed_tools == ["fs_read", "execute_bash"]
    assert child.delegate_tools == ["fs_read", "execute_bash"]


@pytest.mark.asyncio
async def test_declared_delegate_tools_still_refuse_what_they_omit(launch):
    widened = [*REVIEWER, "execute_bash", "fs_*"]
    _seed("0000beef", "widened_reviewer", REVIEWER, widened)
    with pytest.raises(DelegationCeilingError) as excinfo:
        await launch("developer", caller_id="0000beef")
    assert excinfo.value.excess == ["web_fetch"]


@pytest.mark.asyncio
async def test_workflow_step_inherits_the_callers_ceiling(launch):
    """delegating_terminal_id bounds a step whose callback parent is not the caller."""
    _seed("0000beef", "reviewer", REVIEWER, REVIEWER)
    with pytest.raises(DelegationCeilingError):
        await launch("developer", delegating_terminal_id="0000beef")
    step = await launch("reviewer", delegating_terminal_id="0000beef")
    assert step.caller_id is None
    assert step.delegate_tools == REVIEWER


@pytest.mark.asyncio
async def test_asserted_caller_ceiling_bounds_a_remote_child(launch):
    with pytest.raises(DelegationCeilingError):
        await launch("developer", caller_ceiling=REVIEWER)
    child = await launch("reviewer", caller_ceiling=REVIEWER)
    assert child.delegate_tools == REVIEWER
    wide = await launch("developer", caller_ceiling=["*"])
    assert wide.delegate_tools == DEVELOPER


@pytest.mark.asyncio
async def test_asserted_ceiling_is_rejected_with_a_local_caller(launch):
    _seed("0000cafe", "supervisor", SUPERVISOR, ["*"])
    with pytest.raises(ValueError, match="caller_ceiling is only accepted"):
        await launch("developer", caller_id="0000cafe", caller_ceiling=["*"])


@pytest.mark.asyncio
async def test_operator_launch_records_the_declared_ceiling(launch):
    sup = await launch("sup")
    assert sup.delegate_tools == ["*"]
    rev = await launch("reviewer")
    assert rev.delegate_tools == REVIEWER
    yolo = await launch("reviewer", allowed_tools=["*"])
    assert yolo.delegate_tools == ["*"]
    unrestricted = await launch("unrestricted")
    assert unrestricted.delegate_tools == ["*"]
