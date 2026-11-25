"""Status invariants for the mock CLI's captured output."""

from hypothesis import given
from hypothesis import strategies as st

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.mock_cli import MockCliProvider


@given(buffer=st.text())
def test_status_parser_is_total_for_text(buffer: str) -> None:
    """Any captured text yields a status rather than crashing the monitor."""
    provider = MockCliProvider("terminal", "session", "window")
    assert isinstance(provider.get_status(buffer), TerminalStatus)


@given(
    prefix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 ", max_size=100),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789 ", max_size=100),
    color=st.integers(min_value=0, max_value=107),
)
def test_ansi_decoration_preserves_completed_status(prefix: str, suffix: str, color: int) -> None:
    """ANSI styling and unrelated text cannot hide a completed response/prompt."""
    provider = MockCliProvider("terminal", "session", "window")
    buffer = f"{prefix}\n> MOCK: response\n\x1b[{color}m❯\x1b[0m {suffix}\n❯ "
    assert provider.get_status(buffer) == TerminalStatus.COMPLETED
