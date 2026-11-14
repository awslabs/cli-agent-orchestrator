"""``ensure_session_prefix``: one normalization for every route that names a session."""

from cli_agent_orchestrator.constants import SESSION_PREFIX
from cli_agent_orchestrator.utils.terminal import ensure_session_prefix


def test_bare_name_gets_the_prefix():
    assert ensure_session_prefix("demo") == f"{SESSION_PREFIX}demo"


def test_prefixed_name_is_unchanged():
    assert ensure_session_prefix("cao-demo") == "cao-demo"


def test_idempotent():
    once = ensure_session_prefix("demo")
    assert ensure_session_prefix(once) == once


def test_a_name_that_merely_contains_the_prefix_is_still_prefixed():
    """Only a leading prefix counts: ``my-cao-thing`` is a bare name."""
    assert ensure_session_prefix("my-cao-thing") == "cao-my-cao-thing"
