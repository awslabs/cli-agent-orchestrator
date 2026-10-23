"""Structural guards for the #814 defect class.

#814: ``assume_processing_on_dispatch`` publishes PROCESSING *without* provider
evidence (an optimistic latch), yet ``_midburst_processing_probe`` decided
"already busy, nothing to add" by reading the ``_last_status`` field alone. The
field carries two meanings -- observed and assumed -- and only the companion
``_processing_generation`` stamp distinguishes them. Two things let that ship:

1. Nothing stopped a new reader of the latch from keying on the field instead
   of its provenance stamp (``TestLatchReadersAreRegistered``).
2. ``assume_processing_on_dispatch`` is declared on ``BaseProvider`` but set by
   no provider in the tree, so its True branch is unreachable from every
   provider-driven test and nothing required it be exercised at all
   (``TestCapabilityFlagsAreExercised``).

What these guards are, and are not. They read source text, so they can
ENUMERATE the readers of the latch and the class-level declarations of a flag,
and they can check that a behavior test registered for each one exists. They
cannot tell whether a reader uses the stamp correctly: the first version of
this file accepted a function because a provenance name occurred somewhere in
it, and it passed while the probe's key was wrong (review of PR #998). The
proof that a reader is right is the production-path behavior test registered
against it, which fails on the defective code. These guards only make sure no
reader, and no flag, goes without one.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PROVIDERS_DIR = REPO_ROOT / "src/cli_agent_orchestrator/providers"
STATUS_MONITOR = REPO_ROOT / "src/cli_agent_orchestrator/services/status_monitor.py"

# Statuses a terminal can hold *without* having earned them this turn, because
# notify_input_sent can publish them optimistically.
ASSUMABLE = {"PROCESSING", "WAITING_USER_ANSWER"}

# Every function in status_monitor.py that compares a ``_last_status`` read to
# an assumable status: what the read decides, and the production-path behavior
# test that pins it. A new reader fails the guard until it is registered here
# with a test. The test, not the entry, is what guards the reader.
LATCH_READERS: dict[str, tuple[str, str]] = {
    "_processing_observed_this_turn_locked": (
        "the provenance rule itself: cached PROCESSING counts only with a stamp "
        "newer than the turn's dispatch generation",
        "test/services/test_status_monitor.py::TestLatchSkipsThroughProcessChunk::"
        "test_dispatch_latch_without_evidence_is_probed_once_then_skipped",
    ),
    "_schedule_raw_detection": (
        "debounces per-chunk detection only once PROCESSING is observed this turn, "
        "through the helper above",
        "test/services/test_status_monitor.py::TestLatchSkipsThroughProcessChunk::"
        "test_raw_path_under_dispatch_latch_keeps_detecting_until_evidence",
    ),
    "_apply_detection": (
        "re-stamps an unchanged PROCESSING only when the verdict carries " "processing_evidence",
        "test/services/test_status_monitor.py::TestStatusObservationIsStampedWhenEarned::"
        "test_a_changed_status_is_stamped_with_the_output_that_produced_it",
    ),
    "get_status": (
        "decides whether to re-check a cached PROCESSING/WAITING; the re-check is "
        "gated on the stamps in _fresh_capture_pane_status",
        "test/services/test_status_monitor.py::TestCopilotBoxedComposerRecovery::"
        "test_unearned_new_turn_never_schedules_a_capture",
    ),
    "_fresh_capture_pane_status": (
        "selects the waiting-vs-processing recovery gate; both compare the stamps "
        "to the pinned generation",
        "test/services/test_status_monitor.py::TestCopilotStaleWaitingCapture::"
        "test_new_input_invalidates_waiting_capture_evidence",
    ),
    "_schedule_stale_capture_confirmation.arm": (
        "only decides whether to arm a timer; the confirming read runs the gates in "
        "_fresh_capture_pane_status",
        "test/services/test_status_monitor.py::TestCopilotBoxedComposerRecovery::"
        "test_unearned_new_turn_never_schedules_a_capture",
    ),
}


def _own_nodes(fn: ast.AST):
    """Walk a function's own nodes, not those of functions nested inside it.

    Attribution must be innermost-enclosing: a nested closure's read is the
    closure's, and a registered entry for one closure does not vouch for a
    bare field read in its sibling.
    """
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(ast.iter_child_nodes(node))


def _walk_functions(tree: ast.AST, prefix: str = ""):
    """Yield (FunctionDef, dotted path) for every function, nested ones included."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            path = f"{prefix}{node.name}"
            yield node, path
            yield from _walk_functions(node, f"{path}.")
        elif isinstance(node, ast.ClassDef):
            yield from _walk_functions(node, prefix)


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


