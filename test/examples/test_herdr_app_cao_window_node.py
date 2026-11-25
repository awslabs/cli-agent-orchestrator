"""Runs every ``*.test.mjs`` of examples/herdr-app-cao-window under pytest, one case per file.

The node suites need ``node`` (20.19 or later: they load ``.js`` files with ``import`` and no ``package.json``
``"type": "module"``, which older Node reads as CommonJS) and ``jq``. They are marked ``must_run("unit")``: on the CI leg that
owns the ``unit`` gate (3.12, ``CAO_MUST_RUN=unit``) a missing tool fails the session instead of
skipping (``test/_must_run.py``). The portability suite is the one that keeps shipped files free of
one person's setup, so it must actually run in CI.

The gate cases at the bottom run this very file in a child pytest with the tools hidden, because a
gate that fails the session can only be observed from outside the session.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = REPO_ROOT / "examples" / "herdr-app-cao-window"
THIS_FILE = Path(__file__).resolve()
NODE = shutil.which("node")
JQ = shutil.which("jq")
SUITES = sorted(p for p in EXAMPLE.rglob("*.test.mjs") if "node_modules" not in p.parts)

# Every suite the example ships (25). SUITES comes from the folder, so deleting a suite file would just drop
# its case and the run would still pass; this list is what makes a deletion fail. A new suite is
# collected without being added here, but add it so that deleting it later is caught too.
REQUIRED_SUITES = (
    "install.test.mjs",
    "launcher.test.mjs",
    "lib/agents.test.mjs",
    "lib/cao.test.mjs",
    "lib/draft-followups.test.mjs",
    "lib/draft-view.test.mjs",
    "lib/draft.test.mjs",
    "lib/flows-followups.test.mjs",
    "lib/flows-form.test.mjs",
    "lib/flows.test.mjs",
    "lib/honesty.test.mjs",
    "lib/launch-followups.test.mjs",
    "lib/launch.test.mjs",
    "lib/logic.test.mjs",
    "lib/names.test.mjs",
    "lib/shell.test.mjs",
    "lib/store.test.mjs",
    "lib/workflows-followups.test.mjs",
    "lib/workflows-view.test.mjs",
    "lib/workflows.test.mjs",
    "make-window.test.mjs",
    "portability.test.mjs",
    "test/harness.test.mjs",
    "test/security-wiring.test.mjs",
    "test/shell-wiring.test.mjs",
)

# Seconds one suite may run. install.test.mjs runs the installer many times in scratch homes. It took
# 533 s and 540 s on the CI legs that passed and ran past 600 s on one that did not (two pytest-xdist
# workers share the runner) while it ran its cases one after another; it now spreads them over up to four
# processes and the installer starts far fewer programs, but a loaded runner can still be slow, so it keeps
# 1500 s. The others finish in seconds.
DEFAULT_SUITE_TIMEOUT_S = 300
SUITE_TIMEOUT_S = {"install.test.mjs": 1500}

# How many suites run at once when the cases are not spread over pytest-xdist workers (which already run
# them side by side). install.test.mjs is the long one; the rest finish while it runs.
MAX_PARALLEL_SUITES = 4

pytestmark = [
    pytest.mark.must_run("unit"),
    pytest.mark.skipif(NODE is None or JQ is None, reason="needs node and jq"),
]


def suite_timeout(suite: Path) -> int:
    """Seconds the suite may run before its case fails.

    Args:
        suite: Path of the ``*.test.mjs`` file.

    Returns:
        The suite's own limit, or the default one.
    """
    return SUITE_TIMEOUT_S.get(suite.name, DEFAULT_SUITE_TIMEOUT_S)


def run_node_suite(suite: Path) -> subprocess.CompletedProcess[str]:
    """Run one node suite from the example folder and capture its output.

    Args:
        suite: Path of the ``*.test.mjs`` file.

    Returns:
        The finished process; a non-zero return code means a failed case.
    """
    assert NODE is not None
    # A CAO_WINDOW_TEST_FILTER or CAO_WINDOW_TEST_SHARD left exported in a shell would make a suite run fewer
    # cases than it has.
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("CAO_WINDOW_TEST_FILTER", "CAO_WINDOW_TEST_SHARD")
    }
    return subprocess.run(
        [NODE, str(suite)],
        cwd=EXAMPLE,
        capture_output=True,
        text=True,
        timeout=suite_timeout(suite),
        env=env,
    )


class SuiteRuns:
    """Runs suites on a small thread pool, started as soon as the object is made.

    The suites are independent node processes, so running them side by side cuts the wall time of the
    file to that of its longest suite. The first of ``suites`` is started first: pass the long one first.

    Thread Safety:
        ``result`` may be called from any thread; each suite runs once.
    """

    def __init__(self, suites: list[Path], max_workers: int = MAX_PARALLEL_SUITES) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="node-suite")
        self._futures: dict[Path, Future[subprocess.CompletedProcess[str]]] = {
            suite: self._pool.submit(run_node_suite, suite) for suite in suites
        }
        self._pool.shutdown(wait=False)

    def result(self, suite: Path) -> subprocess.CompletedProcess[str]:
        """Wait for ``suite`` and return its finished process.

        Args:
            suite: A suite given to the constructor.

        Returns:
            The finished process.

        Raises:
            subprocess.TimeoutExpired: The suite ran past its limit.
        """
        return self._futures[suite].result()


def suites_to_run_together(items: list[Any]) -> list[Path]:
    """The suites among the collected ``items`` that ``test_node_suite`` will run, the longest first.

    Args:
        items: The items of the pytest session.

    Returns:
        The selected suites, ``install.test.mjs`` first.
    """
    picked = [
        item.callspec.params["suite"]
        for item in items
        if getattr(item, "originalname", None) == "test_node_suite" and hasattr(item, "callspec")
    ]
    return sorted(picked, key=lambda p: (suite_timeout(p) != max(SUITE_TIMEOUT_S.values()), str(p)))


@pytest.fixture(scope="session")
def suite_runs(request: pytest.FixtureRequest) -> SuiteRuns | None:
    """Every selected suite running side by side, or None when pytest-xdist already spreads the cases."""
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return None
    return SuiteRuns(suites_to_run_together(request.session.items))


def assert_suite_ran_and_passed(result: subprocess.CompletedProcess[str]) -> None:
    """Fail unless the suite exited 0 and printed no line that starts with ``SKIP``.

    A suite that cannot check something prints ``SKIP - <reason>``: the harness does so before it
    exits 0, and some cases print it and go on. Either way part of what the suite promises was not
    checked, so on a machine that is meant to run it that is a failure, not a pass.

    Args:
        result: The finished process from ``run_node_suite``.

    Raises:
        AssertionError: The suite failed, or skipped part of its checks.
    """
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-6000:]
    skipped = [line for line in output.splitlines() if line.startswith("SKIP")]
    assert not skipped, "the suite skipped part of its checks:\n" + "\n".join(skipped)


def run_this_file(env_overrides: dict[str, str], *extra: str) -> subprocess.CompletedProcess[str]:
    """Run this file in a child pytest, with a controlled environment.

    Args:
        env_overrides: Variables to set; ``PATH`` is always replaced by the caller's value.
        *extra: Further pytest arguments.

    Returns:
        The finished child process.
    """
    env = {
        "HOME": os.environ.get("HOME", "/"),
        "PYTHONDONTWRITEBYTECODE": "1",
        **env_overrides,
    }
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(THIS_FILE),
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-q",
            "--no-header",
            *extra,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


@pytest.mark.parametrize("suite", SUITES, ids=lambda p: str(p.relative_to(EXAMPLE)))
def test_node_suite(suite: Path, suite_runs: SuiteRuns | None) -> None:
    result = run_node_suite(suite) if suite_runs is None else suite_runs.result(suite)
    assert_suite_ran_and_passed(result)


@pytest.mark.parametrize("name", REQUIRED_SUITES)
def test_every_pinned_suite_is_among_the_suites(name: str) -> None:
    assert EXAMPLE / name in SUITES


def test_every_suite_of_lib_is_pinned() -> None:
    """Every suite of ``lib/`` and of ``test/`` (the harness, security and shell wiring suites) is pinned."""
    on_disk = {
        str(p.relative_to(EXAMPLE))
        for folder in ("lib", "test")
        for p in (EXAMPLE / folder).glob("*.test.mjs")
    }
    assert on_disk, "no suite found in lib/ or test/"
    assert on_disk <= set(REQUIRED_SUITES), sorted(on_disk - set(REQUIRED_SUITES))


def test_every_suite_of_the_example_is_pinned() -> None:
    on_disk = {str(p.relative_to(EXAMPLE)) for p in SUITES}
    assert on_disk == set(REQUIRED_SUITES), sorted(on_disk ^ set(REQUIRED_SUITES))


def test_the_pinned_list_has_no_repeats_and_the_suites_do_not_shrink_below_it() -> None:
    assert len(set(REQUIRED_SUITES)) == len(REQUIRED_SUITES)
    assert len(SUITES) >= len(REQUIRED_SUITES)


def test_suites_under_node_modules_are_not_collected() -> None:
    assert all("node_modules" not in p.parts for p in SUITES)


def test_a_failing_suite_fails_its_case_and_shows_the_failure(tmp_path: Path) -> None:
    failing = tmp_path / "failing.test.mjs"
    failing.write_text('console.log("FAIL - boom");\nprocess.exit(1);\n')
    result = run_node_suite(failing)
    assert result.returncode == 1
    assert "FAIL - boom" in result.stdout


def test_a_suite_that_prints_skip_and_exits_zero_fails_its_case(tmp_path: Path) -> None:
    skipping = tmp_path / "skipping.test.mjs"
    skipping.write_text('console.log("SKIP - cao-tab example absent; parity not checked");\n')
    result = run_node_suite(skipping)
    assert result.returncode == 0
    with pytest.raises(AssertionError, match="SKIP - cao-tab example absent"):
        assert_suite_ran_and_passed(result)


def test_a_skip_line_among_passing_cases_fails_the_suite(tmp_path: Path) -> None:
    partial = tmp_path / "partial.test.mjs"
    partial.write_text(
        'console.log("ok - alpha");\n'
        'console.log("SKIP - forwarded_env.py not found; env parity not checked");\n'
        'console.log("ok - beta");\n'
    )
    result = run_node_suite(partial)
    assert result.returncode == 0
    with pytest.raises(AssertionError, match="forwarded_env.py not found"):
        assert_suite_ran_and_passed(result)


def test_a_line_that_only_mentions_skip_is_not_a_skip(tmp_path: Path) -> None:
    mentions = tmp_path / "mentions.test.mjs"
    mentions.write_text(
        'console.log("ok - CLAUDE_CODE_SKIP_BEDROCK_AUTH is allowed");\n'
        'console.log("ok - a SKIP - inside a name");\n'
    )
    assert_suite_ran_and_passed(run_node_suite(mentions))


def test_a_failing_suite_fails_the_guard_with_its_output(tmp_path: Path) -> None:
    failing = tmp_path / "failing.test.mjs"
    failing.write_text('console.log("FAIL - boom");\nprocess.exit(1);\n')
    with pytest.raises(AssertionError, match="FAIL - boom"):
        assert_suite_ran_and_passed(run_node_suite(failing))


def test_suites_run_side_by_side_on_at_most_the_pool_size(tmp_path: Path) -> None:
    ticks = tmp_path / "ticks.log"
    probes = []
    for n in range(6):
        probe = tmp_path / f"probe{n}.test.mjs"
        probe.write_text(
            'import fs from "node:fs";\n'
            f"const log = {str(ticks)!r};\n"
            "fs.appendFileSync(log, `+ ${Date.now()}\\n`);\n"
            "await new Promise((r) => setTimeout(r, 400));\n"
            "fs.appendFileSync(log, `- ${Date.now()}\\n`);\n"
        )
        probes.append(probe)
    runs = SuiteRuns(probes, max_workers=3)
    assert all(runs.result(p).returncode == 0 for p in probes)
    events = sorted((int(line[2:]), line[0]) for line in ticks.read_text().splitlines())
    running = peak = 0
    for _, kind in events:
        running += 1 if kind == "+" else -1
        peak = max(peak, running)
    assert 2 <= peak <= 3, f"{peak} suites ran at once"


def test_the_long_suite_is_started_first_and_only_selected_suites_are_run_together() -> None:
    class Item:
        def __init__(self, name: str, suite: Path | None) -> None:
            self.originalname = name
            if suite is not None:
                self.callspec = type("Spec", (), {"params": {"suite": suite}})()

    install = EXAMPLE / "install.test.mjs"
    logic = EXAMPLE / "lib" / "logic.test.mjs"
    items = [
        Item("test_node_suite", logic),
        Item("test_every_pinned_suite_is_among_the_suites", None),
    ]
    items.append(Item("test_node_suite", install))
    assert suites_to_run_together(items) == [install, logic]
    assert suites_to_run_together(items[:2]) == [logic]


def test_install_gets_more_time_than_the_other_suites() -> None:
    assert suite_timeout(EXAMPLE / "install.test.mjs") == 1500
    assert suite_timeout(EXAMPLE / "lib" / "logic.test.mjs") == DEFAULT_SUITE_TIMEOUT_S


def test_every_suite_on_disk_is_collected_as_its_own_case() -> None:
    result = run_this_file({"PATH": os.environ["PATH"]}, "--collect-only")
    assert result.returncode == 0, result.stdout + result.stderr
    on_disk = {str(p.relative_to(EXAMPLE)) for p in EXAMPLE.rglob("*.test.mjs")}
    collected = {
        line.split("test_node_suite[", 1)[1].rstrip("]")
        for line in result.stdout.splitlines()
        if "test_node_suite[" in line
    }
    assert collected == on_disk


@pytest.fixture
def path_with_only_node(tmp_path: Path) -> str:
    """A PATH that holds node and nothing else, so ``jq`` is not found."""
    assert NODE is not None
    bin_dir = tmp_path / "only-node"
    bin_dir.mkdir()
    (bin_dir / "node").symlink_to(NODE)
    return str(bin_dir)


@pytest.fixture
def path_with_nothing(tmp_path: Path) -> str:
    """A PATH that names an empty folder, so neither ``node`` nor ``jq`` is found."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    return str(empty)


