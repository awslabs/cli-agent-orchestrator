"""Structural guards for the #814 defect class.

#814: ``assume_processing_on_dispatch`` publishes PROCESSING *without* provider
evidence (an optimistic latch), yet ``_midburst_processing_probe`` decided
"already busy, nothing to add" by reading the ``_last_status`` field alone. The
field carries two meanings -- observed and assumed -- and only the companion
``_processing_generation`` stamp distinguishes them. Two things let that ship:

1. Nothing stopped a new reader of the latch from keying on the field instead
   of its provenance stamp (``TestLatchProvenance``).
2. ``assume_processing_on_dispatch`` is declared on ``BaseProvider`` but set by
   no provider in the tree, so its True branch is unreachable from every
   provider-driven test and nothing required it be exercised at all
   (``TestCapabilityFlagsAreExercised``).
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DIR = REPO_ROOT / "src/cli_agent_orchestrator/providers"
STATUS_MONITOR = REPO_ROOT / "src/cli_agent_orchestrator/services/status_monitor.py"

# Statuses a terminal can hold *without* having earned them this generation,
# because notify_input_sent can publish them optimistically.
ASSUMABLE = {"PROCESSING", "WAITING_USER_ANSWER"}
# Reading any of these in the same function proves the author considered
# provenance rather than trusting the field.
PROVENANCE = {
    "_processing_generation",
    "_waiting_generation",
    "processing_evidence",
    "_processing_observed_this_turn_locked",
}

# Functions that read the latch field for something other than an evidence
# decision. Each needs a reason, and the reason must survive a reviewer.
PROVENANCE_EXEMPT = {
    # Schedules a *confirming* read that itself runs the provenance gates in
    # _fresh_capture_pane_status; this only decides whether to arm a timer.
    "_schedule_stale_capture_confirmation.arm": "defers to _fresh_capture_pane_status",
}


def _own_nodes(fn: ast.AST):
    """Walk a function's own nodes, not those of functions nested inside it.

    Attribution must be innermost-enclosing: a nested closure's conflation is
    the closure's, and a provenance check in one closure does not vouch for a
    bare field read in its sibling.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _latch_readers(tree: ast.AST) -> dict[str, list[int]]:
    """Map function path -> lines comparing a ``_last_status`` read to ASSUMABLE."""

    def is_latch_read(node: ast.AST) -> bool:
        """True for ``self._last_status[tid]`` and ``self._last_status.get(tid)``."""
        if isinstance(node, ast.Call):
            node = node.func
        base = node.value if isinstance(node, (ast.Attribute, ast.Subscript)) else None
        return isinstance(base, ast.Attribute) and base.attr == "_last_status"

    def assumable(node: ast.AST) -> bool:
        parts = [node] if not isinstance(node, (ast.Tuple, ast.List, ast.Set)) else node.elts
        return any(
            isinstance(p, ast.Attribute)
            and p.attr in ASSUMABLE
            and isinstance(p.value, ast.Name)
            and p.value.id == "TerminalStatus"
            for p in parts
        )

    readers: dict[str, list[int]] = {}
    for fn, path in _walk_functions(tree):
        aliases = {
            t.id
            for n in _own_nodes(fn)
            if isinstance(n, ast.Assign) and is_latch_read(n.value)
            for t in n.targets
            if isinstance(t, ast.Name)
        }

        def names_latch(node: ast.AST) -> bool:
            return (isinstance(node, ast.Name) and node.id in aliases) or is_latch_read(node)

        for cmp_node in [n for n in _own_nodes(fn) if isinstance(n, ast.Compare)]:
            operands = [cmp_node.left, *cmp_node.comparators]
            if any(names_latch(o) for o in operands) and any(assumable(o) for o in operands):
                readers.setdefault(path, []).append(cmp_node.lineno)
    return readers


