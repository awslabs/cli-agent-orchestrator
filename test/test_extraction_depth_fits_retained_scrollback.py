"""Structural guards for the defect class behind #728.

The class: CAO asks a backend to read N lines of scrollback, and how much
scrollback actually survives is decided somewhere else entirely -- a global
tmux option, raised once per pane-creating path. The two numbers live in
different modules and nothing related them, so the deepest rungs of
``get_output``'s extraction ladder (5000 lines) and its overflow threshold
(4500) were silently unreachable on tmux's 2000-line default. Every unit test
on that path mocks the backend, and a mock cannot clamp, so the suite was
green while the real channel dropped a finished worker's reply.

Two properties close it, both enumerated rather than spot-checked:

1. Every read depth declared anywhere in ``src/`` fits inside the scrollback
   floor CAO guarantees (``TMUX_HISTORY_LIMIT``). A rung deeper than the floor
   is dead weight that silently returns less than it asks for.
2. Every ``TmuxClient`` path that creates a tmux session, window or pane
   raises that floor first -- directly, or via a caller that does. A pane reads
   ``history-limit`` only at creation, so a creation path that skips the raise
   re-opens the bug for its own terminals.
"""

import ast
from pathlib import Path

import pytest

from cli_agent_orchestrator import constants

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"
TMUX_CLIENT = SRC / "clients" / "tmux.py"
TERMINAL_SERVICE = SRC / "services" / "terminal_service.py"

# tmux's own compiled-in default. Any read depth above this is unreachable
# unless CAO raises the option, which is the whole mechanism of #728.
TMUX_DEFAULT_HISTORY_LIMIT = 2000

# libtmux calls that bring a new pane into existence. ``split`` is only
# counted when the call passes keywords, which excludes ``str.split``.
_LIBTMUX_CREATORS = {"new_session", "new_window", "split", "split_window"}

_RAISE_FLOOR = "_set_server_history_limit"


def _retained_floor() -> int:
    """The per-pane scrollback depth CAO guarantees it will raise tmux to."""
    floor = getattr(constants, "TMUX_HISTORY_LIMIT", None)
    if not isinstance(floor, int):
        pytest.fail(
            "constants.TMUX_HISTORY_LIMIT is gone: nothing declares how much scrollback "
            "CAO guarantees, so no read depth in the repo can be checked against it and "
            "panes fall back to tmux's 2000-line default (#728)"
        )
    return floor


def _escalation_steps() -> list[int]:
    """Read ``get_output``'s function-local escalation ladder out of the AST."""
    tree = ast.parse(TERMINAL_SERVICE.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_ESCALATION_STEPS" for t in node.targets
        ):
            return [ast.literal_eval(e) for e in node.value.elts]  # type: ignore[attr-defined]
    pytest.fail("could not find _ESCALATION_STEPS in terminal_service.py")


def _declared_tail_lines() -> dict[str, int]:
    """Every ``extraction_tail_lines`` declaration in src/, by file:line."""
    found: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "extraction_tail_lines":
                continue
            returns = [n for n in ast.walk(node) if isinstance(n, ast.Return) and n.value]
            where = f"{path.relative_to(SRC.parent.parent)}:{node.lineno}"
            if len(returns) != 1:
                pytest.fail(
                    f"{where}: extraction_tail_lines must return a single literal depth so "
                    "this guard can compare it against the scrollback floor"
                )
            try:
                found[where] = int(ast.literal_eval(returns[0].value))  # type: ignore[arg-type]
            except (ValueError, TypeError):
                pytest.fail(f"{where}: extraction_tail_lines must return an int literal")
    return found


