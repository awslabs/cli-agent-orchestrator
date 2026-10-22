"""Tests for the ``must_run`` assurance gate itself (``test/_must_run.py``).

Two halves:

* behaviour of the gate, exercised by running a real pytest session in a
  throwaway directory. A gate that fails the *session* can only be observed
  from the outside, so these use a subprocess rather than ``pytester``.
* the wiring guard: every ``must_run`` gate name used anywhere in ``test/``
  must be enforced by at least one workflow step's ``CAO_MUST_RUN``. Without
  this, a gate no job enforces would be precisely the dead guard the whole
  mechanism exists to prevent.
"""

import re
import subprocess
import sys
from pathlib import Path
from test._must_run import ENV_VAR, MARKER, active_gates, marker_gates
from textwrap import dedent

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
PLUGIN_PATH = REPO_ROOT / "test" / "_must_run.py"


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_active_gates_parses_a_comma_separated_list():
    assert active_gates({ENV_VAR: "unit, gitleaks ,"}) == {"unit", "gitleaks"}


def test_active_gates_is_empty_when_unset_or_blank():
    assert active_gates({}) == set()
    assert active_gates({ENV_VAR: "   "}) == set()


def test_marker_gates_reads_the_marker_arguments():
    mark = getattr(pytest.mark, MARKER)("unit", "gitleaks")
    assert marker_gates(mark.mark) == ("unit", "gitleaks")
    assert marker_gates(None) == ()


# ---------------------------------------------------------------------------
# End-to-end: a real pytest session
# ---------------------------------------------------------------------------


def _scratch_session(tmp_path: Path, test_body: str) -> None:
    """Lay out a self-contained pytest project that loads the real gate."""
    # A byte-identical copy of the real plugin, loaded as a plugin by name so
    # pytest can rewrite its assertions (importing it first would warn).
    (tmp_path / "_must_run_under_test.py").write_text(PLUGIN_PATH.read_text())
    (tmp_path / "conftest.py").write_text('pytest_plugins = ("_must_run_under_test",)\n')
    (tmp_path / "test_scratch.py").write_text(dedent(test_body))


def _run(tmp_path: Path, *extra: str, gates: str | None = None) -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        # Keep the scratch session off this repo's addopts/markers config.
        "PYTHONDONTWRITEBYTECODE": "1",
        # The scratch dir holds the plugin copy conftest.py loads by name.
        "PYTHONPATH": str(tmp_path),
    }
    if gates is not None:
        env[ENV_VAR] = gates
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(tmp_path),
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            "-o",
            f"markers={MARKER}(gate): assurance gate",
            "-q",
            *extra,
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=180,
    )


SKIPPING_TEST = """
    import pytest

    @pytest.mark.must_run("unit")
    @pytest.mark.skipif(True, reason="precondition vanished")
    def test_assurance():
        assert True
"""

PASSING_TEST = """
    import pytest

    @pytest.mark.must_run("unit")
    def test_assurance():
        assert True
"""


def test_skipped_must_run_test_fails_the_session_when_its_gate_is_enforced(tmp_path):
    _scratch_session(tmp_path, SKIPPING_TEST)
    result = _run(tmp_path, gates="unit")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: VIOLATION" in result.stdout
    assert "SKIPPED (precondition vanished)" in result.stdout
    assert "test_assurance" in result.stdout


