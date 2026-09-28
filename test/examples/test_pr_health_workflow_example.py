"""Tests for the deterministic PR-health workflow example.

The example lives under ``examples/`` and is loaded by path, so it is not
covered by ``--cov=src``. These tests are therefore the only coverage the
scoring engine, the marker lifecycle, and the GitHub-commenting enforcement
branches get; the workflow itself carries no in-product assert bundle.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cli_agent_orchestrator.services.flow_service import _parse_flow_file
from cli_agent_orchestrator.services.script_lint import lint_script

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_DIR = REPO_ROOT / "examples" / "workflows" / "pr-health"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def workflow() -> ModuleType:
    return _load_module("pr_health_example", EXAMPLE_DIR / "pr_health.py")


@pytest.fixture(scope="module")
def guard() -> ModuleType:
    return _load_module(
        "pr_health_biweekly_guard",
        EXAMPLE_DIR / "pr_health_biweekly_guard.py",
    )


@pytest.fixture
def base_pr() -> dict[str, Any]:
    """A maximally healthy PR: every component at full points, score 100."""
    return {
        "number": 1,
        "title": "feat: deterministic workflow",
        "url": "https://example.invalid/pull/1",
        "author": {"login": "owner"},
        "isDraft": False,
        "body": (
            "This change fixes #1 because the workflow needs deterministic scoring. "
            "Testing and verification cover every score boundary. " * 3
        ),
        "createdAt": "2026-07-01T00:00:00Z",
        "additions": 100,
        "deletions": 20,
        "changedFiles": 4,
        "files": [{"path": "test/test_pr_health.py"}],
        "labels": [{"name": "feature"}],
        "comments": [],
        "commits": [{"committedDate": "2026-07-31T00:00:00Z"}],
        "reviewDecision": "APPROVED",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
        "closingIssuesReferences": [{"number": 1}],
    }


def _marker(stage: str, score: int, as_of: str) -> str:
    """A legacy v1 marker. Every v1 marker must migrate to notification epoch 0."""
    return f"<!-- cao-pr-health:v1 stage={stage} score={score} as_of={as_of} -->"


def _marker_v2(epoch: int, stage: str, score: int, as_of: str) -> str:
    return (
        f"<!-- cao-pr-health:v2 epoch={epoch} stage={stage} "
        f"score={score} as_of={as_of} -->"
    )


def _authored_comment(body: str, created_at: str) -> dict[str, Any]:
    return {
        "author": {"login": "maintainer"},
        "createdAt": created_at,
        "body": body,
        "viewerDidAuthor": True,
    }


def _owner_comment(created_at: str, body: str = "I will follow up.") -> dict[str, Any]:
    return {
        "author": {"login": "owner"},
        "createdAt": created_at,
        "body": body,
        "viewerDidAuthor": False,
    }


def _streak_state(as_of: str, score: int = 12) -> dict[str, Any]:
    """Persisted state that has already satisfied the two-observation rule."""
    return {
        "last_as_of": as_of,
        "last_score": score,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 2,
    }


@pytest.fixture
def at_risk_pr(base_pr: dict[str, Any]) -> dict[str, Any]:
    """Score 56: passing CI, blocked merge, changes requested, 17 idle days."""
    return {
        **base_pr,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeStateStatus": "BLOCKED",
        "commits": [{"committedDate": "2026-07-14T00:00:00Z"}],
    }


@pytest.fixture
def warned_pr(at_risk_pr: dict[str, Any]) -> dict[str, Any]:
    """Score 44 with a 7-day-old warning marker: draft-eligible."""
    return {
        **at_risk_pr,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        "commits": [{"committedDate": "2026-07-23T00:00:00Z"}],
        "comments": [
            _authored_comment(
                _marker("warning", 44, "2026-07-24"), "2026-07-24T00:00:00Z"
            )
        ],
    }


@pytest.fixture
def stale_pr(base_pr: dict[str, Any]) -> dict[str, Any]:
    """Raw score 12, owner-blocked, and permanently idle. Never recovers."""
    return {
        **base_pr,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        "commits": [{"committedDate": "2026-01-01T00:00:00Z"}],
        "comments": [],
        "labels": [],
        "closingIssuesReferences": [],
        "body": "short",
        "files": [],
    }


@pytest.fixture
def protected_stale_pr(stale_pr: dict[str, Any]) -> dict[str, Any]:
    """The same permanently stale PR, but P0 — so it escalates, never closes."""
    return {**stale_pr, "labels": [{"name": "security"}]}


def _simulate_cadence(
    workflow: ModuleType,
    pr: dict[str, Any],
    start: str,
    runs: int,
    cadence_days: int = 14,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    """Drive the real 14-day cadence, appending markers exactly as apply mode does.

    Returns the action per run, the per-run score results, and the bodies of
    every comment the workflow would actually have posted.
    """
    ordinal = workflow._date_ordinal(start)
    state: dict[str, Any] | None = None
    actions: list[str] = []
    results: list[dict[str, Any]] = []
    posted: list[str] = []
    for index in range(runs):
        as_of = _iso_from_ordinal(workflow, ordinal + index * cadence_days)
        result, state = workflow._score_pr(pr, as_of, state)
        actions.append(result["recommended_action"])
        results.append(result)
        if result["recommended_action"] in workflow.ACTIONABLE_RECOMMENDATIONS:
            body = workflow._comment_body(result, as_of)
            posted.append(body)
            pr = {
                **pr,
                "comments": [
                    *pr["comments"],
                    _authored_comment(body, f"{as_of}T00:00:00Z"),
                ],
            }
    return actions, results, posted


def _iso_from_ordinal(workflow: ModuleType, ordinal: int) -> str:
    """Invert ``_date_ordinal`` by search: the module exposes no inverse."""
    base = date(2026, 1, 1)
    offset = workflow._date_ordinal("2026-01-01") - base.toordinal()
    return date.fromordinal(ordinal - offset).isoformat()


# --------------------------------------------------------------------------
# Static validation and wiring
# --------------------------------------------------------------------------


def test_workflow_passes_static_validation() -> None:
    path = EXAMPLE_DIR / "pr_health.py"
    result = lint_script(path.read_text(encoding="utf-8"), str(path))

    assert result.status == "pass"
    assert result.findings == []


def test_repo_storage_key_is_unambiguous(workflow: ModuleType) -> None:
    assert workflow._repo_storage_key("a--b/c") != workflow._repo_storage_key("a/b--c")


# --------------------------------------------------------------------------
# Calendar engine
# --------------------------------------------------------------------------


def test_days_between_crosses_leap_and_non_leap_february(workflow: ModuleType) -> None:
    assert workflow._days_between("2024-02-28", "2024-03-01") == 2
    assert workflow._days_between("2025-02-28", "2025-03-01") == 1


@pytest.mark.parametrize(
    ("earlier", "later", "expected"),
    [
        ("2026-01-31", "2026-02-01", 1),
        ("2026-12-31", "2027-01-01", 1),
        ("2026-01-01", "2027-01-01", 365),
        ("2024-01-01", "2025-01-01", 366),
        ("2024-02-29", "2024-03-01", 1),
        ("2026-07-31", "2026-07-31", 0),
        ("2026-08-01", "2026-07-31", 0),  # clamped, never negative
        ("1900-02-28", "1900-03-01", 1),  # 1900 is not a leap year
        ("2000-02-28", "2000-03-01", 2),  # 2000 is a leap year
    ],
)
def test_days_between_boundaries(
    workflow: ModuleType,
    earlier: str,
    later: str,
    expected: int,
) -> None:
    assert workflow._days_between(earlier, later) == expected


def test_date_ordinal_agrees_with_stdlib_across_a_dense_range(
    workflow: ModuleType,
) -> None:
    """The bespoke ordinal must match ``date.toordinal`` offsets exactly."""
    samples = [
        date(year, month, day)
        for year in (1900, 1999, 2000, 2024, 2025, 2026, 2100)
        for month in range(1, 13)
        for day in (1, 15, 28)
    ]
    for value in samples:
        delta = workflow._date_ordinal(value.isoformat()) - value.toordinal()
        assert (
            delta == workflow._date_ordinal("2026-01-01") - date(2026, 1, 1).toordinal()
        )


@pytest.mark.parametrize(
    "value",
    [
        "2026-02-30",
        "2025-02-29",  # not a leap year
        "2026-13-01",
        "2026-00-10",
        "2026-01-00",
        "2026-01-32",
        "0000-01-01",
        "2026-7-31",
        "2026/07/31",
        "2026-07-31T00:00:00Z",
        "",
        "not-a-date",
    ],
)
def test_validate_as_of_rejects_invalid_dates(workflow: ModuleType, value: str) -> None:
    with pytest.raises(ValueError):
        workflow._validate_as_of(value)


def test_validate_as_of_accepts_a_leap_day(workflow: ModuleType) -> None:
    workflow._validate_as_of("2024-02-29")


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_engagement_bands(workflow: ModuleType) -> None:
    days = (3, 4, 7, 8, 14, 15, 21, 22, 30, 31)
    assert [workflow._engagement_component(day) for day in days] == [
        40,
        32,
        32,
        24,
        24,
        16,
        16,
        8,
        8,
        0,
    ]


def test_category_bands(workflow: ModuleType) -> None:
    scores = (100, 85, 84, 70, 69, 60, 59, 51, 50, 30, 29)
    assert [workflow._category(score) for score in scores] == [
        "healthy",
        "healthy",
        "active",
        "active",
        "watch",
        "watch",
        "at_risk",
        "at_risk",
        "stalled",
        "stalled",
        "abandoned",
    ]


def test_healthy_pr_scores_100_and_recommends_nothing(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    healthy, _ = workflow._score_pr(base_pr, "2026-07-31", None)

    assert healthy["score"] == 100
    assert healthy["category"] == "healthy"
    assert healthy["next_actor"] == "MAINTAINER"
    assert healthy["recommended_action"] == "none"


def test_first_below_60_observation_only_observes(
    workflow: ModuleType,
    at_risk_pr: dict[str, Any],
) -> None:
    at_risk, _ = workflow._score_pr(at_risk_pr, "2026-07-31", None)

    assert at_risk["score"] == 56
    assert at_risk["recommended_action"] == "observe_again"
    assert at_risk["below60_owner_streak"] == 1


def test_second_below_60_observation_warns_the_owner(
    workflow: ModuleType,
    at_risk_pr: dict[str, Any],
) -> None:
    _, state = workflow._score_pr(at_risk_pr, "2026-07-31", None)
    warning, _ = workflow._score_pr(at_risk_pr, "2026-08-01", state)

    assert warning["below60_owner_streak"] == 2
    assert warning["recommended_action"] == "warn_owner"


def test_unanswered_warning_after_seven_days_proposes_draft(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
) -> None:
    stalled, _ = workflow._score_pr(warned_pr, "2026-07-31", None)

    assert stalled["score"] == 44
    assert stalled["recommended_action"] == "propose_draft"


def test_ignored_draft_after_fourteen_days_proposes_close(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    abandoned_pr = {
        **base_pr,
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [
            _authored_comment(
                _marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z"
            )
        ],
    }

    abandoned, _ = workflow._score_pr(abandoned_pr, "2026-07-31", None)

    assert abandoned["raw_score"] == 50
    assert abandoned["score"] == 25
    assert abandoned["category"] == "abandoned"
    assert abandoned["recommended_action"] == "propose_close"


def test_protected_pr_escalates_instead_of_closing(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    protected_pr = {
        **base_pr,
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [
            _authored_comment(
                _marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z"
            )
        ],
        "title": "fix(security): prevent command injection",
        "labels": [{"name": "security"}],
    }

    protected, _ = workflow._score_pr(protected_pr, "2026-07-31", None)

    assert protected["priority"] == "P0"
    assert protected["score"] == 25
    assert protected["recommended_action"] == "escalate_protected_pr"


def test_forged_marker_from_another_author_is_ignored(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
) -> None:
    forged = {
        **warned_pr,
        "comments": [
            {
                "author": {"login": "owner"},
                "createdAt": "2026-07-01T00:00:00Z",
                "body": _marker("warning", 44, "2026-07-24"),
                "viewerDidAuthor": False,
            }
        ],
    }

    result, _ = workflow._score_pr(forged, "2026-07-31", None)

    assert result["lifecycle_marker"] is None
    assert result["recommended_action"] == "observe_again"


# --------------------------------------------------------------------------
# Observation streak: stale persisted state must not abort the run
# --------------------------------------------------------------------------


def test_streak_restarts_instead_of_raising_on_stale_state(
    workflow: ModuleType,
) -> None:
    """A backfill / reopened PR / clock skew must not kill the whole run."""
    previous = {
        "last_as_of": "2026-08-15",
        "last_score": 40,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 3,
    }

    assert workflow._observation_streak(previous, "2026-07-31", 40, "OWNER") == 1


def test_streak_restarts_on_unparsable_persisted_date(workflow: ModuleType) -> None:
    previous = {"last_as_of": "not-a-date", "below60_owner_streak": 9}

    assert workflow._observation_streak(previous, "2026-07-31", 40, "OWNER") == 1


def test_stale_state_for_one_pr_does_not_stop_scoring_others(
    workflow: ModuleType,
    at_risk_pr: dict[str, Any],
) -> None:
    stale = {
        "last_as_of": "2026-12-01",
        "last_score": 40,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 5,
    }

    first, _ = workflow._score_pr(at_risk_pr, "2026-07-31", stale)
    second, _ = workflow._score_pr({**at_risk_pr, "number": 2}, "2026-07-31", None)

    assert first["below60_owner_streak"] == 1
    assert second["below60_owner_streak"] == 1


def test_same_day_rerun_preserves_the_streak(workflow: ModuleType) -> None:
    previous = {
        "last_as_of": "2026-07-31",
        "last_score": 40,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 2,
    }

    assert workflow._observation_streak(previous, "2026-07-31", 40, "OWNER") == 2


def test_streak_resets_when_the_pr_recovered_in_between(workflow: ModuleType) -> None:
    previous = {
        "last_as_of": "2026-07-24",
        "last_score": 90,
        "last_next_actor": "MAINTAINER",
        "below60_owner_streak": 0,
    }

    assert workflow._observation_streak(previous, "2026-07-31", 40, "OWNER") == 1


def test_zero_last_score_preserves_the_below_60_streak(workflow: ModuleType) -> None:
    previous = {
        "last_as_of": "2026-07-24",
        "last_score": 0,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 2,
    }

    assert workflow._observation_streak(previous, "2026-07-31", 20, "OWNER") == 3


# --------------------------------------------------------------------------
# Lifecycle marker selection: progression must not depend on run cadence
# --------------------------------------------------------------------------


def test_draft_marker_wins_over_a_later_warning_marker(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    """A later warning comment must not shadow an existing draft marker.

    Selecting the newest marker instead would restart the draft grace period on
    every run, so the closure branch would never be re-evaluated.
    """
    pr = {
        **base_pr,
        "comments": [
            _authored_comment(
                _marker("draft_recommendation", 50, "2026-07-01"),
                "2026-07-01T00:00:00Z",
            ),
            _authored_comment(
                _marker("warning", 45, "2026-07-20"), "2026-07-20T00:00:00Z"
            ),
        ],
    }

    marker = workflow._lifecycle_marker(pr)

    assert marker is not None
    assert marker["stage"] == "draft_recommendation"
    assert marker["created_at"] == "2026-07-01T00:00:00Z"


def test_latest_marker_of_the_winning_stage_owns_the_grace_period(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    """Within one epoch, exact (epoch, stage) dedup means at most one marker.

    A duplicate can still exist (a hand-posted marker, or a pre-dedup run), and
    the least-noise direction is to anchor on the newest one: it lengthens the
    grace period rather than shortening it.
    """
    pr = {
        **base_pr,
        "comments": [
            _authored_comment(
                _marker("warning", 50, "2026-07-01"), "2026-07-01T00:00:00Z"
            ),
            _authored_comment(
                _marker("warning", 45, "2026-07-20"), "2026-07-20T00:00:00Z"
            ),
        ],
    }

    marker = workflow._lifecycle_marker(pr)

    assert marker is not None
    assert marker["created_at"] == "2026-07-20T00:00:00Z"


def test_policy_constants_match_the_approved_values(workflow: ModuleType) -> None:
    """The lifecycle is a fixed policy; drift here silently changes noise volume."""
    assert workflow.MARKER_SCHEMA_VERSION == 2
    assert workflow.WARNING_GRACE_DAYS == 7
    assert workflow.DRAFT_GRACE_DAYS == 14
    assert workflow.OWNER_RESPONSE_GRACE_DAYS == 30
    assert workflow.EPOCH_REENTRY_COOLDOWN_DAYS == 90
    assert workflow.MAX_NOTIFICATION_EPOCHS == 4
    assert workflow.MAX_ESCALATION_NOTIFICATIONS == 3
    assert workflow.TERMINAL_STAGES == frozenset(
        {"closure_recommendation", "escalation"}
    )


def test_v2_marker_carries_epoch_and_stage(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _authored_comment(
                _marker_v2(2, "draft_recommendation", 31, "2026-07-01"),
                "2026-07-01T00:00:00Z",
            )
        ]
    }

    lifecycle = workflow._lifecycle(pr)

    assert lifecycle["current_epoch"] == 2
    assert lifecycle["current"]["stage"] == "draft_recommendation"
    assert lifecycle["current"]["epoch"] == 2
    assert lifecycle["epoch_count"] == 1


@pytest.mark.parametrize(
    ("body", "stage"),
    [
        (_marker("warning", 44, "2026-07-01"), "warning"),
        (_marker("draft", 44, "2026-07-01"), "draft_recommendation"),
        (_marker("closed", 44, "2026-07-01"), "closure_recommendation"),
        (
            "<!-- cao-pr-health:v1 action=escalation score=44 as_of=2026-07-01 -->",
            "escalation",
        ),
    ],
)
def test_legacy_v1_markers_migrate_to_epoch_zero(
    workflow: ModuleType,
    body: str,
    stage: str,
) -> None:
    """A PR mid-ladder under v1 must keep its place, not restart in a new epoch."""
    pr = {"comments": [_authored_comment(body, "2026-07-01T00:00:00Z")]}

    lifecycle = workflow._lifecycle(pr)

    assert lifecycle["current_epoch"] == 0
    assert lifecycle["current"]["epoch"] == 0
    assert lifecycle["current"]["stage"] == stage
    assert lifecycle["current"]["version"] == 1


def test_current_epoch_is_the_highest_epoch_then_highest_stage(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    """An older epoch's terminal marker must not shadow a newer epoch's warning."""
    pr = {
        **base_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "closure_recommendation", 10, "2026-01-01"),
                "2026-01-01T00:00:00Z",
            ),
            _authored_comment(
                _marker_v2(1, "warning", 20, "2026-06-01"),
                "2026-06-01T00:00:00Z",
            ),
            _authored_comment(
                _marker_v2(1, "draft_recommendation", 20, "2026-06-15"),
                "2026-06-15T00:00:00Z",
            ),
        ],
    }

    lifecycle = workflow._lifecycle(pr)

    assert lifecycle["current_epoch"] == 1
    assert lifecycle["current"]["stage"] == "draft_recommendation"
    assert lifecycle["epoch_count"] == 2


def test_terminal_stage_closes_the_epoch_until_the_cooldown_expires(
    workflow: ModuleType,
    stale_pr: dict[str, Any],
) -> None:
    """A closure recommendation ends the epoch; only the cooldown reopens one."""
    pr = {
        **stale_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "closure_recommendation", 0, "2026-07-01"),
                "2026-07-01T00:00:00Z",
            )
        ],
    }

    day_89, _ = workflow._score_pr(pr, "2026-09-28", _streak_state("2026-09-14"))
    day_90, _ = workflow._score_pr(pr, "2026-09-29", _streak_state("2026-09-15"))

    assert day_89["recommended_action"] == "await_owner_deadline"
    assert "awaiting_epoch_reentry_cooldown" in day_89["action_reasons"]
    assert day_90["recommended_action"] == "warn_owner"
    assert day_90["notification_epoch"] == 1


def test_epoch_reentry_restarts_at_warning_never_mid_ladder(
    workflow: ModuleType,
    protected_stale_pr: dict[str, Any],
) -> None:
    """Re-entry after an escalation must warn again, not escalate again."""
    pr = {
        **protected_stale_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 12, "2026-06-17"),
                "2026-06-17T00:00:00Z",
            ),
            _authored_comment(
                _marker_v2(0, "escalation", 12, "2026-07-01"),
                "2026-07-01T00:00:00Z",
            ),
        ],
    }

    result, _ = workflow._score_pr(pr, "2026-09-29", _streak_state("2026-09-15"))

    assert result["recommended_action"] == "warn_owner"
    assert result["notification_epoch"] == 1


def test_owner_response_grace_is_thirty_days(
    workflow: ModuleType,
    stale_pr: dict[str, Any],
) -> None:
    pr = {
        **stale_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 12, "2026-07-01"),
                "2026-07-01T00:00:00Z",
            ),
            _owner_comment("2026-07-02T00:00:00Z"),
        ],
    }

    day_29, _ = workflow._score_pr(pr, "2026-07-31", _streak_state("2026-07-17"))
    day_30, _ = workflow._score_pr(pr, "2026-08-01", _streak_state("2026-07-18"))

    assert day_29["recommended_action"] == "monitor_response"
    assert "owner_responded" in day_29["action_reasons"]
    assert day_30["recommended_action"] != "monitor_response"
    assert "owner_response_epoch_expired" in day_30["action_reasons"]


def test_expired_owner_response_does_not_escalate_immediately(
    workflow: ModuleType,
    protected_stale_pr: dict[str, Any],
) -> None:
    """An answered-then-abandoned PR waits out the cooldown; it never jumps a stage."""
    pr = {
        **protected_stale_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 12, "2026-07-01"),
                "2026-07-01T00:00:00Z",
            ),
            _owner_comment("2026-07-02T00:00:00Z"),
        ],
    }

    expired, _ = workflow._score_pr(pr, "2026-08-15", _streak_state("2026-08-01"))
    after_cooldown, _ = workflow._score_pr(
        pr, "2026-09-30", _streak_state("2026-09-16")
    )

    assert expired["recommended_action"] == "await_owner_deadline"
    assert "awaiting_epoch_reentry_cooldown" in expired["action_reasons"]
    # 90 days after the response anchor (2026-07-02), not after the marker.
    assert after_cooldown["recommended_action"] == "warn_owner"
    assert after_cooldown["notification_epoch"] == 1


def test_mid_score_pr_advances_past_warning_without_a_dead_zone(
    workflow: ModuleType,
    at_risk_pr: dict[str, Any],
) -> None:
    """Score 56 sits above the old ``<=50`` gate: it must still advance, not stall.

    The removed defect: a warned PR scoring 51-59 matched no advancement branch
    and fell back onto the score ladder, which re-recommended ``warn_owner`` —
    a stage it had already passed.
    """
    pr = {
        **at_risk_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 56, "2026-07-17"),
                "2026-07-17T00:00:00Z",
            )
        ],
    }

    result, _ = workflow._score_pr(
        pr, "2026-07-31", _streak_state("2026-07-17", score=56)
    )

    assert result["raw_score"] == 56
    assert result["recommended_action"] == "propose_draft"
    assert result["notification_epoch"] == 0


def test_no_action_after_a_marker_ever_walks_the_ladder_backwards(
    workflow: ModuleType,
    at_risk_pr: dict[str, Any],
) -> None:
    """Every marker-present branch must return; none may fall back to the ladder."""
    for stage in (
        "warning",
        "draft_recommendation",
        "closure_recommendation",
        "escalation",
    ):
        for marker_as_of in ("2026-07-30", "2026-07-17", "2026-05-01", "2026-01-01"):
            pr = {
                **at_risk_pr,
                "comments": [
                    _authored_comment(
                        _marker_v2(0, stage, 56, marker_as_of),
                        f"{marker_as_of}T00:00:00Z",
                    )
                ],
            }
            result, _ = workflow._score_pr(
                pr, "2026-07-31", _streak_state("2026-07-17", score=56)
            )
            action = result["recommended_action"]
            if stage == "warning":
                assert action != "warn_owner", (stage, marker_as_of, action)
            else:
                assert action not in {"warn_owner", "propose_draft"}, (
                    stage,
                    marker_as_of,
                    action,
                )


def test_stale_penalty_only_applies_while_insufficiently_healthy(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    """A recovered PR with an old draft marker must not be penalized into 'stalled'."""
    pr = {
        **base_pr,
        "comments": [
            _authored_comment(
                _marker_v2(0, "draft_recommendation", 40, "2026-08-01"),
                "2026-08-01T00:00:00Z",
            )
        ],
    }

    result, _ = workflow._score_pr(pr, "2026-08-05", None)

    assert result["raw_score"] == 92
    assert result["score"] == 92
    assert result["components"]["unanswered_draft_recommendation"] == 0
    assert result["category"] == "healthy"
    assert result["recommended_action"] == "none"


def test_persisted_score_is_the_raw_score_not_the_penalized_score(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    """Persisting the penalized score double-counts the penalty on the next run."""
    pr = {
        **base_pr,
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [
            _authored_comment(
                _marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z"
            )
        ],
    }

    result, state = workflow._score_pr(pr, "2026-07-31", None)

    assert result["raw_score"] == 50
    assert result["score"] == 25
    assert state["last_score"] == 50


# --------------------------------------------------------------------------
# Long-horizon anti-spam invariants at the real 14-day cadence
# --------------------------------------------------------------------------


def test_biweekly_cadence_over_three_years_is_bounded_by_the_epoch_cap(
    workflow: ModuleType,
    stale_pr: dict[str, Any],
) -> None:
    """A permanently stale PR must receive a fixed, small number of comments.

    78 fortnightly runs span 1092 days. Without an epoch cap this PR would be
    re-warned indefinitely; the cap is what makes the ceiling a constant.
    """
    actions, results, posted = _simulate_cadence(workflow, stale_pr, "2026-01-05", 78)

    assert len(posted) == workflow.MAX_NOTIFICATION_EPOCHS * 3 == 12
    assert actions.count("warn_owner") == 4
    assert actions.count("propose_draft") == 4
    assert actions.count("propose_close") == 4
    assert "report_only" in actions
    assert max(result["notification_epoch"] for result in results) == 3
    first_cap = actions.index("report_only")
    assert not (set(actions[first_cap:]) & workflow.ACTIONABLE_RECOMMENDATIONS)
    assert "notification_epoch_cap_reached" in results[-1]["action_reasons"]


def test_escalation_notifications_are_capped_over_three_years(
    workflow: ModuleType,
    protected_stale_pr: dict[str, Any],
) -> None:
    """A protected PR escalates instead of closing, at most three times ever."""
    actions, results, posted = _simulate_cadence(
        workflow, protected_stale_pr, "2026-01-05", 78
    )

    assert (
        actions.count("escalate_protected_pr")
        == workflow.MAX_ESCALATION_NOTIFICATIONS
        == 3
    )
    assert actions.count("propose_close") == 0
    assert actions.count("warn_owner") == 4
    assert len(posted) == 7
    assert "report_only" in actions
    assert "escalation_cap_reached" in results[-1]["action_reasons"]


def test_the_ladder_never_regresses_across_a_year_of_runs(
    workflow: ModuleType,
    stale_pr: dict[str, Any],
) -> None:
    """(epoch, stage rank) must be monotonically non-decreasing, run over run."""
    _, results, _ = _simulate_cadence(workflow, stale_pr, "2026-01-05", 27)

    progress = [
        (
            result["lifecycle_marker"]["epoch"],
            workflow.STAGE_RANK[result["lifecycle_marker"]["stage"]],
        )
        if result["lifecycle_marker"]
        else (0, -1)
        for result in results
    ]
    assert progress == sorted(progress)
    assert progress[-1][0] >= 2  # at least three epochs inside one year


def test_a_recovered_pr_stops_the_ladder_and_never_re_enters(
    workflow: ModuleType,
    stale_pr: dict[str, Any],
    base_pr: dict[str, Any],
) -> None:
    actions, _, posted = _simulate_cadence(workflow, stale_pr, "2026-01-05", 3)
    assert actions[-1] == "propose_draft"

    recovered = {
        **base_pr,
        "comments": [_authored_comment(posted[0], "2026-01-19T00:00:00Z")],
        "commits": [{"committedDate": "2026-02-01T00:00:00Z"}],
    }
    later, _, later_posted = _simulate_cadence(workflow, recovered, "2026-02-02", 26)

    assert later_posted == []
    assert set(later) <= {"none", "monitor_response"}
    assert "none" in set(later)


# --------------------------------------------------------------------------
# Grace periods, owner response, and closed-epoch behaviour
# --------------------------------------------------------------------------


def test_warned_pr_holds_instead_of_re_warning_before_the_deadline(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
) -> None:
    """Inside the warning grace period the ladder holds, it does not repeat."""
    pr = {
        **warned_pr,
        "comments": [
            _authored_comment(
                _marker("warning", 44, "2026-07-28"), "2026-07-28T00:00:00Z"
            )
        ],
    }
    state = {
        "last_as_of": "2026-07-28",
        "last_score": 44,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 2,
    }

    result, _ = workflow._score_pr(pr, "2026-07-31", state)

    assert result["recommended_action"] == "await_owner_deadline"
    assert "awaiting_warning_grace_period" in result["action_reasons"]


def test_owner_response_after_a_marker_switches_to_monitoring(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
) -> None:
    pr = {
        **warned_pr,
        "commits": [{"committedDate": "2026-07-30T00:00:00Z"}],
    }

    result, _ = workflow._score_pr(pr, "2026-07-31", None)

    assert result["recommended_action"] == "monitor_response"
    assert "owner_responded" in result["action_reasons"]


@pytest.mark.parametrize("closure_stage", ["closure_recommendation", "closed"])
@pytest.mark.parametrize("activity_kind", ["reply", "commit"])
def test_owner_activity_after_closure_reopens_only_after_the_cooldown(
    workflow: ModuleType,
    base_pr: dict[str, Any],
    closure_stage: str,
    activity_kind: str,
) -> None:
    """One response settles the epoch; the next ladder starts 90 days later at warning."""
    comments = [
        _authored_comment(
            _marker(closure_stage, 20, "2026-07-01"),
            "2026-07-01T00:00:00Z",
        )
    ]
    commits = [{"committedDate": "2026-06-01T00:00:00Z"}]
    if activity_kind == "reply":
        comments.append(_owner_comment("2026-07-02T00:00:00Z"))
    else:
        commits = [{"committedDate": "2026-07-02T00:00:00Z"}]

    pr: dict[str, Any] = {
        **base_pr,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeable": "CONFLICTING",
        "mergeStateStatus": "DIRTY",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        "commits": commits,
        "comments": comments,
        "labels": [],
        "closingIssuesReferences": [],
        "body": "short",
        "files": [],
    }
    observed = []
    for as_of in ("2026-07-03", "2026-08-01", "2026-09-29", "2026-09-30"):
        result, _ = workflow._score_pr(pr, as_of, _streak_state("2026-07-03"))
        observed.append((result["recommended_action"], result["notification_epoch"]))

    assert observed == [
        ("monitor_response", 0),
        ("await_owner_deadline", 0),
        ("await_owner_deadline", 0),
        ("warn_owner", 1),
    ]


# --------------------------------------------------------------------------
# Comment bodies and markers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "stage"),
    [
        ("warn_owner", "warning"),
        ("propose_draft", "draft_recommendation"),
        ("second_owner_notification", "draft_recommendation"),
        ("propose_close", "closure_recommendation"),
        ("escalate_protected_pr", "escalation"),
    ],
)
def test_comment_body_embeds_the_v2_stage_marker(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
    action: str,
    stage: str,
) -> None:
    item, _ = workflow._score_pr(warned_pr, "2026-07-31", None)
    item = {**item, "recommended_action": action, "notification_epoch": 2}

    body = workflow._comment_body(item, "2026-07-31")

    assert body.startswith("## Automated PR-health notification")
    assert body.count(workflow.COMMENT_ONLY_DISCLAIMER) == 1
    assert "@owner " in body
    assert _marker_v2(2, stage, item["score"], "2026-07-31") in body
    # Every emitted marker must be parseable by the reader that consumes it.
    parsed = workflow.MARKER_V2_RE.search(body)
    assert parsed is not None
    assert parsed.group(1) == "2"
    assert parsed.group(2) == stage


def test_comment_body_refuses_to_render_a_report_only_decision(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
) -> None:
    item, _ = workflow._score_pr(warned_pr, "2026-07-31", None)
    item = {**item, "recommended_action": "report_only"}

    with pytest.raises(ValueError):
        workflow._comment_body(item, "2026-07-31")


def test_escalation_comment_names_the_protection_and_is_parseable(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    protected_pr = {
        **base_pr,
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [
            _authored_comment(
                _marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z"
            )
        ],
        "title": "fix(security): prevent command injection",
        "labels": [{"name": "security"}],
    }
    protected, _ = workflow._score_pr(protected_pr, "2026-07-31", None)

    body = workflow._comment_body(protected, "2026-07-31")

    assert body.startswith("## Automated PR-health notification")
    assert body.count(workflow.COMMENT_ONLY_DISCLAIMER) == 1
    assert "@owner " in body
    assert "priority P0" in body
    assert _marker_v2(0, "escalation", 25, "2026-07-31") in body
    assert workflow.MARKER_V2_RE.search(body) is not None


def test_action_marker_rejects_a_non_enforcement_action(workflow: ModuleType) -> None:
    with pytest.raises(ValueError):
        workflow._action_marker("observe_again", 0, 40, "2026-07-31")


# --------------------------------------------------------------------------
# Cross-run idempotency: dedupe on (epoch, stage), never on score/as_of/date
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "stage"),
    [
        ("warn_owner", "warning"),
        ("propose_draft", "draft_recommendation"),
        ("second_owner_notification", "draft_recommendation"),
        ("propose_close", "closure_recommendation"),
        ("escalate_protected_pr", "escalation"),
    ],
)
def test_marker_from_a_prior_run_is_recognized_despite_different_score_and_date(
    workflow: ModuleType,
    action: str,
    stage: str,
) -> None:
    """The defect this guards: exact-text dedupe re-posts the same notice.

    The prior marker carries a different score and as_of than this run would
    emit, so an exact-string comparison would miss it.
    """
    pr = {
        "comments": [
            _authored_comment(
                _marker_v2(1, stage, 33, "2026-01-01"), "2026-01-01T00:00:00Z"
            )
        ]
    }

    assert workflow._has_marker_for_action(pr, action, 1) is True


def test_marker_dedup_is_exact_per_epoch_and_never_expires_by_date(
    workflow: ModuleType,
) -> None:
    """Date-based dedupe expiry is what re-posted a stage inside its own epoch."""
    pr = {
        "comments": [
            _authored_comment(
                _marker_v2(0, "closure_recommendation", 20, "2026-01-01"),
                "2026-01-01T00:00:00Z",
            )
        ]
    }

    assert workflow._has_marker_for_action(pr, "propose_close", 0) is True
    # Ten years later the epoch-0 closure stage is still spent.
    assert workflow._has_marker_for_action(pr, "propose_close", 0) is True
    # A different epoch is a different notification: not deduped.
    assert workflow._has_marker_for_action(pr, "propose_close", 1) is False


def test_legacy_escalation_marker_dedupes_epoch_zero(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _authored_comment(
                "<!-- cao-pr-health:v1 action=escalation score=12 as_of=2026-01-01 -->",
                "2026-01-01T00:00:00Z",
            )
        ]
    }

    assert workflow._has_marker_for_action(pr, "escalate_protected_pr", 0) is True
    assert workflow._has_marker_for_action(pr, "escalate_protected_pr", 1) is False


def test_legacy_v1_stage_marker_dedupes_epoch_zero(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _authored_comment(
                _marker("warning", 44, "2026-07-24"), "2026-07-24T00:00:00Z"
            )
        ]
    }

    assert workflow._has_marker_for_action(pr, "warn_owner", 0) is True
    assert workflow._has_marker_for_action(pr, "warn_owner", 1) is False


def test_marker_for_a_different_stage_does_not_dedupe(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 44, "2026-07-24"), "2026-07-24T00:00:00Z"
            )
        ]
    }

    assert workflow._has_marker_for_action(pr, "warn_owner", 0) is True
    assert workflow._has_marker_for_action(pr, "propose_draft", 0) is False
    assert workflow._has_marker_for_action(pr, "escalate_protected_pr", 0) is False


def test_marker_authored_by_someone_else_does_not_dedupe(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _owner_comment("2026-07-24T00:00:00Z", _marker_v2(0, "warning", 44, "x"))
        ]
    }

    assert workflow._has_marker_for_action(pr, "warn_owner", 0) is False


def test_has_marker_for_action_rejects_unknown_actions(workflow: ModuleType) -> None:
    with pytest.raises(ValueError):
        workflow._has_marker_for_action({"comments": []}, "observe_again", 0)


# --------------------------------------------------------------------------
# Enforcement branches: every path that can mutate GitHub
# --------------------------------------------------------------------------


def _plan(number: int, action: str, score: int = 40, epoch: int = 0) -> dict[str, Any]:
    return {
        "number": number,
        "score": score,
        "recommended_action": action,
        "notification_epoch": epoch,
    }


@pytest.fixture
def enforcement(workflow: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run ``_apply_recommendations`` against a stubbed gh surface."""
    commands: list[list[str]] = []

    def _run(
        plans: list[dict[str, Any]],
        live_pr: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        monkeypatch.setattr(
            workflow,
            "_fetch_pr",
            lambda _repo, _number: dict(live_pr or {"state": "OPEN", "comments": []}),
        )
        monkeypatch.setattr(
            workflow, "_run_gh_command", lambda args: commands.append(args)
        )
        return workflow._apply_recommendations(
            "owner/repo",
            "2026-07-31",
            plans,
            state or {"prs": {}},
            tmp_path / "enforcement.json",
        )

    return _run, commands


def test_enforcement_skips_non_open_pr_before_mutation(enforcement) -> None:
    run, commands = enforcement

    results = run([_plan(7, "warn_owner")], live_pr={"state": "CLOSED", "comments": []})

    assert results == [
        {
            "number": 7,
            "planned_action": "warn_owner",
            "notification_epoch": 0,
            "status": "skipped_not_open",
        }
    ]
    assert commands == []


def test_enforcement_is_idempotent_across_runs(enforcement) -> None:
    """A prior run's marker (different score/date) must suppress the comment."""
    run, commands = enforcement
    live = {
        "state": "OPEN",
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 12, "2026-07-25"), "2026-07-25T00:00:00Z"
            )
        ],
    }

    results = run([_plan(7, "warn_owner")], live_pr=live)

    assert results[0]["status"] == "already_applied"
    assert commands == []


