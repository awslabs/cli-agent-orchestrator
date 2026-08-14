"""Non-live coverage for supervisor-orchestration output predicates."""

from test.e2e.test_supervisor_orchestration import (
    _final_report_matches,
    _has_post_input_output,
)

import pytest


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("## Summary\nData A, B, and C\n## Conclusions", True),
        ("\x1b[32mFinal report\x1b[0m\nRecommendations", True),
        ("Dataset A callback delivered", False),
        ("## Summary\nDataset A, B, and C", False),
    ],
)
def test_final_report_matches_requires_report_and_synthesis_markers(
    output: str, expected: bool
) -> None:
    matched, _ = _final_report_matches(output)

    assert matched is expected


@pytest.mark.parametrize(
    ("output_before", "current_output", "expected"),
    [
        ("completed report", "completed report", False),
        ("completed report", "", False),
        ("completed report", "completed report\nFinal report", True),
    ],
)
def test_has_post_input_output_rejects_stale_snapshot(
    output_before: str, current_output: str, expected: bool
) -> None:
    assert _has_post_input_output(output_before, current_output) is expected