def test_missing_node_and_jq_fail_the_unit_leg_instead_of_skipping(path_with_nothing: str) -> None:
    result = run_this_file({"PATH": path_with_nothing, "CAO_MUST_RUN": "unit"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: VIOLATION" in result.stdout
    assert "SKIPPED (needs node and jq)" in result.stdout


def test_missing_jq_alone_fails_the_unit_leg(path_with_only_node: str) -> None:
    result = run_this_file({"PATH": path_with_only_node, "CAO_MUST_RUN": "unit"})
    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: VIOLATION" in result.stdout


def test_missing_tools_only_skip_when_the_unit_gate_is_not_enforced(path_with_nothing: str) -> None:
    result = run_this_file({"PATH": path_with_nothing})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "skipped" in result.stdout
    assert "VIOLATION" not in result.stdout


def test_another_legs_gate_does_not_enforce_this_files_skip(path_with_nothing: str) -> None:
    result = run_this_file({"PATH": path_with_nothing, "CAO_MUST_RUN": "agui-node"})
    assert "SKIPPED (needs node and jq)" not in result.stdout


def test_the_wrapper_hides_the_test_filter_from_the_suites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAO_WINDOW_TEST_FILTER", "zz-no-match")
    probe = tmp_path / "probe.test.mjs"
    probe.write_text('console.log("filter=" + (process.env.CAO_WINDOW_TEST_FILTER ?? "unset"));\n')
    result = run_node_suite(probe)
    assert "filter=unset" in result.stdout


def test_the_wrapper_hides_the_shard_from_the_suites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAO_WINDOW_TEST_SHARD", "1/2")
    probe = tmp_path / "probe.test.mjs"
    probe.write_text('console.log("shard=" + (process.env.CAO_WINDOW_TEST_SHARD ?? "unset"));\n')
    assert "shard=unset" in run_node_suite(probe).stdout


def test_the_wrapper_runs_all_cases_even_when_the_filter_is_exported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CAO_WINDOW_TEST_FILTER", "zz-no-match")
    harness = (EXAMPLE / "test" / "harness.mjs").as_uri()
    suite = tmp_path / "two-cases.test.mjs"
    suite.write_text(
        f'import {{ test, finish }} from "{harness}";\n'
        'test("alpha", () => {});\ntest("beta", () => {});\nawait finish();\n'
    )
    result = run_node_suite(suite)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ok - alpha" in result.stdout and "ok - beta" in result.stdout
    assert "filtered run" not in result.stdout