def test_enforcement_skips_on_live_drift(
    workflow: ModuleType,
    enforcement,
    warned_pr: dict[str, Any],
) -> None:
    run, commands = enforcement
    live = {**warned_pr, "state": "OPEN"}

    # The plan claims a score the live PR does not reproduce.
    results = run([_plan(1, "propose_draft", score=99)], live_pr=live)

    assert results[0]["status"] == "skipped_live_drift"
    assert results[0]["live_score"] == 44
    assert commands == []


def test_enforcement_close_recommendation_only_comments(
    workflow: ModuleType,
    enforcement,
    base_pr: dict[str, Any],
) -> None:
    run, commands = enforcement
    abandoned_pr = {
        **base_pr,
        "state": "OPEN",
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [
            _authored_comment(
                _marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z"
            )
        ],
    }
    planned, _ = workflow._score_pr(abandoned_pr, "2026-07-31", None)

    results = run([planned], live_pr=abandoned_pr)

    assert results[0]["status"] == "commented"
    assert len(commands) == 1
    assert commands[0][:5] == ["pr", "comment", "1", "--repo", "owner/repo"]
    assert _marker_v2(0, "closure_recommendation", 25, "2026-07-31") in commands[0][-1]
    assert "being closed" not in commands[0][-1]
    assert "consider closing" in commands[0][-1]