def _walk_functions(tree: ast.AST, prefix: str = ""):
    """Yield (FunctionDef, dotted path) for every function, nested ones included."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            path = f"{prefix}{node.name}"
            yield node, path
            yield from _walk_functions(node, f"{path}.")
        elif isinstance(node, ast.ClassDef):
            yield from _walk_functions(node, prefix)


def _code_names(fn: ast.AST) -> set[str]:
    """Attribute/identifier names a function actually *uses* (docstrings excluded)."""
    return {
        n.attr if isinstance(n, ast.Attribute) else n.id
        for n in _own_nodes(fn)
        if isinstance(n, (ast.Attribute, ast.Name))
    }


class TestLatchProvenance:
    """Every evidence decision on the latch must consult its provenance stamp."""

    def test_every_last_status_latch_reader_consults_provenance(self):
        tree = ast.parse(STATUS_MONITOR.read_text())
        by_path = {path: fn for fn, path in _walk_functions(tree)}
        readers = _latch_readers(tree)
        assert readers, "pattern matched nothing -- the guard has rotted, not the code"

        offenders = [
            f"{STATUS_MONITOR.name}:{lines[0]} in {path}()"
            for path, lines in sorted(readers.items())
            if path not in PROVENANCE_EXEMPT and not (_code_names(by_path[path]) & PROVENANCE)
        ]
        assert not offenders, (
            "These read `_last_status` against an assumable status without "
            "consulting its provenance stamp, so they cannot tell an OBSERVED "
            f"status from one notify_input_sent asserted optimistically (#814): {offenders}. "
            f"Compare against one of {sorted(PROVENANCE)}, or add a reasoned "
            "PROVENANCE_EXEMPT entry."
        )


class TestCapabilityFlagsAreExercised:
    """A declared provider capability must be reachable by something."""

    # flag -> test module that synthesizes a provider declaring the non-default
    # value, for flags no shipped provider overrides.
    SYNTHETIC_ONLY = {
        "assume_processing_on_dispatch": "test/services/test_status_monitor.py",
    }

    @staticmethod
    def _base_flags() -> dict[str, int]:
        flags: dict[str, int] = {}
        for node in ast.walk(ast.parse((PROVIDERS_DIR / "base.py").read_text())):
            if not (isinstance(node, ast.ClassDef) and node.name == "BaseProvider"):
                continue
            for st in node.body:
                if (
                    isinstance(st, ast.AnnAssign)
                    and isinstance(st.target, ast.Name)
                    and ast.unparse(st.annotation) == "bool"
                ):
                    flags[st.target.id] = st.lineno
                elif isinstance(st, ast.FunctionDef) and "property" in [
                    ast.unparse(d) for d in st.decorator_list
                ]:
                    rets = [
                        r for r in ast.walk(st) if isinstance(r, ast.Return) and r.value is not None
                    ]
                    if rets and all(
                        isinstance(r.value, ast.Constant) and isinstance(r.value.value, bool)
                        for r in rets
                    ):
                        flags[st.name] = st.lineno
        return flags

    @staticmethod
    def _provider_overrides() -> set[str]:
        names: set[str] = set()
        for path in PROVIDERS_DIR.glob("*.py"):
            if path.name == "base.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    names.add(node.name)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names.add(node.target.id)
                elif isinstance(node, ast.Assign):
                    names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        return names

    def test_no_flag_is_declared_without_being_exercised(self):
        flags = self._base_flags()
        assert "assume_processing_on_dispatch" in flags, "flag renamed -- update this guard"
        overridden = self._provider_overrides()
        unreachable = {
            flag: line
            for flag, line in flags.items()
            if flag not in overridden and flag not in self.SYNTHETIC_ONLY
        }
        assert not unreachable, (
            "These BaseProvider capability flags are set by no provider and claimed "
            "by no synthetic test, so their non-default branch is unreachable from "
            "the whole suite -- exactly how #814 stayed latent: "
            f"{ {f: f'base.py:{ln}' for f, ln in sorted(unreachable.items())} }. "
            "Override them on a provider, or add a SYNTHETIC_ONLY entry naming the "
            "test that declares the non-default value."
        )

    @pytest.mark.parametrize("flag,module", sorted(SYNTHETIC_ONLY.items()))
    def test_synthetic_only_flags_really_are_exercised(self, flag, module):
        """A SYNTHETIC_ONLY entry is a promise; check the cited test keeps it."""
        if flag in self._provider_overrides():
            pytest.fail(f"{flag} is now set by a provider -- drop it from SYNTHETIC_ONLY")
        cited = REPO_ROOT / module
        assert cited.is_file(), f"{flag} cites a missing test module: {module}"
        assert flag in cited.read_text(), f"{module} no longer exercises {flag}"
