"""Tests for ``scripts/mypy_ratchet.py`` and the Code Quality job that runs it.

Issue #1005. The defect these pin is a gate that reports success no matter what: before this
script, ``ci.yml``'s mypy step carried ``continue-on-error: true``, so 177 pre-existing type
errors and any number of newly introduced ones produced the same green check.

A ratchet is only worth having if it fails on the thing it exists to catch, so every test
here either feeds the comparator a regression and asserts it goes red, or feeds it the
machinery's own failure modes (mypy crashing, a missing baseline, unreadable output) and
asserts those are failures rather than passes. The workflow tests pin the absence of
``continue-on-error`` on the step, because that one key is the whole difference between a
gate and a decoration — and the contributing skill records that a mis-inserted
``continue-on-error`` has silently moved between steps in this repo before.

The comparator tests use synthetic mypy output rather than invoking mypy: they are asserting
the ratchet's arithmetic, which must hold regardless of what the repo's real error count is
on the day the test runs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "mypy_ratchet.py"
BASELINE = REPO_ROOT / "mypy-baseline.json"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("mypy_ratchet", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ratchet = _load_module()


def _mypy_output(*errors: str) -> str:
    """Build mypy-shaped output from ``path:line: error: msg  [code]`` fragments."""
    body = "\n".join(errors)
    count = len(errors)
    files = len({line.split(":", 1)[0] for line in errors})
    summary = (
        f"Found {count} errors in {files} files (checked 276 source files)"
        if count
        else "Success: no issues found in 276 source files"
    )
    return f"{body}\n{summary}\n" if body else f"{summary}\n"


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def test_parse_groups_by_file_and_error_code() -> None:
    output = _mypy_output(
        "src/a.py:1: error: Incompatible return value type  [return-value]",
        "src/a.py:9: error: Incompatible return value type  [return-value]",
        "src/a.py:3: error: Too many arguments  [call-arg]",
        "src/b.py:2: error: Returning Any  [no-any-return]",
    )
    assert ratchet.parse_errors(output) == {
        "src/a.py": {"return-value": 2, "call-arg": 1},
        "src/b.py": {"no-any-return": 1},
    }


def test_parse_ignores_notes_and_config_lines() -> None:
    """``note:`` lines are continuation detail, not separate diagnostics."""
    output = (
        "src/a.py:1: error: Returning Any  [no-any-return]\n"
        "src/a.py:1: note: Consider using --check-untyped-defs  [annotation-unchecked]\n"
        "mypy.ini: note: unused section(s): [mypy-foo]\n"
        "Found 1 error in 1 file (checked 1 source file)\n"
    )
    assert ratchet.parse_errors(output) == {"src/a.py": {"no-any-return": 1}}


def test_parse_handles_column_numbers_and_missing_error_code() -> None:
    output = (
        "src/a.py:4:11: error: Name 'x' is not defined  [name-defined]\n"
        "src/b.py:7: error: Cannot determine type\n"
    )
    assert ratchet.parse_errors(output) == {
        "src/a.py": {"name-defined": 1},
        "src/b.py": {ratchet._NO_CODE: 1},
    }


def test_parse_normalises_absolute_paths_under_the_repo_root() -> None:
    absolute = (REPO_ROOT / "src" / "a.py").as_posix()
    output = f"{absolute}:1: error: Returning Any  [no-any-return]\n"
    assert ratchet.parse_errors(output) == {"src/a.py": {"no-any-return": 1}}


# --------------------------------------------------------------------------------------
# Comparison
# --------------------------------------------------------------------------------------


def test_identical_counts_are_neither_regression_nor_improvement() -> None:
    counts = {"src/a.py": {"return-value": 3}}
    assert ratchet.compare(counts, dict(counts)) == ([], [])


def test_new_error_code_in_a_baselined_file_is_a_regression() -> None:
    regressions, improvements = ratchet.compare(
        {"src/a.py": {"return-value": 3}},
        {"src/a.py": {"return-value": 3, "call-arg": 1}},
    )
    assert improvements == []
    assert len(regressions) == 1
    assert "call-arg" in regressions[0] and "0 -> 1" in regressions[0]


def test_error_in_a_file_absent_from_the_baseline_is_a_regression() -> None:
    regressions, _ = ratchet.compare({}, {"src/new.py": {"arg-type": 1}})
    assert len(regressions) == 1
    assert "src/new.py" in regressions[0]


def test_falling_counts_are_improvements_and_not_failures() -> None:
    regressions, improvements = ratchet.compare(
        {"src/a.py": {"return-value": 3}}, {"src/a.py": {"return-value": 1}}
    )
    assert regressions == []
    assert improvements == ["src/a.py: [return-value] 3 -> 1 (-2)"]


def test_a_fully_fixed_file_is_an_improvement() -> None:
    regressions, improvements = ratchet.compare({"src/a.py": {"return-value": 2}}, {})
    assert regressions == []
    assert improvements == ["src/a.py: [return-value] 2 -> 0 (-2)"]


def test_net_zero_swap_across_files_is_still_a_regression() -> None:
    """The reason the baseline is per-file-per-code and not a single repo-wide total.

    One error added in ``b.py`` and one unrelated error removed in ``a.py`` leaves the total
    unchanged. A total-only gate passes this; the regression is real.
    """
    baseline = {"src/a.py": {"call-arg": 1}}
    current = {"src/b.py": {"arg-type": 1}}
    assert ratchet.total_errors(baseline) == ratchet.total_errors(current)

    regressions, improvements = ratchet.compare(baseline, current)
    assert len(regressions) == 1 and "src/b.py" in regressions[0]
    assert len(improvements) == 1 and "src/a.py" in improvements[0]


def test_net_zero_swap_of_codes_within_one_file_is_still_a_regression() -> None:
    regressions, _ = ratchet.compare(
        {"src/a.py": {"call-arg": 2}}, {"src/a.py": {"call-arg": 1, "arg-type": 1}}
    )
    assert len(regressions) == 1 and "arg-type" in regressions[0]


# --------------------------------------------------------------------------------------
# Baseline I/O
# --------------------------------------------------------------------------------------


def test_missing_baseline_raises_rather_than_reading_as_empty(tmp_path: Path) -> None:
    """Reading a missing baseline as empty would still fail -- every current error compares
    as a regression against nothing -- but as a confusing wall of per-file regressions
    instead of a clear "baseline not found" error."""
    with pytest.raises(ratchet.RatchetError, match="not found"):
        ratchet.load_baseline(tmp_path / "absent.json")


def test_unparseable_baseline_raises(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ratchet.RatchetError, match="not readable JSON"):
        ratchet.load_baseline(path)


def test_baseline_without_files_object_raises(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"total_errors": 7}), encoding="utf-8")
    with pytest.raises(ratchet.RatchetError, match="no 'files' object"):
        ratchet.load_baseline(path)


def test_baseline_with_non_integer_counts_raises(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    path.write_text(json.dumps({"files": {"src/a.py": {"call-arg": "lots"}}}), encoding="utf-8")
    with pytest.raises(ratchet.RatchetError, match="malformed"):
        ratchet.load_baseline(path)


def test_write_then_load_baseline_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "baseline.json"
    counts = {"src/a.py": {"call-arg": 2, "arg-type": 1}, "src/b.py": {"no-any-return": 3}}
    ratchet.write_baseline(path, counts, ["src/"])

    assert ratchet.load_baseline(path) == counts
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["total_errors"] == 6
    assert payload["targets"] == ["src/"]
    assert "mypy_version" in payload


# --------------------------------------------------------------------------------------
# main(): end-to-end with mypy stubbed out
# --------------------------------------------------------------------------------------


def _stub_mypy(
    monkeypatch: pytest.MonkeyPatch, output: str, exit_code: int, version: str = "mypy 2.4.0"
) -> None:
    monkeypatch.setattr(ratchet, "run_mypy", lambda targets: (output, exit_code))
    monkeypatch.setattr(ratchet, "mypy_version", lambda: version)


def _baseline_file(tmp_path: Path, counts: dict, version: str = "mypy 2.4.0") -> Path:
    path = tmp_path / "baseline.json"
    path.write_text(
        json.dumps(
            {
                "mypy_version": version,
                "targets": ["src/"],
                "total_errors": ratchet.total_errors(counts),
                "files": counts,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_main_passes_when_counts_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_mypy(monkeypatch, _mypy_output("src/a.py:1: error: Returning Any  [no-any-return]"), 1)
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}})

    assert ratchet.main(["--baseline", str(path), "src/"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_main_fails_on_a_new_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_mypy(
        monkeypatch,
        _mypy_output(
            "src/a.py:1: error: Returning Any  [no-any-return]",
            "src/a.py:5: error: Too many arguments  [call-arg]",
        ),
        1,
    )
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}})

    assert ratchet.main(["--baseline", str(path), "src/"]) == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "call-arg" in out


def test_main_passes_and_suggests_update_when_errors_fall(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_mypy(monkeypatch, _mypy_output(), 0)
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}})

    assert ratchet.main(["--baseline", str(path), "src/"]) == 0
    out = capsys.readouterr().out
    assert "PASS" in out and "--update" in out


def test_main_echoes_raw_mypy_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate must not hide the diagnostics it is gating on."""
    _stub_mypy(monkeypatch, _mypy_output("src/a.py:1: error: Returning Any  [no-any-return]"), 1)
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}})

    ratchet.main(["--baseline", str(path), "src/"])
    assert "src/a.py:1: error: Returning Any" in capsys.readouterr().out


