"""Non-live coverage for supervisor-orchestration output predicates."""

from test.e2e.test_supervisor_orchestration import _final_report_matches

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