def _test_exists(ref: str) -> bool:
    """True when ``module::Class::func`` (or ``module::func``) names a real test
    function -- a definition in the module's AST, not a substring of its text.
    A docstring that mentions the name does not count (review of PR #998, M3).
    """
    module, *qualname = ref.split("::")
    path = REPO_ROOT / module
    if not path.is_file() or not qualname:
        return False
    scope: ast.AST = ast.parse(path.read_text())
    for name in qualname[:-1]:
        scope = next(
            (
                n
                for n in ast.iter_child_nodes(scope)
                if isinstance(n, ast.ClassDef) and n.name == name
            ),
            None,
        )
        if scope is None:
            return False
    return any(
        isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == qualname[-1]
        for n in ast.iter_child_nodes(scope)
    )


class TestLatchReadersAreRegistered:
    """Every reader of the latch field is enumerated and has a behavior test.

    This is a change-time prompt, not a data-flow proof: it fires when a reader
    appears that nobody has written a production-path test for, and when a
    registered reader or its test goes away. Whether the reader is RIGHT is the
    registered test's job.
    """

    def test_every_latch_reader_is_registered_with_a_behavior_test(self):
        readers = _latch_readers(ast.parse(STATUS_MONITOR.read_text()))
        assert readers, "pattern matched nothing -- the guard has rotted, not the code"

        unregistered = {
            f"{STATUS_MONITOR.name}:{lines[0]} in {path}()"
            for path, lines in readers.items()
            if path not in LATCH_READERS
        }
        assert not unregistered, (
            "These compare `_last_status` to an assumable status (PROCESSING / "
            "WAITING_USER_ANSWER), which notify_input_sent can publish without "
            f"evidence (#814): {sorted(unregistered)}. Decide through "
            "_processing_observed_this_turn_locked (or the generation stamps), write a "
            "production-path test that fails without it, and register both in "
            "LATCH_READERS."
        )

    def test_registry_has_no_stale_entries(self):
        readers = _latch_readers(ast.parse(STATUS_MONITOR.read_text()))
        stale = sorted(set(LATCH_READERS) - set(readers))
        assert not stale, f"no longer read the latch; drop them from LATCH_READERS: {stale}"

    @pytest.mark.parametrize("reader", sorted(LATCH_READERS))
    def test_registered_behavior_test_exists(self, reader):
        reason, test_ref = LATCH_READERS[reader]
        assert reason.strip(), f"{reader}: say what the read decides"
        assert _test_exists(test_ref), f"{reader}: registered test is not defined: {test_ref}"