def test_enforcement_draft_recommendation_only_comments(
    workflow: ModuleType,
    enforcement,
    warned_pr: dict[str, Any],
) -> None:
    run, commands = enforcement
    live = {**warned_pr, "state": "OPEN"}
    planned, _ = workflow._score_pr(live, "2026-07-31", None)
    assert planned["recommended_action"] == "propose_draft"

    results = run([planned], live_pr=live)

    assert results[0]["status"] == "commented"
    assert len(commands) == 1
    assert commands[0][:5] == ["pr", "comment", "1", "--repo", "owner/repo"]
    assert _marker_v2(0, "draft_recommendation", 44, "2026-07-31") in commands[0][-1]
    assert "being moved to draft" not in commands[0][-1]
    assert "consider moving this PR to draft" in commands[0][-1]


def test_already_draft_pr_gets_a_second_notification_not_a_draft_call(
    workflow: ModuleType,
    enforcement,
    warned_pr: dict[str, Any],
) -> None:
    """A draft PR takes the second-notification path; it is never re-drafted."""
    run, commands = enforcement
    live = {**warned_pr, "state": "OPEN", "isDraft": True}
    planned, _ = workflow._score_pr(live, "2026-07-31", None)
    assert planned["recommended_action"] == "second_owner_notification"

    results = run([planned], live_pr=live)

    assert results[0]["status"] == "commented"
    assert [command[:2] for command in commands] == [["pr", "comment"]]
    assert (
        _marker_v2(0, "draft_recommendation", planned["score"], "2026-07-31")
        in commands[0][-1]
    )


