"""Every terminal backend declares how deep a single history read can go (#728).

The class behind #728: ``get_output`` climbs an escalation ladder of read
depths (200 -> 500 -> 1000 -> 5000 lines) and labels an exhausted ladder as
"overflow" or "no response" against a threshold derived from the deepest rung.
How deep a backend can actually read is decided somewhere else entirely -- a
tmux ``history-limit`` option, a ``lines.min(1000)`` clamp in herdr's server --
and nothing related the two numbers. A rung deeper than the backend's ceiling
silently returns the same bytes as the rung below it, and the overflow label
becomes unreachable, so a worker that filled the whole readable depth is
reported as having produced nothing.

``TerminalBackend.max_history_lines`` is where each backend states its
ceiling, and ``get_output`` clips the ladder and the threshold to it. These
tests enumerate the concrete backends rather than spot-checking one, so a new
backend that forgets to declare a ceiling (or declares a nonsensical one)
fails here instead of inheriting the bug.
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

import cli_agent_orchestrator.backends as backends_pkg
from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend
from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend
from cli_agent_orchestrator.constants import HERDR_PANE_READ_MAX_LINES, TMUX_HISTORY_LIMIT

# The shallowest rung of get_output's ladder. A ceiling below it would clip
# the ladder to nothing.
_SHALLOWEST_RUNG = 200


def _concrete_backends() -> list[type[TerminalBackend]]:
    """Every concrete TerminalBackend subclass shipped under ``backends/``."""
    import importlib
    import pkgutil

    found: list[type[TerminalBackend]] = []
    for info in pkgutil.iter_modules(backends_pkg.__path__):
        module = importlib.import_module(f"{backends_pkg.__name__}.{info.name}")
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if (
                issubclass(obj, TerminalBackend)
                and obj is not TerminalBackend
                and not inspect.isabstract(obj)
                and obj.__module__ == module.__name__
            ):
                found.append(obj)
    return found


def _instantiate(cls: type[TerminalBackend]) -> TerminalBackend:
    if cls is TmuxBackend:
        with patch("cli_agent_orchestrator.backends.tmux_backend.TmuxClient"):
            return TmuxBackend(client=MagicMock())
    if cls is HerdrBackend:
        with patch.object(HerdrBackend, "_ensure_session_running"):
            return HerdrBackend()
    pytest.fail(
        f"{cls.__name__} is a new TerminalBackend: teach this test how to build one so its "
        "max_history_lines is checked like the others (#728)"
    )


class TestEveryBackendDeclaresAReadCeiling:
    def test_the_contract_requires_it(self) -> None:
        assert "max_history_lines" in TerminalBackend.__abstractmethods__, (
            "max_history_lines must be abstract on TerminalBackend, or a new backend can "
            "ship without stating how deep get_output may read (#728)"
        )

    def test_enumeration_found_the_shipped_backends(self) -> None:
        names = {cls.__name__ for cls in _concrete_backends()}
        assert {"TmuxBackend", "HerdrBackend"} <= names, names

    @pytest.mark.parametrize("cls", _concrete_backends(), ids=lambda c: c.__name__)
    def test_ceiling_is_a_usable_depth(self, cls: type[TerminalBackend]) -> None:
        cap = _instantiate(cls).max_history_lines
        assert isinstance(cap, int) and not isinstance(cap, bool), (cls.__name__, cap)
        assert cap >= _SHALLOWEST_RUNG, (
            f"{cls.__name__}.max_history_lines={cap} is below the shallowest extraction rung "
            f"({_SHALLOWEST_RUNG}); get_output could not read anything on it"
        )


class TestShippedCeilings:
    def test_tmux_ceiling_is_the_floor_cao_raises_history_limit_to(self) -> None:
        assert _instantiate(TmuxBackend).max_history_lines == TMUX_HISTORY_LIMIT

    def test_herdr_ceiling_is_herdrs_read_clamp(self) -> None:
        assert _instantiate(HerdrBackend).max_history_lines == HERDR_PANE_READ_MAX_LINES == 1000