class TestCapabilityFlagsAreExercised:
    """A declared provider capability must be reachable by something."""

    # flag -> ``module::Class::test`` for flags no shipped provider sets at a
    # non-default value: a class in that module must declare the flag at the
    # non-default value, and the named test must exist.
    SYNTHETIC_ONLY = {
        "assume_processing_on_dispatch": (
            "test/services/test_terminal_service_full.py::"
            "TestDispatchLatchDeclaredOnAProvider::"
            "test_declared_flag_reaches_the_monitor_and_the_probe_still_runs"
        ),
    }

    NON_CONSTANT = object()

    @staticmethod
    def _base_flags() -> dict[str, tuple[int, bool]]:
        """BaseProvider's boolean capability flags: name -> (line, default)."""
        flags: dict[str, tuple[int, bool]] = {}
        for node in ast.walk(ast.parse((PROVIDERS_DIR / "base.py").read_text())):
            if not (isinstance(node, ast.ClassDef) and node.name == "BaseProvider"):
                continue
            for st in node.body:
                if (
                    isinstance(st, ast.AnnAssign)
                    and isinstance(st.target, ast.Name)
                    and ast.unparse(st.annotation) == "bool"
                    and isinstance(st.value, ast.Constant)
                    and isinstance(st.value.value, bool)
                ):
                    flags[st.target.id] = (st.lineno, st.value.value)
                elif isinstance(st, ast.FunctionDef) and "property" in [
                    ast.unparse(d) for d in st.decorator_list
                ]:
                    rets = [
                        r for r in ast.walk(st) if isinstance(r, ast.Return) and r.value is not None
                    ]
                    values = {
                        r.value.value
                        for r in rets
                        if isinstance(r.value, ast.Constant) and isinstance(r.value.value, bool)
                    }
                    if rets and len(values) == 1 and len(rets) == len(values):
                        flags[st.name] = (st.lineno, values.pop())
        return flags

    @classmethod
    def _class_level_values(cls, source: str, flags: dict[str, tuple[int, bool]]):
        """Values each flag is declared with at CLASS level in ``source``.

        Only class-body statements count: a local variable, an argument, or a
        method that happens to share a flag's name is not an override. A
        property that returns anything but one boolean constant is recorded as
        NON_CONSTANT, since it can take the non-default value at runtime.
        """
        declared: dict[str, set[object]] = {}
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ClassDef):
                continue
            for st in node.body:
                target = None
                value: object = cls.NON_CONSTANT
                if isinstance(st, ast.Assign) and len(st.targets) == 1:
                    target = st.targets[0]
                elif isinstance(st, ast.AnnAssign):
                    target = st.target
                if target is not None:
                    if not isinstance(target, ast.Name) or target.id not in flags:
                        continue
                    if isinstance(st.value, ast.Constant) and isinstance(st.value.value, bool):
                        value = st.value.value
                    declared.setdefault(target.id, set()).add(value)
                elif isinstance(st, ast.FunctionDef) and st.name in flags:
                    rets = [
                        r for r in ast.walk(st) if isinstance(r, ast.Return) and r.value is not None
                    ]
                    consts = {
                        r.value.value
                        for r in rets
                        if isinstance(r.value, ast.Constant) and isinstance(r.value.value, bool)
                    }
                    if rets and len(consts) == 1 and len(rets) == len(consts):
                        value = consts.pop()
                    declared.setdefault(st.name, set()).add(value)
        return declared

    @classmethod
    def _non_default_in(cls, source: str, flags: dict[str, tuple[int, bool]]) -> set[str]:
        """Flags that some class in ``source`` declares at a non-default value."""
        return {
            flag
            for flag, values in cls._class_level_values(source, flags).items()
            if any(v is cls.NON_CONSTANT or v != flags[flag][1] for v in values)
        }

    @classmethod
    def _provider_overrides(cls, flags: dict[str, tuple[int, bool]]) -> set[str]:
        overridden: set[str] = set()
        for path in PROVIDERS_DIR.glob("*.py"):
            if path.name != "base.py":
                overridden |= cls._non_default_in(path.read_text(), flags)
        return overridden

    def test_enumerator_counts_only_class_level_non_default_declarations(self):
        """The enumerator's own contract, since a false 'overridden' hides a flag."""
        flags = {"f_default_false": (1, False), "f_prop": (2, False), "f_local": (3, False)}
        source = (
            "class P:\n"
            "    f_default_false = False\n"  # restates the default: not an override
            "    @property\n"
            "    def f_prop(self):\n"
            "        return True\n"  # non-default: an override
            "    def method(self):\n"
            "        f_local = True\n"  # a local, not a declaration
            "        return f_local\n"
        )
        assert self._non_default_in(source, flags) == {"f_prop"}
        assert "f_local" not in self._class_level_values(source, flags)

    def test_no_flag_is_declared_without_being_exercised(self):
        flags = self._base_flags()
        assert "assume_processing_on_dispatch" in flags, "flag renamed -- update this guard"
        overridden = self._provider_overrides(flags)
        unreachable = {
            flag: line
            for flag, (line, _default) in flags.items()
            if flag not in overridden and flag not in self.SYNTHETIC_ONLY
        }
        assert not unreachable, (
            "These BaseProvider capability flags are set to a non-default value by no "
            "provider and claimed by no synthetic test, so their non-default branch is "
            "unreachable from the whole suite -- exactly how #814 stayed latent: "
            f"{ {f: f'base.py:{ln}' for f, ln in sorted(unreachable.items())} }. "
            "Override them on a provider, or add a SYNTHETIC_ONLY entry naming the test "
            "whose module declares the non-default value on a provider class."
        )

    @pytest.mark.parametrize("flag,test_ref", sorted(SYNTHETIC_ONLY.items()))
    def test_synthetic_only_flags_really_are_exercised(self, flag, test_ref):
        """A SYNTHETIC_ONLY entry is a promise; check the cited test keeps it."""
        flags = self._base_flags()
        if flag in self._provider_overrides(flags):
            pytest.fail(f"{flag} is now set by a provider -- drop it from SYNTHETIC_ONLY")
        module = REPO_ROOT / test_ref.split("::")[0]
        assert module.is_file(), f"{flag} cites a missing test module: {module}"
        assert flag in self._non_default_in(module.read_text(), flags), (
            f"{module.name} has no class declaring {flag} at its non-default value; "
            "a docstring or a direct notify_input_sent(assume_processing=True) call does "
            "not exercise the provider branch"
        )
        assert _test_exists(test_ref), f"{flag}: the cited test is not defined: {test_ref}"