def test_skipped_must_run_test_is_tolerated_when_its_gate_is_not_enforced(tmp_path):
    """The pip-only job must stay green; only the owning job enforces."""
    _scratch_session(tmp_path, SKIPPING_TEST)
    result = _run(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "VIOLATION" not in result.stdout


TWO_GATE_TEST = """
    import pytest

    @pytest.mark.must_run("unit")
    @pytest.mark.skipif(True, reason="precondition vanished")
    def test_assurance():
        assert True

    @pytest.mark.must_run("gitleaks")
    def test_other():
        assert True
"""


def test_a_different_gate_does_not_enforce_this_one(tmp_path):
    """A skip under an un-enforced gate must not fail a run that owns another gate."""
    _scratch_session(tmp_path, TWO_GATE_TEST)
    result = _run(tmp_path, gates="gitleaks")

    assert result.returncode == 0, result.stdout + result.stderr


def test_deselected_must_run_test_fails_the_session(tmp_path):
    """A `-k`/`-m` filter must not be able to quietly drop an owned gate."""
    _scratch_session(tmp_path, PASSING_TEST)
    result = _run(tmp_path, "-k", "nothing_matches_this", gates="unit")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "DESELECTED" in result.stdout


def test_clean_run_passes_and_says_so(tmp_path):
    _scratch_session(tmp_path, PASSING_TEST)
    result = _run(tmp_path, gates="unit")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "must_run: 1 test(s) for gate(s) ['unit'] ran as required" in result.stdout


def test_enforced_gate_with_no_owned_test_fails_the_session(tmp_path):
    """Delete/rename/mistype the marked test and the gate must not go quiet."""
    _scratch_session(tmp_path, PASSING_TEST)
    result = _run(tmp_path, gates="unit,ghost")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: VIOLATION" in result.stdout
    assert "gate 'ghost'" in result.stdout
    assert "no must_run test was collected" in result.stdout


CALL_PHASE_SKIP_TEST = """
    import pytest

    @pytest.mark.must_run("unit")
    def test_assurance():
        pytest.skip("precondition vanished at runtime")
"""


def test_call_phase_skip_fails_the_session_when_its_gate_is_enforced(tmp_path):
    """A ``pytest.skip()`` inside the test body, not just a ``skipif`` decorator."""
    _scratch_session(tmp_path, CALL_PHASE_SKIP_TEST)
    result = _run(tmp_path, gates="unit")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: VIOLATION" in result.stdout
    assert "SKIPPED (precondition vanished at runtime)" in result.stdout


XFAIL_TEST = """
    import pytest

    @pytest.mark.must_run("unit")
    @pytest.mark.xfail(reason="known broken", strict=False)
    def test_assurance():
        assert False
"""


def test_xfail_is_not_reported_as_a_must_run_violation(tmp_path):
    """xfail is a known-failure marker, not the silent-skip hazard this gate targets."""
    _scratch_session(tmp_path, XFAIL_TEST)
    result = _run(tmp_path, gates="unit")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "VIOLATION" not in result.stdout


@pytest.mark.parametrize("gates", [None, "unit"], ids=["unset", "unit-enforced"])
def test_marker_without_a_gate_name_is_reported_as_misuse(tmp_path, gates):
    """A bare marker fails the session regardless of CAO_MUST_RUN, including unset."""
    _scratch_session(
        tmp_path,
        """
        import pytest

        @pytest.mark.must_run
        def test_assurance():
            assert True
        """,
    )
    result = _run(tmp_path, gates=gates)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: MISUSE" in result.stdout
    assert "with no gate name" in result.stdout


def test_gate_fails_closed_under_pytest_xdist(tmp_path):
    """Under -n, collection runs on the xdist workers, not this controller plugin
    instance -- it must fail closed rather than report "0 test(s) ... ran as
    required" and exit 0 for a gate it never actually checked."""
    _scratch_session(tmp_path, SKIPPING_TEST)
    result = _run(tmp_path, "-p", "xdist", "-n", "2", gates="unit")

    assert result.returncode != 0, result.stdout + result.stderr
    assert "must_run: VIOLATION" in result.stdout
    assert "pytest-xdist" in result.stdout
    assert "ran as required" not in result.stdout


# ---------------------------------------------------------------------------
# Wiring guard: every gate must have an owner
# ---------------------------------------------------------------------------


def _strip_line_comments(text: str) -> str:
    """Drop everything from a bare ``#`` to end of line.

    Both the files this is applied to — ``test/*.py`` markers and
    ``.github/workflows/*.yml`` env lines — never need a literal ``#`` inside
    a ``must_run`` gate name or a ``CAO_MUST_RUN`` value, so a commented-out
    marker or env line must not be mistaken for a live one.
    """
    return re.sub(r"#.*", "", text)


def _gates_used_in_the_suite() -> set[str]:
    pattern = re.compile(rf"{MARKER}\(\s*((?:\"[^\"]+\"\s*,?\s*)+)\)")
    gates: set[str] = set()
    for path in sorted((REPO_ROOT / "test").rglob("*.py")):
        if path.name in {"_must_run.py", Path(__file__).name}:
            continue
        text = _strip_line_comments(path.read_text())
        for match in pattern.finditer(text):
            gates.update(re.findall(r"\"([^\"]+)\"", match.group(1)))
    return gates


def _gates_enforced_by_workflows() -> set[str]:
    gates: set[str] = set()
    for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
        text = _strip_line_comments(path.read_text())
        for value in re.findall(rf"{ENV_VAR}:\s*(.+)", text):
            # Tolerate the matrix-conditional expression form in ci.yml by
            # harvesting every quoted literal in the value.
            gates.update(token for token in re.findall(r"'([a-z0-9_-]+)'", value))
            bare = value.strip().strip("\"'")
            if re.fullmatch(r"[a-z0-9_,\- ]+", bare):
                gates.update(t.strip() for t in bare.split(",") if t.strip())
    return gates


def test_strip_line_comments_hides_a_commented_out_marker():
    text = '# @pytest.mark.must_run("unit")\n'
    pattern = re.compile(rf"{MARKER}\(\s*((?:\"[^\"]+\"\s*,?\s*)+)\)")
    assert pattern.search(_strip_line_comments(text)) is None
    # Sanity check: without stripping, the regex does find it — this is the
    # exact gap a commented-out marker would otherwise exploit.
    assert pattern.search(text) is not None


def test_strip_line_comments_hides_a_commented_out_env_line():
    text = f"      # {ENV_VAR}: gitleaks\n"
    assert re.search(rf"{ENV_VAR}:\s*(.+)", _strip_line_comments(text)) is None
    assert re.search(rf"{ENV_VAR}:\s*(.+)", text) is not None


def test_every_must_run_gate_is_enforced_by_a_workflow():
    used = _gates_used_in_the_suite()
    assert used, "no must_run gates found — did the markers move?"

    enforced = _gates_enforced_by_workflows()
    orphans = used - enforced
    assert not orphans, (
        f"must_run gate(s) {sorted(orphans)} are declared on tests but no "
        f".github/workflows step sets {ENV_VAR} to them, so nothing enforces "
        "them. Add the gate to the owning job's env, or drop the marker."
    )


def test_the_known_gates_are_owned_by_the_expected_jobs():
    """Pin the gate-to-job mapping so a silent move is visible in the diff."""
    ci = (WORKFLOWS_DIR / "ci.yml").read_text()
    secret_scan = (WORKFLOWS_DIR / "secret-scan.yml").read_text()

    assert f"{ENV_VAR}: ${{{{ matrix.python-version == '3.12' && 'unit' || '' }}}}" in ci
    assert f"{ENV_VAR}: gitleaks" in secret_scan