def test_enforcement_comments_for_a_warning(
    workflow: ModuleType,
    enforcement,
    at_risk_pr: dict[str, Any],
) -> None:
    run, commands = enforcement
    live = {**at_risk_pr, "state": "OPEN"}
    state = {
        "prs": {
            "1": {
                "last_as_of": "2026-07-30",
                "last_score": 56,
                "last_next_actor": "OWNER",
                "below60_owner_streak": 1,
            }
        }
    }
    planned, _ = workflow._score_pr(live, "2026-07-31", state["prs"]["1"])
    assert planned["recommended_action"] == "warn_owner"

    results = run([planned], live_pr=live, state=state)

    assert results[0]["status"] == "commented"
    assert commands[0][:5] == ["pr", "comment", "1", "--repo", "owner/repo"]
    assert _marker_v2(0, "warning", 56, "2026-07-31") in commands[0][-1]


def test_apply_replay_never_posts_a_second_comment(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    at_risk_pr: dict[str, Any],
) -> None:
    """Resume re-executes the script top-to-bottom; the marker is the only guard.

    The live PR here accumulates the comment the first pass posts, exactly as
    GitHub would, so the replayed pass sees its own prior marker.
    """
    live: dict[str, Any] = {**at_risk_pr, "state": "OPEN"}
    previous = {
        "last_as_of": "2026-07-30",
        "last_score": 56,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 1,
    }
    planned, _ = workflow._score_pr(live, "2026-07-31", previous)
    assert planned["recommended_action"] == "warn_owner"

    commands: list[list[str]] = []

    def _comment(args: list[str]) -> str:
        commands.append(args)
        live["comments"] = [
            *live["comments"],
            _authored_comment(args[-1], "2026-07-31T00:00:00Z"),
        ]
        return "posted"

    monkeypatch.setattr(workflow, "_fetch_pr", lambda _repo, _number: dict(live))
    monkeypatch.setattr(workflow, "_run_gh_command", _comment)

    statuses = []
    for _ in range(3):
        results = workflow._apply_recommendations(
            "owner/repo",
            "2026-07-31",
            [planned],
            {"prs": {"1": previous}},
            tmp_path / "enforcement.json",
        )
        statuses.append(results[0]["status"])

    assert statuses == ["commented", "already_applied", "already_applied"]
    assert len(commands) == 1