class TestReadDepthFitsRetainedScrollback:
    """A read depth deeper than the retained floor returns less than it asks."""

    def test_the_floor_is_deeper_than_tmux_default(self) -> None:
        assert _retained_floor() > TMUX_DEFAULT_HISTORY_LIMIT, (
            "CAO must raise tmux's history-limit above its 2000-line default, or every "
            "read deeper than 2000 lines silently returns the same clamped buffer (#728)"
        )

    def test_every_escalation_rung_fits(self) -> None:
        floor = _retained_floor()
        steps = _escalation_steps()
        assert steps, "the escalation ladder must not be empty"
        for step in steps:
            assert step <= floor, (
                f"escalation rung {step} is deeper than the {floor}-line "
                "scrollback floor CAO guarantees, so it cannot read any further than "
                "the rung below it"
            )

    def test_the_overflow_threshold_is_reachable(self) -> None:
        # terminal_service classifies an extraction as overflow at 90% of the
        # deepest rung. Above the floor that arm is dead code and every real
        # overflow is reported as "agent produced no text response" (#728).
        floor = _retained_floor()
        threshold = int(_escalation_steps()[-1] * 0.9)
        assert threshold <= floor, (
            f"the overflow threshold ({threshold} lines) exceeds the scrollback floor "
            f"({floor}), so a genuine overflow can never be labelled as one"
        )

    def test_every_provider_tail_depth_fits(self) -> None:
        floor = _retained_floor()
        declared = _declared_tail_lines()
        assert declared, "expected at least one provider to declare extraction_tail_lines"
        for where, depth in declared.items():
            assert depth <= floor, (
                f"{where} asks for {depth} lines, deeper than the {floor}-line "
                "floor; raise TMUX_HISTORY_LIMIT or lower the provider's depth"
            )


class _TmuxClientMethods:
    """Call-graph facts about ``TmuxClient``, read from the AST."""

    def __init__(self) -> None:
        tree = ast.parse(TMUX_CLIENT.read_text())
        cls = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "TmuxClient"),
            None,
        )
        assert cls is not None, "TmuxClient class not found in clients/tmux.py"
        self.names: set[str] = set()
        self.creators: dict[str, list[int]] = {}
        self.raises: dict[str, list[int]] = {}
        self.calls: dict[str, set[str]] = {}
        for fn in cls.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            self.names.add(fn.name)
            self.creators[fn.name] = []
            self.raises[fn.name] = []
            self.calls[fn.name] = set()
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                attr = node.func.attr
                if attr in _LIBTMUX_CREATORS and not (attr == "split" and not node.keywords):
                    self.creators[fn.name].append(node.lineno)
                elif attr == _RAISE_FLOOR:
                    self.raises[fn.name].append(node.lineno)
                else:
                    self.calls[fn.name].add(attr)

    def callers_of(self, name: str) -> set[str]:
        return {m for m in self.names if name in self.calls[m]}

    def reaches_creator(self, name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name in seen:
            return False
        if self.creators[name]:
            return True
        seen = seen | {name}
        return any(self.reaches_creator(c, seen) for c in self.calls[name] if c in self.names)

    def guarded(self) -> set[str]:
        """Methods that raise the floor, or are only reached through one that does."""
        guarded = {m for m in self.names if self.raises[m]}
        changed = True
        while changed:
            changed = False
            for m in self.names - guarded:
                callers = self.callers_of(m)
                if callers and callers <= guarded:
                    guarded.add(m)
                    changed = True
        return guarded


class TestEveryPaneCreatingPathRaisesTheScrollbackFloor:
    """A pane reads ``history-limit`` at creation; a path that skips the raise keeps 2000."""

    def test_no_creation_path_skips_the_raise(self) -> None:
        methods = _TmuxClientMethods()
        creating = {m for m in methods.names if methods.reaches_creator(m)}
        assert creating, "expected TmuxClient to create tmux sessions/windows/panes"
        unguarded = sorted(creating - methods.guarded())
        assert not unguarded, (
            f"TmuxClient.{{{', '.join(unguarded)}}} brings a new tmux pane into existence "
            f"without calling {_RAISE_FLOOR}() (directly or via every caller). That pane "
            f"keeps tmux's {TMUX_DEFAULT_HISTORY_LIMIT}-line default, which is shallower "
            "than the extraction ladder reaches, and a long worker reply is lost (#728)"
        )

    def test_the_raise_precedes_the_creation_it_protects(self) -> None:
        methods = _TmuxClientMethods()
        for name in sorted(methods.names):
            if not (methods.raises[name] and methods.creators[name]):
                continue
            assert min(methods.raises[name]) < min(methods.creators[name]), (
                f"TmuxClient.{name} calls {_RAISE_FLOOR}() at line "
                f"{min(methods.raises[name])}, after it creates a pane at line "
                f"{min(methods.creators[name])}. A pane reads history-limit only at "
                "creation, so the raise must come first (#728)"
            )
