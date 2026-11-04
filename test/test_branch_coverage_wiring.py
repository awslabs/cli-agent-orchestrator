"""Guards for branch-coverage collection and its separate floor (issue #1005).

A line floor cannot see a condition that is only ever taken one way -- which is
how an unreachable capability branch stays invisible (#814). Branch data is
therefore collected everywhere coverage is collected, and floored as its OWN
metric.

Two things are easy to get wrong and silent when wrong:

- Dropping ``--cov-branch`` from one of the CI invocations, so the ratchet's
  branch metric quietly reports "not measured" forever.
- Reading ``totals.percent_covered`` as line coverage. With ``--cov-branch`` on,
  coverage.py redefines that field as blended line+branch; on a measured subset
  it moved 20.19% -> 16.52% from the flag alone. The existing 87.0 line floor
  would then be enforcing a metric nobody chose.
"""

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
BASELINE = REPO_ROOT / ".coverage-baseline.json"
RATCHET = REPO_ROOT / "cao_mcp_apps/scripts/coverage-ratchet.mjs"


def _addopts() -> str:
    for line in PYPROJECT.read_text().splitlines():
        if line.startswith("addopts"):
            return line
    pytest.fail("pyproject.toml has no addopts line")


def _ci_coverage_invocations() -> list[str]:
    """Each multi-line ``uv run pytest ... --cov=...`` block in ci.yml."""
    text = CI_WORKFLOW.read_text()
    blocks = re.findall(r"uv run pytest(?:[^\n]*\\\n)*[^\n]*", text)
    return [b for b in blocks if "--cov=src/cli_agent_orchestrator" in b]


class TestBranchCoverageIsCollected:
    def test_pyproject_addopts_requests_branch_data(self):
        assert "--cov-branch" in _addopts(), (
            "local runs collect line coverage only, so a half-taken branch is "
            "invisible in term-missing until CI"
        )

    def test_both_ci_invocations_request_branch_data(self):
        invocations = _ci_coverage_invocations()
        assert len(invocations) == 2, (
            "expected exactly the two package-coverage pytest invocations in ci.yml "
            f"(build-and-test and the ratchet feed); found {len(invocations)} -- "
            "update this guard along with the workflow"
        )
        missing = [i.splitlines()[0] for i in invocations if "--cov-branch" not in i]
        assert not missing, f"these CI coverage runs collect no branch data: {missing}"


class TestFloorsStaySeparate:
    def test_baseline_declares_a_branch_metric(self):
        baseline = json.loads(BASELINE.read_text())
        assert "python_branch" in baseline, "branch coverage has no ratchet entry"
        assert baseline["python_branch"].get("coverage_json"), "branch metric names no report"
        assert baseline["python"]["floor_pct"] == 87.0, (
            "the python LINE floor moved; branch coverage must be floored "
            "separately rather than by disturbing this number"
        )

    def test_ratchet_computes_line_pct_rather_than_reading_percent_covered(self):
        source = RATCHET.read_text()
        assert "covered_lines" in source and "num_statements" in source, (
            "the ratchet no longer computes line coverage from covered_lines/"
            "num_statements -- under --cov-branch it is enforcing a blended metric"
        )
        # Comments are stripped: the script explains the trap by naming the field,
        # and a guard that cannot tell prose from code would forbid saying so.
        code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("//"))
        python_metrics = code.split("function frontendLinePct")[0]
        assert "percent_covered" not in python_metrics, (
            "the python line metric reads totals.percent_covered, which --cov-branch "
            "redefines as blended line+branch"
        )
        assert "covered_branches" in source, "the ratchet reports no branch metric"