def test_apply_ignores_report_only_decisions_at_the_cap(
    workflow: ModuleType,
    enforcement,
) -> None:
    run, commands = enforcement

    results = run([_plan(7, "report_only"), _plan(8, "await_owner_deadline")])

    assert results == []
    assert commands == []
    assert "report_only" not in workflow.ACTIONABLE_RECOMMENDATIONS


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "ready", "1", "--repo", "owner/repo", "--undo"],
        ["pr", "close", "1", "--repo", "owner/repo"],
        ["pr", "edit", "1", "--repo", "owner/repo", "--add-label", "stale"],
        ["pr", "review", "1", "--repo", "owner/repo", "--approve"],
        ["api", "repos/owner/repo/statuses/abc", "--method", "POST"],
        ["api", "repos/owner/repo/check-runs", "--method", "POST"],
    ],
)
def test_github_write_boundary_rejects_every_non_comment_command(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("rejected commands must not reach subprocess")

    monkeypatch.setattr(workflow.subprocess, "run", _must_not_run)

    with pytest.raises(ValueError, match="comment-only"):
        workflow._run_gh_command(args)


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "comment"],
        ["pr", "comment", "0", "--repo", "owner/repo", "--body", "message"],
        ["pr", "comment", "١", "--repo", "owner/repo", "--body", "message"],
        ["pr", "comment", "1", "--body", "message"],
        ["pr", "comment", "1", "--repo", "owner/repo"],
        [
            "pr",
            "comment",
            "1",
            "--repo",
            "owner/repo",
            "--body",
            "message",
            "--edit-last",
        ],
    ],
)
def test_github_write_boundary_requires_explicit_repo_and_pr_scope(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("unscoped comments must not reach subprocess")

    monkeypatch.setattr(workflow.subprocess, "run", _must_not_run)

    with pytest.raises(ValueError, match="explicit repository and PR number"):
        workflow._run_gh_command(args)


def test_github_write_boundary_runs_one_explicitly_scoped_comment(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _completed(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        return workflow.subprocess.CompletedProcess(
            args, 0, stdout="commented\n", stderr=""
        )

    monkeypatch.setattr(workflow.subprocess, "run", _completed)

    output = workflow._run_gh_command(
        ["pr", "comment", "17", "--repo", "owner/repo", "--body", "message"]
    )

    assert output == "commented"
    assert calls == [
        ["gh", "pr", "comment", "17", "--repo", "owner/repo", "--body", "message"]
    ]


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "ready", "1", "--repo", "owner/repo", "--undo"],
        ["pr", "close", "1", "--repo", "owner/repo"],
        ["pr", "edit", "1", "--repo", "owner/repo", "--add-label", "stale"],
    ],
)
def test_github_read_boundary_rejects_write_shaped_commands_before_launch(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("rejected commands must not reach subprocess")

    monkeypatch.setattr(workflow.subprocess, "run", _must_not_run)

    with pytest.raises(ValueError, match="read boundary"):
        workflow._run_gh(args)


@pytest.mark.parametrize(
    "args",
    [
        ["pr", "view", "١", "--repo", "owner/repo", "--json", "number"],
        ["pr", "view", "1", "--repo", "owner/repo", "--json", "number", "--comments"],
        [
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "all",
            "--limit",
            "10",
            "--json",
            "number",
        ],
        [
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--limit",
            "2002",
            "--json",
            "number",
        ],
    ],
)
def test_github_read_boundary_rejects_unsafe_shapes_before_launch(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
) -> None:
    def _must_not_run(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("rejected commands must not reach subprocess")

    monkeypatch.setattr(workflow.subprocess, "run", _must_not_run)

    with pytest.raises(ValueError, match="read boundary"):
        workflow._run_gh(args)


def test_github_read_boundary_runs_only_expected_list_and_view_shapes(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def _completed(args: list[str], **_kwargs: Any) -> Any:
        calls.append(args)
        return workflow.subprocess.CompletedProcess(args, 0, stdout="[]\n", stderr="")

    monkeypatch.setattr(workflow.subprocess, "run", _completed)

    workflow._run_gh(
        [
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--limit",
            "501",
            "--json",
            "number",
        ]
    )
    workflow._run_gh(
        ["pr", "view", "17", "--repo", "owner/repo", "--json", workflow.PR_FIELDS]
    )

    assert calls == [
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "owner/repo",
            "--state",
            "open",
            "--limit",
            "501",
            "--json",
            "number",
        ],
        [
            "gh",
            "pr",
            "view",
            "17",
            "--repo",
            "owner/repo",
            "--json",
            workflow.PR_FIELDS,
        ],
    ]


def test_enforcement_records_gh_failures_without_aborting_the_run(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    at_risk_pr: dict[str, Any],
) -> None:
    live = {**at_risk_pr, "state": "OPEN"}
    previous = {
        "last_as_of": "2026-07-30",
        "last_score": 56,
        "last_next_actor": "OWNER",
        "below60_owner_streak": 1,
    }
    planned, _ = workflow._score_pr(live, "2026-07-31", previous)

    monkeypatch.setattr(
        workflow, "_fetch_pr", lambda _repo, number: {**live, "number": number}
    )

    def _boom(_args: list[str]) -> str:
        raise RuntimeError("gh command failed (1): rate limited")

    monkeypatch.setattr(workflow, "_run_gh_command", _boom)

    results = workflow._apply_recommendations(
        "owner/repo",
        "2026-07-31",
        [planned, {**planned, "number": 2}],
        {"prs": {"1": previous, "2": previous}},
        tmp_path / "enforcement.json",
    )

    assert [result["status"] for result in results] == ["error", "error"]
    assert "rate limited" in results[0]["error"]


def test_enforcement_ignores_non_actionable_recommendations(enforcement) -> None:
    run, commands = enforcement

    results = run([_plan(7, "observe_again"), _plan(8, "monitor_response")])

    assert results == []
    assert commands == []


def test_enforcement_journal_is_written_incrementally(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    journal = tmp_path / "enforcement.json"
    monkeypatch.setattr(
        workflow,
        "_fetch_pr",
        lambda _repo, _number: {"state": "CLOSED", "comments": []},
    )

    workflow._apply_recommendations(
        "owner/repo",
        "2026-07-31",
        [_plan(7, "warn_owner"), _plan(9, "warn_owner")],
        {"prs": {}},
        journal,
    )

    written = workflow._read_object(journal)
    assert [entry["number"] for entry in written["results"]] == [7, 9]


def test_manifest_reports_comment_specific_github_effect(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs: list[dict[str, Any]] = []
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(
        workflow,
        "_fetch_snapshot",
        lambda repo, as_of, snapshot_id, _max_prs: {
            "schema_version": workflow.SCHEMA_VERSION,
            "repo": repo,
            "as_of": as_of,
            "snapshot_id": snapshot_id,
            "prs": [],
        },
    )
    monkeypatch.setattr(
        workflow,
        "_apply_recommendations",
        lambda *_args: [{"number": 1, "status": "commented"}],
    )
    monkeypatch.setattr(workflow, "emit_output", outputs.append)

    workflow._run_locked(
        {
            "repo": "owner/repo",
            "as_of": "2026-07-31",
            "snapshot_id": "manifest-effect",
            "mode": "apply",
            "importance_analysis": False,
            "importance_provider": "claude_code",
            "importance_agent": "reviewer",
        }
    )

    assert outputs[-1]["posted_github_comments"] is True
    assert "mutated_github" not in outputs[-1]


# --------------------------------------------------------------------------
# Journal-visible run output, replay, and persisted-state migration
# --------------------------------------------------------------------------


@pytest.fixture
def run_locked(workflow: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Drive ``_run_locked`` against a stubbed snapshot under a temp HOME."""
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    outputs: list[dict[str, Any]] = []
    monkeypatch.setattr(workflow, "emit_output", outputs.append)
    applied: list[int] = []

    def _run(prs: list[dict[str, Any]], snapshot_id: str, mode: str = "dry_run"):
        monkeypatch.setattr(
            workflow,
            "_fetch_snapshot",
            lambda repo, as_of, sid, _max: {
                "schema_version": workflow.SCHEMA_VERSION,
                "repo": repo,
                "as_of": as_of,
                "snapshot_id": sid,
                "prs": prs,
            },
        )
        monkeypatch.setattr(
            workflow,
            "_apply_recommendations",
            lambda *_args: applied.append(1) or [],
        )
        workflow._run_locked(
            {
                "repo": "owner/repo",
                "as_of": "2026-07-31",
                "snapshot_id": snapshot_id,
                "mode": mode,
                "importance_analysis": False,
                "importance_provider": "claude_code",
                "importance_agent": "reviewer",
            }
        )
        return outputs[-1]

    return _run, outputs, applied, tmp_path


def test_manifest_exposes_structured_per_pr_decisions_and_artifact_paths(
    workflow: ModuleType,
    run_locked,
    at_risk_pr: dict[str, Any],
    stale_pr: dict[str, Any],
) -> None:
    """The run output is the journal record: decisions must be readable from it."""
    run, _outputs, _applied, tmp_path = run_locked
    warned = {
        **stale_pr,
        "number": 2,
        "comments": [
            _authored_comment(
                _marker_v2(0, "warning", 12, "2026-07-17"),
                "2026-07-17T00:00:00Z",
            )
        ],
    }

    manifest = run([at_risk_pr, warned], "decisions")

    assert manifest["policy"] == {
        "warning_grace_days": 7,
        "draft_grace_days": 14,
        "owner_response_grace_days": 30,
        "epoch_reentry_cooldown_days": 90,
        "max_notification_epochs": 4,
        "max_escalation_notifications": 3,
    }
    assert manifest["artifact_dir"].endswith("/runs/decisions")
    assert Path(manifest["decisions_file"]).is_file()
    assert Path(manifest["artifact_dir"]).is_dir()

    decisions = {entry["number"]: entry for entry in manifest["decisions"]}
    assert set(decisions) == {1, 2}
    assert decisions[1]["recommended_action"] == "observe_again"
    assert decisions[1]["notification_epoch"] == 0
    assert decisions[1]["notified"] is False
    assert decisions[2]["recommended_action"] == "propose_draft"
    assert decisions[2]["marker_stage"] == "warning"
    assert decisions[2]["notified"] is False  # dry-run never notifies
    assert isinstance(decisions[2]["action_reasons"], list)
    assert workflow._read_object(Path(manifest["decisions_file"]))["decisions"]


def test_run_replay_emits_the_same_manifest_without_reapplying(
    workflow: ModuleType,
    run_locked,
    at_risk_pr: dict[str, Any],
) -> None:
    """A resumed run must reuse the frozen manifest, not comment a second time."""
    run, outputs, applied, _tmp = run_locked

    first = run([at_risk_pr], "replay", mode="apply")
    second = run([at_risk_pr], "replay", mode="apply")

    assert applied == [1]
    assert first == second
    assert len(outputs) == 2


def test_legacy_v1_persisted_state_is_migrated_not_rejected(
    workflow: ModuleType,
    run_locked,
    at_risk_pr: dict[str, Any],
) -> None:
    """Rejecting v1 state would abort every run until an operator deleted the file."""
    run, _outputs, _applied, tmp_path = run_locked
    state_path = (
        tmp_path
        / ".local"
        / "state"
        / "cao"
        / "pr-health"
        / workflow._repo_storage_key("owner/repo")
        / "state.json"
    )
    workflow._write_json(
        state_path,
        {
            "schema_version": 1,
            "repo": "owner/repo",
            "prs": {
                "1": {
                    "last_as_of": "2026-07-17",
                    "last_score": 31,
                    "last_next_actor": "OWNER",
                    "below60_owner_streak": 2,
                }
            },
        },
    )

    manifest = run([at_risk_pr], "migrate")

    assert manifest["open_prs"] == 1
    migrated = workflow._read_object(state_path)
    assert migrated["schema_version"] == workflow.SCHEMA_VERSION
    assert migrated["prs"]["1"]["below60_owner_streak"] == 3
    assert migrated["prs"]["1"]["last_score"] == 56


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("snapshot_id", [".", ".."])
def test_reserved_snapshot_ids_are_rejected(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    snapshot_id: str,
) -> None:
    """``..`` matches SAFE_ID_RE but would resolve artifact_dir to the state root."""
    assert workflow.SAFE_ID_RE.fullmatch(snapshot_id) is not None
    assert snapshot_id in workflow.RESERVED_IDS

    def _no_network(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("validation must reject the id before any gh call")

    monkeypatch.setattr(workflow, "_run_gh", _no_network)
    monkeypatch.setattr(workflow, "_run_gh_command", _no_network)

    with pytest.raises(ValueError, match="snapshot_id"):
        workflow._run_locked(
            {
                "repo": "owner/repo",
                "as_of": "2026-07-31",
                "snapshot_id": snapshot_id,
                "mode": "dry_run",
                "importance_provider": "claude_code",
                "importance_agent": "reviewer",
            }
        )


# --------------------------------------------------------------------------
# Per-repository lock
# --------------------------------------------------------------------------


def test_repo_lock_is_exclusive_and_released(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import fcntl

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))

    handle, lock_path = workflow._acquire_repo_lock("owner/repo")
    assert lock_path.is_file()

    contender = lock_path.open("a+", encoding="utf-8")
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

        # Released: the contender can now take it.
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
    finally:
        contender.close()


def test_main_releases_the_lock_even_when_the_run_fails(
    workflow: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import fcntl

    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    monkeypatch.setattr(workflow, "get_inputs", lambda: {"repo": "owner/repo"})

    def _boom(_inputs: dict[str, Any]) -> None:
        raise RuntimeError("run failed")

    monkeypatch.setattr(workflow, "_run_locked", _boom)

    with pytest.raises(RuntimeError, match="run failed"):
        workflow.main()

    lock_path = (
        tmp_path
        / ".local"
        / "state"
        / "cao"
        / "pr-health"
        / workflow._repo_storage_key("owner/repo")
        / ".workflow.lock"
    )
    contender = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
    finally:
        contender.close()


# --------------------------------------------------------------------------
# Scheduled flows
# --------------------------------------------------------------------------


def test_guard_enforces_exact_fourteen_day_cadence(guard: ModuleType) -> None:
    assert guard.is_due(date(2026, 1, 5))
    assert not guard.is_due(date(2026, 1, 12))
    assert guard.is_due(date(2026, 1, 19))
    assert guard.is_due(date(2027, 1, 18))
    assert not guard.is_due(date(2025, 12, 22))  # before the anchor


def test_guard_uses_utc_so_thresholds_do_not_shift_at_midnight(
    guard: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``as_of`` is compared against gh's UTC timestamps, so it must be UTC.

    ``date.today()`` is stubbed to a sentinel the UTC clock can never return,
    so this fails on a UTC-local machine too — where simply comparing against
    ``datetime.now(timezone.utc).date()`` would pass vacuously.
    """

    class _LocalDate(date):
        @classmethod
        def today(cls) -> date:
            return date(1999, 12, 31)

    monkeypatch.setattr(guard, "date", _LocalDate)

    assert guard.today_utc() == datetime.now(timezone.utc).date()
    assert guard.today_utc() != date(1999, 12, 31)


def test_scheduled_flow_defaults_to_non_mutating_mode() -> None:
    flow_path = EXAMPLE_DIR / "pr-health-biweekly.md"
    metadata, prompt = _parse_flow_file(flow_path)

    assert metadata["schedule"] == "0 9 * * 0"
    assert metadata["script"] == "./pr_health_biweekly_guard.py"
    assert (flow_path.parent / metadata["script"]).is_file()
    assert "--input mode=dry_run" in prompt
    assert "--input close_allowlist=" not in prompt
    assert "--input mode=apply" not in prompt
    assert "blocks until completion" in prompt
    assert "full result JSON" in prompt


def test_apply_schedule_explicitly_authorizes_comments_only() -> None:
    flow_path = EXAMPLE_DIR / "pr-health-biweekly-apply.md"
    metadata, prompt = _parse_flow_file(flow_path)
    normalized_prompt = " ".join(prompt.split())

    assert metadata["schedule"] == "0 9 * * 0"
    assert metadata["script"] == "./pr_health_biweekly_guard.py"
    assert "--input mode=apply" in prompt
    assert "--input repo=[[repo]]" in prompt
    assert "--input close_allowlist=" not in prompt
    assert "comments are the only authorized github mutation" in prompt.lower()
    assert "Do not add labels, change PR state, submit reviews" in normalized_prompt
    assert "blocks until completion" in prompt
    assert "full result JSON" in prompt


def test_apply_schedule_defers_authorization_to_the_platform_approval_gate() -> None:
    """Prompt prose is not an authorization boundary; the plan-approval gate is."""
    _metadata, prompt = _parse_flow_file(EXAMPLE_DIR / "pr-health-biweekly-apply.md")
    normalized = " ".join(prompt.split())

    assert "workflow.require_approval" in normalized
    assert "cao workflow approve" in normalized
    # The apply plan is a DIFFERENT plan than the dry-run plan, because inputs
    # are part of the plan identifier. Approving one never authorizes the other.
    assert "plan_id" in normalized


def _readme_text() -> str:
    return (EXAMPLE_DIR / "README.md").read_text(encoding="utf-8")


def test_readme_documents_the_approved_lifecycle_policy() -> None:
    readme = _readme_text()

    assert "90-day" in readme
    assert "30-day" in readme
    assert "four notification epochs" in readme
    assert "three escalation" in readme
    assert "report-only" in readme
    assert "cao-pr-health:v2" in readme
    assert "epoch 0" in readme


def test_readme_documents_approval_journal_and_replay_behaviour() -> None:
    readme = _readme_text()

    assert "cao workflow approve" in readme
    assert "workflow.require_approval" in readme
    assert "plan_id" in readme
    assert "decisions" in readme
    assert "replay" in readme.lower()
    assert "resume" in readme.lower()


def test_readme_records_the_platform_capability_gap_without_guessing() -> None:
    """The example must not imply APIs this checkout does not ship."""
    readme = _readme_text()

    assert "Platform gaps" in readme
    assert "run_step" in readme
    assert "step(" in readme
    assert "recovery" in readme


def _guard_payload() -> dict[str, Any]:
    """The guard's real stdout, parsed the way flow_service parses it."""
    import json
    import subprocess
    import sys

    completed = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "pr_health_biweekly_guard.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    payload: dict[str, Any] = json.loads(completed.stdout)
    assert set(payload) == {"execute", "output"}
    return payload


def _rendered_flows() -> dict[str, str]:
    """Both flow prompts rendered from the guard's actual output.

    Rendering from the guard's real payload — rather than a hand-written
    variable dict — is what makes these assertions able to fail if the guard
    stops differentiating identifiers by mode.
    """
    from cli_agent_orchestrator.utils.template import render_template

    variables = _guard_payload()["output"]
    return {
        name: render_template(_parse_flow_file(EXAMPLE_DIR / name)[1], variables)
        for name in ("pr-health-biweekly.md", "pr-health-biweekly-apply.md")
    }


def test_guard_emits_every_variable_both_flow_templates_require() -> None:
    """render_template raises on a missing variable, so this is the wiring proof."""
    assert set(_rendered_flows()) == {
        "pr-health-biweekly.md",
        "pr-health-biweekly-apply.md",
    }


def test_both_flows_can_be_registered_without_colliding() -> None:
    """Mode-agnostic identifiers would make the two flows mutually exclusive.

    The second flow to run on a due Monday would hit the workflow's manifest
    guard ("snapshot_id already exists with different inputs").
    """
    rendered = _rendered_flows()

    def _value(text: str, flag: str) -> str:
        return text.split(f"--input {flag}=", 1)[1].split()[0]

    def _run_id(text: str) -> str:
        return text.split("--run-id ", 1)[1].split()[0]

    dry = rendered["pr-health-biweekly.md"]
    apply_ = rendered["pr-health-biweekly-apply.md"]

    assert _value(dry, "snapshot_id") != _value(apply_, "snapshot_id")
    assert _run_id(dry) != _run_id(apply_)
    # Both must still agree on the repository and the evaluation date.
    assert _value(dry, "repo") == _value(apply_, "repo")
    assert _value(dry, "as_of") == _value(apply_, "as_of")