def test_main_fails_when_mypy_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """mypy exit 2 is an internal error or bad config -- not 'no errors found'."""
    _stub_mypy(monkeypatch, "mypy: error: Cannot find config file\n", 2)
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}})

    assert ratchet.main(["--baseline", str(path), "src/"]) == 1
    assert "exited 2" in capsys.readouterr().out


def test_main_fails_when_output_cannot_be_parsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 1 with zero parsed errors means the output format moved under us."""
    _stub_mypy(monkeypatch, "something went sideways\n", 1)
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}})

    assert ratchet.main(["--baseline", str(path), "src/"]) == 1
    assert "no 'error:' lines were parsed" in capsys.readouterr().out


def test_main_fails_when_the_baseline_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_mypy(monkeypatch, _mypy_output(), 0)

    assert ratchet.main(["--baseline", str(tmp_path / "absent.json"), "src/"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_main_warns_on_a_mypy_version_mismatch_but_still_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_mypy(
        monkeypatch,
        _mypy_output("src/a.py:1: error: Returning Any  [no-any-return]"),
        1,
        version="mypy 2.9.9",
    )
    path = _baseline_file(tmp_path, {"src/a.py": {"no-any-return": 1}}, version="mypy 2.4.0")

    assert ratchet.main(["--baseline", str(path), "src/"]) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out and "2.9.9" in out and "2.4.0" in out


def test_main_update_writes_the_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_mypy(
        monkeypatch,
        _mypy_output(
            "src/a.py:1: error: Returning Any  [no-any-return]",
            "src/b.py:2: error: Too many arguments  [call-arg]",
        ),
        1,
    )
    path = tmp_path / "baseline.json"

    assert ratchet.main(["--update", "--baseline", str(path), "src/"]) == 0
    assert ratchet.load_baseline(path) == {
        "src/a.py": {"no-any-return": 1},
        "src/b.py": {"call-arg": 1},
    }


# --------------------------------------------------------------------------------------
# The committed baseline and the workflow wiring
# --------------------------------------------------------------------------------------


def test_committed_baseline_is_loadable_and_self_consistent() -> None:
    counts = ratchet.load_baseline(BASELINE)
    payload = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert payload["total_errors"] == ratchet.total_errors(counts)


def _code_quality_steps() -> List[Tuple[int, str]]:
    """Return ``(line_number, text)`` for the Code Quality job's lines in ci.yml."""
    lines = CI_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "name: Code Quality")
    # The job ends at the next top-level job key (two-space indent, trailing colon).
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i]
        if (
            stripped
            and not stripped.startswith("   ")
            and stripped.startswith("  ")
            and stripped.rstrip().endswith(":")
        ):
            end = i
            break
    return [(i + 1, lines[i]) for i in range(start, end)]


def test_ci_code_quality_runs_the_ratchet() -> None:
    body = "\n".join(text for _, text in _code_quality_steps())
    assert "scripts/mypy_ratchet.py" in body, "Code Quality must invoke the ratchet"


def test_ci_mypy_step_is_blocking() -> None:
    """The one key that separates a gate from a decoration (issue #1005).

    Comment lines are excluded deliberately: the step carries a comment *naming*
    ``continue-on-error`` to explain why it is absent, and that prose must not be mistaken
    for the key itself.
    """
    offenders = [
        (line_no, text)
        for line_no, text in _code_quality_steps()
        if text.lstrip().startswith("continue-on-error")
        or text.lstrip().startswith("- continue-on-error")
    ]
    assert not offenders, f"Code Quality must not tolerate failures: {offenders}"


def test_contributing_documents_how_to_move_the_baseline() -> None:
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "mypy_ratchet.py" in text
    assert "--update" in text
