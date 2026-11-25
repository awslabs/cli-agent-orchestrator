"""Contextual recall cannot cross private memory boundaries."""

from hypothesis import given
from hypothesis import strategies as st

from cli_agent_orchestrator.services.memory_service import MemoryService

scope_ids = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=24)


@given(session_id=scope_ids, agent_id=scope_ids, other_id=scope_ids)
def test_private_scope_visibility_is_independent(
    session_id: str, agent_id: str, other_id: str
) -> None:
    """A session/agent's recall sees only its own ID, never another private ID."""
    allowed = {"session": session_id, "agent": agent_id}
    for scope, own_id in allowed.items():
        assert MemoryService._private_scope_is_visible(scope, own_id, allowed)
        if other_id != own_id:
            assert not MemoryService._private_scope_is_visible(scope, other_id, allowed)
        assert not MemoryService._private_scope_is_visible(scope, None, allowed)
    assert MemoryService._private_scope_is_visible("global", None, allowed)
    assert MemoryService._private_scope_is_visible("project", other_id, allowed)
