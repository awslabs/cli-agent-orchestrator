"""The ``must_run`` gate: turn a silently skipped assurance test into a failure.

Some tests in this suite are not ordinary unit tests — they are *assurance*
tests whose only job is to assert a property CI depends on (the wheel really
contains the built web UI, the committed crate graph really has no Python FFI,
the gitleaks rules really catch the strings they claim to). Those tests
necessarily guard their assertions on a precondition: a tool on ``PATH``, a
build artifact on disk. When the precondition disappears the test *skips*,
pytest exits 0, and the gate reports success while asserting nothing. Nobody
reads a skip count, so the gate can stay dead for months.

``@pytest.mark.must_run("<gate>")`` says: *in the CI job that owns ``<gate>``,
this test skipping or being deselected is a failure, not a pass.*

Enforcement is scoped on purpose. A test can be legitimately skipped in one job
and load-bearing in another — ``test_gitleaks_config.py`` has no gitleaks binary
in the pip-only Unit Tests job but is the whole point of the gitleaks job in
``secret-scan.yml``. So the marker names a gate, and a run enforces only the
gates listed in the ``CAO_MUST_RUN`` environment variable (comma-separated).
With ``CAO_MUST_RUN`` unset — every ordinary local run — a *well-formed*
marker is a no-op; the gate only prints a note, so a developer without
cargo/trivy/gitleaks installed is not blocked. A bare ``@pytest.mark.must_run``
with no gate name is a misuse and still fails the session regardless of
``CAO_MUST_RUN``, because that is broken code that should never reach main.

Which jobs set which gates is itself asserted by ``test_must_run_gate.py``;
without that, a gate that no job enforces would be exactly the dead guard this
module exists to prevent.
"""

import os
from typing import Dict, Iterable, List, Sequence, Set, Tuple

import pytest

MARKER = "must_run"
ENV_VAR = "CAO_MUST_RUN"

#: Registered via ``pytest_configure`` below -- the single source of truth for
#: the marker's help text (``pytest --markers``).
MARKER_DESCRIPTION = (
    "must_run(gate): assurance test that must actually execute in the CI job "
    "owning <gate>; a skip or deselection fails the session (see test/_must_run.py)"
)


