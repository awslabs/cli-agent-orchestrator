"""Tests for the deterministic PR-health workflow example.

The example lives under ``examples/`` and is loaded by path, so it is not
covered by ``--cov=src``. These tests are therefore the only coverage the
scoring engine, the marker lifecycle, and the GitHub-mutating enforcement
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
    return f"<!-- cao-pr-health:v1 stage={stage} score={score} as_of={as_of} -->"


def _authored_comment(body: str, created_at: str) -> dict[str, Any]:
    return {
        "author": {"login": "maintainer"},
        "createdAt": created_at,
        "body": body,
        "viewerDidAuthor": True,
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
            _authored_comment(_marker("warning", 44, "2026-07-24"), "2026-07-24T00:00:00Z")
        ],
    }


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


def test_date_ordinal_agrees_with_stdlib_across_a_dense_range(workflow: ModuleType) -> None:
    """The bespoke ordinal must match ``date.toordinal`` offsets exactly."""
    samples = [
        date(year, month, day)
        for year in (1900, 1999, 2000, 2024, 2025, 2026, 2100)
        for month in range(1, 13)
        for day in (1, 15, 28)
    ]
    for value in samples:
        delta = workflow._date_ordinal(value.isoformat()) - value.toordinal()
        assert delta == workflow._date_ordinal("2026-01-01") - date(2026, 1, 1).toordinal()


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
        "comments": [_authored_comment(_marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z")],
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
        "comments": [_authored_comment(_marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z")],
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


def test_streak_restarts_instead_of_raising_on_stale_state(workflow: ModuleType) -> None:
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
            _authored_comment(_marker("draft", 50, "2026-07-01"), "2026-07-01T00:00:00Z"),
            _authored_comment(_marker("warning", 45, "2026-07-20"), "2026-07-20T00:00:00Z"),
        ],
    }

    marker = workflow._lifecycle_marker(pr)

    assert marker is not None
    assert marker["stage"] == "draft"
    assert marker["created_at"] == "2026-07-01T00:00:00Z"


def test_earliest_marker_of_the_winning_stage_owns_the_grace_period(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    pr = {
        **base_pr,
        "comments": [
            _authored_comment(_marker("warning", 50, "2026-07-01"), "2026-07-01T00:00:00Z"),
            _authored_comment(_marker("warning", 45, "2026-07-20"), "2026-07-20T00:00:00Z"),
        ],
    }

    marker = workflow._lifecycle_marker(pr)

    assert marker is not None
    assert marker["created_at"] == "2026-07-01T00:00:00Z"


def test_lifecycle_reaches_closure_at_a_seven_day_cadence(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    """Progression is cadence-independent: weekly runs must still close.

    Simulates warn -> draft -> close on a 7-day cadence, appending the marker
    each stage emits, which is what a real weekly schedule would accumulate.
    """
    pr: dict[str, Any] = {
        **base_pr,
        "reviewDecision": "REVIEW_REQUIRED",
        "mergeStateStatus": "BLOCKED",
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [],
        "labels": [],
        "closingIssuesReferences": [],
        "body": "short",
        "files": [],
    }
    dates = [
        "2026-07-01",
        "2026-07-08",
        "2026-07-15",
        "2026-07-22",
        "2026-07-29",
        "2026-08-05",
        "2026-08-12",
        "2026-08-19",
    ]
    state: dict[str, Any] | None = None
    actions = []
    for as_of in dates:
        result, state = workflow._score_pr(pr, as_of, state)
        action = result["recommended_action"]
        actions.append(action)
        if action in workflow.ACTIONABLE_RECOMMENDATIONS:
            pr = {
                **pr,
                "comments": [
                    *pr["comments"],
                    _authored_comment(
                        workflow._comment_body(result, as_of),
                        f"{as_of}T00:00:00Z",
                    ),
                ],
            }
        if action == "propose_close":
            # A closed PR leaves the open-PR snapshot; the ladder ends here.
            break

    assert "warn_owner" in actions
    assert "propose_draft" in actions
    assert "propose_close" in actions
    assert actions.index("warn_owner") < actions.index("propose_draft")
    assert actions.index("propose_draft") < actions.index("propose_close")
    # A stage is never recommended twice: no duplicate owner notifications.
    for stage_action in ("warn_owner", "propose_draft", "propose_close"):
        assert actions.count(stage_action) == 1


def test_warned_pr_holds_instead_of_re_warning_before_the_deadline(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
) -> None:
    """Inside the warning grace period the ladder holds, it does not repeat."""
    pr = {
        **warned_pr,
        "comments": [
            _authored_comment(_marker("warning", 44, "2026-07-28"), "2026-07-28T00:00:00Z")
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


# --------------------------------------------------------------------------
# Comment bodies and markers
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "stage"),
    [
        ("warn_owner", "warning"),
        ("propose_draft", "draft"),
        ("second_owner_notification", "draft"),
        ("propose_close", "closed"),
    ],
)
def test_comment_body_embeds_the_stage_marker(
    workflow: ModuleType,
    warned_pr: dict[str, Any],
    action: str,
    stage: str,
) -> None:
    item, _ = workflow._score_pr(warned_pr, "2026-07-31", None)
    item = {**item, "recommended_action": action}

    body = workflow._comment_body(item, "2026-07-31")

    assert body.startswith("@owner ")
    assert _marker(stage, item["score"], "2026-07-31") in body
    # Every emitted marker must be parseable by the reader that consumes it.
    parsed = workflow.MARKER_RE.search(body)
    assert parsed is not None
    assert parsed.group(1) == stage


def test_escalation_comment_names_the_protection_and_is_parseable(
    workflow: ModuleType,
    base_pr: dict[str, Any],
) -> None:
    protected_pr = {
        **base_pr,
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [_authored_comment(_marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z")],
        "title": "fix(security): prevent command injection",
        "labels": [{"name": "security"}],
    }
    protected, _ = workflow._score_pr(protected_pr, "2026-07-31", None)

    body = workflow._comment_body(protected, "2026-07-31")

    assert body.startswith("@owner ")
    assert "priority P0" in body
    assert "<!-- cao-pr-health:v1 action=escalation score=25 as_of=2026-07-31 -->" in body
    assert workflow.ESCALATION_MARKER_RE.search(body) is not None


def test_action_marker_rejects_a_non_enforcement_action(workflow: ModuleType) -> None:
    with pytest.raises(ValueError):
        workflow._action_marker("observe_again", 40, "2026-07-31")


# --------------------------------------------------------------------------
# Cross-run idempotency: dedupe on stage, never on score/as_of
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "stage"),
    [
        ("warn_owner", "warning"),
        ("propose_draft", "draft"),
        ("second_owner_notification", "draft"),
        ("propose_close", "closed"),
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
    pr = {"comments": [_authored_comment(_marker(stage, 33, "2026-01-01"), "2026-01-01T00:00:00Z")]}

    assert workflow._has_marker_for_action(pr, action) is True


def test_escalation_marker_from_a_prior_run_is_recognized(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _authored_comment(
                "<!-- cao-pr-health:v1 action=escalation score=12 as_of=2026-01-01 -->",
                "2026-01-01T00:00:00Z",
            )
        ]
    }

    assert workflow._has_marker_for_action(pr, "escalate_protected_pr") is True


def test_marker_for_a_different_stage_does_not_dedupe(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            _authored_comment(_marker("warning", 44, "2026-07-24"), "2026-07-24T00:00:00Z")
        ]
    }

    assert workflow._has_marker_for_action(pr, "warn_owner") is True
    assert workflow._has_marker_for_action(pr, "propose_draft") is False
    assert workflow._has_marker_for_action(pr, "escalate_protected_pr") is False


def test_marker_authored_by_someone_else_does_not_dedupe(workflow: ModuleType) -> None:
    pr = {
        "comments": [
            {
                "author": {"login": "owner"},
                "createdAt": "2026-07-24T00:00:00Z",
                "body": _marker("warning", 44, "2026-07-24"),
                "viewerDidAuthor": False,
            }
        ]
    }

    assert workflow._has_marker_for_action(pr, "warn_owner") is False


def test_has_marker_for_action_rejects_unknown_actions(workflow: ModuleType) -> None:
    with pytest.raises(ValueError):
        workflow._has_marker_for_action({"comments": []}, "observe_again")


# --------------------------------------------------------------------------
# Enforcement branches: every path that can mutate GitHub
# --------------------------------------------------------------------------


def _plan(number: int, action: str, score: int = 40) -> dict[str, Any]:
    return {"number": number, "score": score, "recommended_action": action}


@pytest.fixture
def enforcement(workflow: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Run ``_apply_recommendations`` against a stubbed gh surface."""
    commands: list[list[str]] = []

    def _run(
        plans: list[dict[str, Any]],
        live_pr: dict[str, Any] | None = None,
        close_allowlist: set[int] | None = None,
        state: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        monkeypatch.setattr(
            workflow,
            "_fetch_pr",
            lambda _repo, _number: dict(live_pr or {"state": "OPEN", "comments": []}),
        )
        monkeypatch.setattr(workflow, "_run_gh_command", lambda args: commands.append(args))
        return workflow._apply_recommendations(
            "owner/repo",
            "2026-07-31",
            plans,
            state or {"prs": {}},
            close_allowlist or set(),
            tmp_path / "enforcement.json",
        )

    return _run, commands


def test_enforcement_skips_non_open_pr_before_mutation(enforcement) -> None:
    run, commands = enforcement

    results = run([_plan(7, "warn_owner")], live_pr={"state": "CLOSED", "comments": []})

    assert results == [{"number": 7, "planned_action": "warn_owner", "status": "skipped_not_open"}]
    assert commands == []


def test_enforcement_is_idempotent_across_runs(enforcement) -> None:
    """A prior run's marker (different score/date) must suppress the comment."""
    run, commands = enforcement
    live = {
        "state": "OPEN",
        "comments": [
            _authored_comment(_marker("warning", 12, "2026-01-01"), "2026-01-01T00:00:00Z")
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


def test_enforcement_refuses_closure_without_the_allowlist(
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
        "comments": [_authored_comment(_marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z")],
    }
    planned, _ = workflow._score_pr(abandoned_pr, "2026-07-31", None)
    assert planned["recommended_action"] == "propose_close"

    results = run([planned], live_pr=abandoned_pr, close_allowlist=set())

    assert results[0]["status"] == "skipped_closure_not_allowlisted"
    assert commands == []


def test_enforcement_closes_when_allowlisted(
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
        "comments": [_authored_comment(_marker("draft", 50, "2026-07-17"), "2026-07-17T00:00:00Z")],
    }
    planned, _ = workflow._score_pr(abandoned_pr, "2026-07-31", None)

    results = run([planned], live_pr=abandoned_pr, close_allowlist={1})

    assert results[0]["status"] == "closed_and_commented"
    assert len(commands) == 1
    assert commands[0][:5] == ["pr", "close", "1", "--repo", "owner/repo"]
    assert _marker("closed", 25, "2026-07-31") in commands[0][-1]


def test_enforcement_drafts_then_comments(
    workflow: ModuleType,
    enforcement,
    warned_pr: dict[str, Any],
) -> None:
    run, commands = enforcement
    live = {**warned_pr, "state": "OPEN"}
    planned, _ = workflow._score_pr(live, "2026-07-31", None)
    assert planned["recommended_action"] == "propose_draft"

    results = run([planned], live_pr=live)

    assert results[0]["status"] == "drafted_and_commented"
    assert commands[0] == ["pr", "ready", "1", "--repo", "owner/repo", "--undo"]
    assert commands[1][:5] == ["pr", "comment", "1", "--repo", "owner/repo"]
    assert _marker("draft", 44, "2026-07-31") in commands[1][-1]


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
    assert _marker("draft", planned["score"], "2026-07-31") in commands[0][-1]


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
    assert _marker("warning", 56, "2026-07-31") in commands[0][-1]


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

    monkeypatch.setattr(workflow, "_fetch_pr", lambda _repo, number: {**live, "number": number})

    def _boom(_args: list[str]) -> str:
        raise RuntimeError("gh command failed (1): rate limited")

    monkeypatch.setattr(workflow, "_run_gh_command", _boom)

    results = workflow._apply_recommendations(
        "owner/repo",
        "2026-07-31",
        [planned, {**planned, "number": 2}],
        {"prs": {"1": previous, "2": previous}},
        set(),
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
        set(),
        journal,
    )

    written = workflow._read_object(journal)
    assert [entry["number"] for entry in written["results"]] == [7, 9]


# --------------------------------------------------------------------------
# Input validation
# --------------------------------------------------------------------------


def test_close_allowlist_parsing(workflow: ModuleType) -> None:
    assert workflow._parse_close_allowlist("") == set()
    assert workflow._parse_close_allowlist("222, 231,222") == {222, 231}


@pytest.mark.parametrize("value", ["0", "-1", "abc", "12a", "1,,2", "1.5"])
def test_close_allowlist_rejects_non_pr_numbers(workflow: ModuleType, value: str) -> None:
    with pytest.raises(ValueError):
        workflow._parse_close_allowlist(value)


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
    assert "--input close_allowlist=" in prompt
    assert "--input mode=apply" not in prompt


def test_apply_schedule_requires_explicit_template_and_disables_closure() -> None:
    flow_path = EXAMPLE_DIR / "pr-health-biweekly-apply.md"
    metadata, prompt = _parse_flow_file(flow_path)

    assert metadata["schedule"] == "0 9 * * 0"
    assert metadata["script"] == "./pr_health_biweekly_guard.py"
    assert "--input mode=apply" in prompt
    assert "--input close_allowlist=" in prompt
    assert "Closure is not authorized" in prompt
    assert "STANDING UNATTENDED WRITE GRANT" in prompt


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