def active_gates(environ: "os._Environ[str] | dict[str, str] | None" = None) -> Set[str]:
    """Gate names this session enforces, parsed from ``CAO_MUST_RUN``."""
    env = os.environ if environ is None else environ
    raw = env.get(ENV_VAR, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def marker_gates(marker: "pytest.Mark | None") -> Tuple[str, ...]:
    """Gate names carried by a ``must_run`` marker.

    ``@pytest.mark.must_run("unit")`` -> ``("unit",)``. A bare
    ``@pytest.mark.must_run`` carries no gate and so is never enforced; that is
    a misuse, and :class:`MustRunGate` reports it rather than ignoring it.
    """
    if marker is None:
        return ()
    return tuple(str(arg) for arg in marker.args)


class MustRunGate:
    """Collects ``must_run`` skips/deselections and fails the session for them."""

    def __init__(self, gates: Iterable[str]) -> None:
        self.gates: Set[str] = set(gates)
        self.violations: List[str] = []
        self.misuses: List[str] = []
        self.owned: List[str] = []
        self._gates_by_nodeid: Dict[str, Tuple[str, ...]] = {}

    # -- session lifecycle --------------------------------------------------
    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Fail closed when this process cannot see collection at all.

        Under pytest-xdist (``-n``), collection and ``pytest_itemcollected`` /
        ``pytest_collection_modifyitems`` run on the workers, not on this
        (controller) plugin instance, so ``self._gates_by_nodeid`` and
        ``self.owned`` never get populated here and every enforcement check
        below silently never fires. Left unchecked, the controller would print
        "0 test(s) ... ran as required" and exit 0 -- the exact silent pass
        this module exists to prevent. Catch it upfront instead.
        """
        if self.gates and session.config.pluginmanager.hasplugin("dsession"):
            self.violations.append(
                f"{ENV_VAR} cannot be enforced under pytest-xdist ('-n'); this process "
                "never sees collection, so the gate would otherwise report success "
                "without checking anything. Rerun the gated job without '-n'."
            )

    # -- collection -------------------------------------------------------
    def pytest_itemcollected(self, item: pytest.Item) -> None:
        """Record every ``must_run`` item as it is collected.

        ``report.keywords[MARKER]`` is a truthy sentinel (``1``), not the
        :class:`pytest.Mark`, so the gate names are not recoverable from a
        report. Capture them here — ``pytest_itemcollected`` fires for every
        item, including ones a later ``pytest_collection_modifyitems`` will
        deselect — and look them up by nodeid at report time.
        """
        marker = item.get_closest_marker(MARKER)
        if marker is None:
            return
        gates = marker_gates(marker)
        if not gates:
            self.misuses.append(f"{item.nodeid}: @pytest.mark.{MARKER} with no gate name")
            return
        self._gates_by_nodeid[item.nodeid] = gates
        if self._owned(gates):
            self.owned.append(item.nodeid)

    def pytest_deselected(self, items: Sequence[pytest.Item]) -> None:
        for item in items:
            gates = marker_gates(item.get_closest_marker(MARKER))
            if self._owned(gates):
                self.violations.append(
                    f"{item.nodeid}: DESELECTED, but marked must_run{list(gates)}. "
                    "A marker expression or -k filter removed an assurance test the "
                    "job is supposed to enforce."
                )

    def pytest_collection_modifyitems(
        self, session: pytest.Session, config: pytest.Config, items: Sequence[pytest.Item]
    ) -> None:
        """Catch a gate with no owned test at all: deleted, renamed, or misspelled.

        ``pytest_itemcollected`` has already fired for every collected item by
        the time this runs, so ``self._gates_by_nodeid`` is complete regardless
        of any later deselection. A gate in ``CAO_MUST_RUN`` with no matching
        marker anywhere asserts nothing — the exact silent-skip failure mode
        this module exists to prevent, just one step earlier.
        """
        del session, config, items
        seen = {gate for gates in self._gates_by_nodeid.values() for gate in gates}
        for gate in sorted(self.gates - seen):
            self.violations.append(
                f"gate {gate!r}: no must_run test was collected for it, but "
                f"{ENV_VAR} enforces it. The marked test may have been deleted, "
                "renamed, or misspelled."
            )

    # -- execution --------------------------------------------------------
    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when not in ("setup", "call") or not report.skipped:
            return
        if getattr(report, "wasxfail", None) is not None:
            return
        gates = self._gates_by_nodeid.get(report.nodeid, ())
        if not self._owned(gates):
            return
        reason = ""
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            # ("<path>", <lineno>, "Skipped: <reason>") -- keep just the reason.
            reason = str(report.longrepr[2]).removeprefix("Skipped: ")
        self.violations.append(
            f"{report.nodeid}: SKIPPED ({reason or 'no reason given'}), but marked "
            f"must_run{list(gates)}. The precondition this assurance test needs is "
            "missing in the job that is supposed to enforce it, so the gate asserted "
            "nothing."
        )

    # -- reporting --------------------------------------------------------
    def pytest_terminal_summary(self, terminalreporter) -> None:  # type: ignore[no-untyped-def]
        write = terminalreporter.write_line
        for line in self.misuses:
            write(f"must_run: MISUSE {line}")
        for line in self.violations:
            write(f"must_run: VIOLATION {line}")
        if self.gates and not self.violations and not self.misuses:
            write(
                f"must_run: {len(self.owned)} test(s) for gate(s) "
                f"{sorted(self.gates)} ran as required"
            )

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        del exitstatus
        if self.violations or self.misuses:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    # -- internals --------------------------------------------------------
    def _owned(self, gates: Sequence[str]) -> bool:
        return bool(set(gates) & self.gates)


def pytest_configure(config: pytest.Config) -> None:
    """Register the gate for this session.

    Registered unconditionally so that an unenforced ``must_run`` misuse (a bare
    marker with no gate name) is still reported locally; only *enforcement* is
    scoped to ``CAO_MUST_RUN``.
    """
    config.addinivalue_line("markers", MARKER_DESCRIPTION)
    config.pluginmanager.register(MustRunGate(active_gates()), "cao-must-run")
