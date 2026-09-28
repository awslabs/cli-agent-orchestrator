"""Deterministic health analysis and comment-only notifications for open GitHub PRs.

Dry-run mode snapshots GitHub data, calculates scores with fixed rules, persists
the two-observation warning state, and asks a reviewer for an advisory importance
synthesis. Apply mode revalidates every candidate against live data before posting
an idempotent comment. It cannot change labels, PR state, reviews, statuses, checks,
or any other PR status.

Notifications are organized into NOTIFICATION EPOCHS, and the whole point of the
model is that a PR's lifetime noise is a small constant rather than a function of
how long it stays open. One epoch is at most three comments and always begins
with a warning: warning, then either an escalation (a protected P0/P1 or already
approved PR, which ends the epoch there) or a draft recommendation, and then a
closure recommendation. An escalation and a closure recommendation are both
terminal and both close the epoch. A closed epoch can reopen only after
``EPOCH_REENTRY_COOLDOWN_DAYS`` measured from whichever came last, the terminal
advisory or the owner's answer, and it always restarts at a warning. Four epochs
and three escalations are the lifetime caps; past either, the decision is
``report_only`` and nothing is posted.

Three invariants keep enforcement from spamming or stalling, and all three are
covered by ``test/examples/test_pr_health_workflow_example.py`` over simulated
multi-year runs at the real fortnightly cadence:

- Stage dedup is EXACT on ``(epoch, stage)``. Not on marker text, which embeds
  the emitting run's own score and date and so never matches; and not on a date
  window, which expires and thereby lets a spent stage be posted twice inside a
  single epoch.
- The ladder never runs backwards. Every branch that observes a marker returns a
  decision; none falls through to the fresh ladder, which is how a warned PR used
  to be re-warned and how a PR scoring 51-59 used to stall one rung short forever.
- Lifecycle progression is independent of the run cadence. Grace periods are
  measured in elapsed days against marker dates, so a weekly, fortnightly, or
  ad-hoc run reaches the same stage after the same elapsed time.

Marker versioning: this example emits ``cao-pr-health:v2`` markers carrying an
explicit ``epoch`` and ``stage``. The earlier ``v1`` markers — both the
``stage=`` and the ``action=escalation`` forms — are read as epoch 0, so a PR
that was mid-ladder when this example changed keeps its place rather than
restarting a ladder it has already climbed.

Authorization: dry-run mode is automatic and non-mutating. Apply mode is NOT
gated by anything in this file's prose — prose is not an authorization boundary.
It is gated by CAO's plan-approval gate, because the run's ``inputs`` are part of
the plan identifier, so an ``apply`` run is a DIFFERENT plan than a ``dry_run``
run and needs its own ``cao workflow approve <plan_id>``. See this example's
README for the version requirement and for the capability gaps this file does not
paper over.

Example (authoring does not authorize this run):
    cao workflow run pr_health --run-id pr-health-2026-07-31 \
      --input as_of=2026-07-31 \
      --input snapshot_id=2026-07-31
"""

from __future__ import annotations

import fcntl
import json
import re
import subprocess
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

from cao_workflow import ShimError, emit_output, get_inputs, run_step

INPUTS = {
    "repo": {
        "type": "string",
        "required": False,
        "default": "awslabs/cli-agent-orchestrator",
    },
    "as_of": {
        "type": "string",
        "required": True,
    },
    "snapshot_id": {
        "type": "string",
        "required": True,
    },
    "max_prs": {
        "type": "int",
        "required": False,
        "default": 500,
    },
    "importance_analysis": {
        "type": "bool",
        "required": False,
        "default": True,
    },
    "importance_provider": {
        "type": "string",
        "required": False,
        "default": "claude_code",
    },
    "importance_agent": {
        "type": "string",
        "required": False,
        "default": "reviewer",
    },
    "mode": {
        "type": "string",
        "required": False,
        "default": "dry_run",
    },
}

SCHEMA_VERSION = 2
SUPPORTED_STATE_SCHEMA_VERSIONS = frozenset({1, 2})
MARKER_SCHEMA_VERSION = 2

STAGE_NAMES = "warning|draft_recommendation|closure_recommendation|escalation"
MARKER_V2_RE = re.compile(
    r"<!--\s*cao-pr-health:v2\s+"
    r"epoch=(\d{1,3})\s+"
    rf"stage=({STAGE_NAMES})\s+"
    r"score=(\d{1,3})\s+"
    r"as_of=(\d{4}-\d{2}-\d{2})\s*-->"
)
# v1 had no epoch. Every v1 marker reads as notification epoch 0 so a PR that
# was mid-ladder when this example shipped keeps its place instead of restarting.
MARKER_V1_RE = re.compile(
    r"<!--\s*cao-pr-health:v1\s+"
    r"stage=(warning|draft_recommendation|closure_recommendation|draft|closed)\s+"
    r"score=(\d{1,3})\s+"
    r"as_of=(\d{4}-\d{2}-\d{2})\s*-->"
)
ESCALATION_MARKER_V1_RE = re.compile(
    r"<!--\s*cao-pr-health:v1\s+action=escalation\s+score=(\d{1,3})\s+"
    r"as_of=(\d{4}-\d{2}-\d{2})\s*-->"
)
# ``draft`` and ``closed`` were emitted by the pre-comment-only example. Read
# them conservatively, but never emit wording that implies a status effect.
STAGE_ALIASES = {
    "warning": "warning",
    "draft": "draft_recommendation",
    "draft_recommendation": "draft_recommendation",
    "closed": "closure_recommendation",
    "closure_recommendation": "closure_recommendation",
    "escalation": "escalation",
}
# ``escalation`` and ``closure_recommendation`` share rank 2: both are the last
# advisory an epoch can carry, and which one a PR gets depends only on whether
# it is protected. Equal rank is what lets the monotonicity invariant hold
# across a protected/unprotected transition.
STAGE_RANK = {
    "warning": 0,
    "draft_recommendation": 1,
    "closure_recommendation": 2,
    "escalation": 2,
}
TERMINAL_STAGES = frozenset({"closure_recommendation", "escalation"})

# ---------------------------------------------------------------------------
# Fixed notification policy. These six values are the whole noise budget: a PR
# can receive at most MAX_NOTIFICATION_EPOCHS ladders in its lifetime, each of
# at most three comments, separated by EPOCH_REENTRY_COOLDOWN_DAYS. The grace
# periods are chosen against the scheduled flows' fortnightly cadence so the
# ladder always advances on a run boundary rather than between two runs.
# ---------------------------------------------------------------------------
HEALTHY_SCORE = 60
WARNING_GRACE_DAYS = 7
DRAFT_GRACE_DAYS = 14
OWNER_RESPONSE_GRACE_DAYS = 30
EPOCH_REENTRY_COOLDOWN_DAYS = 90
MAX_NOTIFICATION_EPOCHS = 4
MAX_ESCALATION_NOTIFICATIONS = 3
STALE_DRAFT_PENALTY = 25
# Inline decisions keep the journal record readable without unbounding it; the
# full list is always written to ``decisions.json`` regardless.
MAX_INLINE_DECISIONS = 200

COMMENT_HEADER = "## Automated PR-health notification"
COMMENT_ONLY_DISCLAIMER = (
    "> This automated workflow only posts comments; it does not change PR status."
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
RESERVED_IDS = {".", ".."}
ISSUE_REF_RE = re.compile(r"(?i)(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)?\s*#\d+")
RATIONALE_RE = re.compile(r"(?i)\b(?:motivation|rationale|problem|why|because)\b")
TEST_RE = re.compile(r"(?i)\b(?:test|tests|tested|testing|verification)\b")

FAILURE_CONCLUSIONS = {
    "ACTION_REQUIRED",
    "CANCELLED",
    "ERROR",
    "FAILURE",
    "STALE",
    "TIMED_OUT",
}
SUCCESS_CONCLUSIONS = {"NEUTRAL", "SKIPPED", "SUCCESS"}
PENDING_STATES = {
    "EXPECTED",
    "IN_PROGRESS",
    "PENDING",
    "QUEUED",
    "REQUESTED",
    "WAITING",
}
P0_TERMS = {
    "critical",
    "cve",
    "data loss",
    "priority:p0",
    "release blocker",
    "release-blocker",
    "security",
    "vulnerability",
}
P1_TERMS = {
    "breaking",
    "priority:p1",
    "regression",
    "sev1",
    "sev2",
}
P3_TERMS = {
    "chore",
    "documentation",
    "docs",
    "example",
    "examples",
    "tests",
}
PR_FIELDS = (
    "number,title,url,state,author,isDraft,body,createdAt,additions,deletions,"
    "changedFiles,files,labels,comments,commits,reviewDecision,mergeable,"
    "mergeStateStatus,statusCheckRollup,closingIssuesReferences"
)
ACTIONABLE_RECOMMENDATIONS = frozenset(
    {
        "escalate_protected_pr",
        "propose_close",
        "propose_draft",
        "second_owner_notification",
        "warn_owner",
    }
)
# Reported in the run output and the report, never commented: the PR has hit a
# lifetime cap, so the only remaining action is a human's.
REPORT_ONLY_RECOMMENDATION = "report_only"
# Advisory marker stage each notification writes. Idempotency is keyed on the
# (epoch, stage) pair: exact-string comparison would never match a prior run
# because the marker also carries that run's score and date, and date-window
# comparison would let a spent stage come back inside its own epoch.
ACTION_STAGES = {
    "warn_owner": "warning",
    "propose_draft": "draft_recommendation",
    "second_owner_notification": "draft_recommendation",
    "propose_close": "closure_recommendation",
    "escalate_protected_pr": "escalation",
}


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _date_ordinal(value: str) -> int:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if match is None:
        raise ValueError(f"invalid ISO date: {value!r}")
    year, month, day = (int(part) for part in match.groups())
    month_lengths = (
        31,
        28 + int(_is_leap(year)),
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    )
    if year < 1 or not 1 <= month <= 12 or not 1 <= day <= month_lengths[month - 1]:
        raise ValueError(f"invalid calendar date: {value!r}")
    prior_years = year - 1
    leap_days = prior_years // 4 - prior_years // 100 + prior_years // 400
    return prior_years * 365 + leap_days + sum(month_lengths[: month - 1]) + day


def _validate_as_of(value: str) -> None:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        raise ValueError("as_of must be a calendar date in YYYY-MM-DD form")
    _date_ordinal(value)


def _days_between(earlier: str, later: str) -> int:
    return max(0, _date_ordinal(later) - _date_ordinal(earlier))


def _repo_storage_key(repo: str) -> str:
    return quote(repo, safe="")


def _is_scoped_repo(value: str) -> bool:
    return value.count("/") == 1 and all(value.split("/"))


def _is_positive_ascii_integer(value: str) -> bool:
    return re.fullmatch(r"[1-9][0-9]*", value) is not None


def _validate_gh_read_args(args: list[str]) -> None:
    list_shape = (
        len(args) == 10
        and args[:2] == ["pr", "list"]
        and args[2] == "--repo"
        and _is_scoped_repo(args[3])
        and args[4:6] == ["--state", "open"]
        and args[6] == "--limit"
        and _is_positive_ascii_integer(args[7])
        and int(args[7]) <= 2001
        and args[8:] == ["--json", "number"]
    )
    view_shape = (
        len(args) == 7
        and args[:2] == ["pr", "view"]
        and _is_positive_ascii_integer(args[2])
        and args[3] == "--repo"
        and _is_scoped_repo(args[4])
        and args[5:] == ["--json", PR_FIELDS]
    )
    if not (list_shape or view_shape):
        raise ValueError(
            "the PR-health GitHub read boundary only permits scoped pr list/view"
        )


def _run_gh(args: list[str]) -> Any:
    _validate_gh_read_args(args)
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip() or completed.stdout.strip() or "unknown gh error"
        )
        raise RuntimeError(f"gh command failed ({completed.returncode}): {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh command did not return valid JSON") from exc


def _run_gh_command(args: list[str]) -> str:
    if len(args) < 2 or args[:2] != ["pr", "comment"]:
        raise ValueError("the PR-health GitHub write boundary is comment-only")
    if (
        len(args) != 7
        or not _is_positive_ascii_integer(args[2])
        or args[3] != "--repo"
        or not _is_scoped_repo(args[4])
        or args[5] != "--body"
        or not args[6]
    ):
        raise ValueError(
            "PR-health comments require an explicit repository and PR number"
        )
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (
            completed.stderr.strip() or completed.stdout.strip() or "unknown gh error"
        )
        raise RuntimeError(f"gh command failed ({completed.returncode}): {detail}")
    return completed.stdout.strip()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        f"{json.dumps(value, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _login(value: Any) -> str:
    if isinstance(value, dict):
        login = value.get("login")
        if isinstance(login, str):
            return login
    return ""


def _labels(pr: dict[str, Any]) -> list[str]:
    result = []
    for label in pr.get("labels") or []:
        if isinstance(label, dict) and isinstance(label.get("name"), str):
            result.append(label["name"].strip().lower())
    return sorted(set(result))


def _latest_commit_at(pr: dict[str, Any]) -> str:
    """Newest commit date on the PR, falling back to its creation date.

    ``gh pr view --json commits`` may return a truncated page on PRs with very
    long histories. A missed newer commit only makes the PR look more idle than
    it is, which lowers its score; the live re-score before any comment reads
    the same field, so notification never acts on a stale page alone.
    """
    dates = []
    for commit in pr.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        value = commit.get("committedDate") or commit.get("authoredDate")
        if isinstance(value, str):
            dates.append(value)
    if dates:
        return max(dates)
    created = pr.get("createdAt")
    if not isinstance(created, str):
        raise ValueError(f"PR #{pr.get('number')} has no usable activity date")
    return created


def _ci_component(pr: dict[str, Any]) -> tuple[int, str]:
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        return 5, "missing"

    has_pending = False
    has_failure = False
    for check in checks:
        if not isinstance(check, dict):
            continue
        conclusion = str(check.get("conclusion") or "").upper()
        state = str(check.get("state") or check.get("status") or "").upper()
        if conclusion in FAILURE_CONCLUSIONS or state in FAILURE_CONCLUSIONS:
            has_failure = True
        elif (
            state in PENDING_STATES
            or not conclusion
            and state not in SUCCESS_CONCLUSIONS
        ):
            has_pending = True
        elif conclusion and conclusion not in SUCCESS_CONCLUSIONS:
            has_failure = True

    if has_failure:
        return 0, "failing"
    if has_pending:
        return 12, "pending"
    return 20, "passing"


def _merge_component(pr: dict[str, Any]) -> tuple[int, str]:
    mergeable = str(pr.get("mergeable") or "UNKNOWN").upper()
    state = str(pr.get("mergeStateStatus") or "UNKNOWN").upper()
    if mergeable == "CONFLICTING" or state == "DIRTY":
        return 0, "conflicting"
    if mergeable == "UNKNOWN" or state == "UNKNOWN":
        return 5, "unknown"
    if state == "CLEAN":
        return 15, "clean"
    return 10, state.lower()


def _review_component(pr: dict[str, Any]) -> tuple[int, str]:
    if bool(pr.get("isDraft")):
        return 5, "draft"
    decision = str(pr.get("reviewDecision") or "REVIEW_REQUIRED").upper()
    if decision == "APPROVED":
        return 15, "approved"
    if decision == "CHANGES_REQUESTED":
        return 0, "changes_requested"
    return 10, "review_required"


def _engagement_component(idle_days: int) -> int:
    if idle_days <= 3:
        return 40
    if idle_days <= 7:
        return 32
    if idle_days <= 14:
        return 24
    if idle_days <= 21:
        return 16
    if idle_days <= 30:
        return 8
    return 0


def _completeness_component(pr: dict[str, Any]) -> tuple[int, dict[str, int]]:
    body = str(pr.get("body") or "").strip()
    files = pr.get("files") or []
    paths = [str(item.get("path") or "") for item in files if isinstance(item, dict)]
    description = 3 if len(body) >= 200 else 0
    linked_issue = bool(pr.get("closingIssuesReferences")) or bool(
        ISSUE_REF_RE.search(body)
    )
    rationale = 2 if linked_issue or RATIONALE_RE.search(body) else 0
    has_test_file = any(
        path.startswith(("test/", "tests/", "web/src/test/"))
        or "/test_" in path
        or path.endswith((".spec.ts", ".spec.tsx", ".test.ts", ".test.tsx"))
        for path in paths
    )
    tests = 3 if has_test_file or TEST_RE.search(body) else 0
    changed_files = int(pr.get("changedFiles") or len(paths))
    churn = int(pr.get("additions") or 0) + int(pr.get("deletions") or 0)
    if changed_files <= 25 and churn <= 1000:
        focus = 2
    elif changed_files <= 50 and churn <= 3000:
        focus = 1
    else:
        focus = 0
    details = {
        "description": description,
        "rationale": rationale,
        "tests": tests,
        "focus": focus,
    }
    return sum(details.values()), details


def _priority(pr: dict[str, Any]) -> tuple[str, list[str]]:
    labels = _labels(pr)
    title = str(pr.get("title") or "").lower()
    evidence = sorted(set(labels + [title]))
    joined = " ".join(evidence)
    p0_matches = sorted(term for term in P0_TERMS if term in joined)
    if p0_matches:
        return "P0", p0_matches
    p1_matches = sorted(term for term in P1_TERMS if term in joined)
    if p1_matches:
        return "P1", p1_matches
    if labels and all(any(term in label for term in P3_TERMS) for label in labels):
        return "P3", labels
    if any(term in title for term in P3_TERMS) and not any(
        label in {"bug", "feature", "enhancement"} for label in labels
    ):
        return "P3", ["title"]
    return "P2", labels or ["default"]


def _next_actor(
    pr: dict[str, Any],
    ci_status: str,
    merge_status: str,
    review_status: str,
) -> str:
    if bool(pr.get("isDraft")):
        return "OWNER"
    if merge_status == "conflicting" or review_status == "changes_requested":
        return "OWNER"
    if ci_status == "failing":
        return "OWNER"
    if ci_status == "pending":
        return "CI"
    return "MAINTAINER"


def _category(score: int) -> str:
    if score >= 85:
        return "healthy"
    if score >= 70:
        return "active"
    if score >= 60:
        return "watch"
    if score >= 51:
        return "at_risk"
    if score >= 30:
        return "stalled"
    return "abandoned"


def _parse_markers(pr: dict[str, Any]) -> list[dict[str, Any]]:
    """Every workflow-authored lifecycle marker on the PR, v1 and v2 alike.

    Only comments authored by the authenticated identity count, and ``gh pr
    view`` may return a truncated comment page on very busy PRs; a marker that
    falls outside that page reads as absent, which is the conservative
    direction (the ladder restarts at a warning rather than escalating).
    """
    markers: list[dict[str, Any]] = []
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if comment.get("viewerDidAuthor") is not True:
            continue
        body = str(comment.get("body") or "")
        created_at = comment.get("createdAt")
        if not isinstance(created_at, str):
            continue
        for match in MARKER_V2_RE.finditer(body):
            markers.append(
                {
                    "version": 2,
                    "epoch": int(match.group(1)),
                    "stage": STAGE_ALIASES[match.group(2)],
                    "score": int(match.group(3)),
                    "as_of": match.group(4),
                    "created_at": created_at,
                }
            )
        for match in MARKER_V1_RE.finditer(body):
            markers.append(
                {
                    "version": 1,
                    "epoch": 0,
                    "stage": STAGE_ALIASES[match.group(1)],
                    "score": int(match.group(2)),
                    "as_of": match.group(3),
                    "created_at": created_at,
                }
            )
        for match in ESCALATION_MARKER_V1_RE.finditer(body):
            markers.append(
                {
                    "version": 1,
                    "epoch": 0,
                    "stage": "escalation",
                    "score": int(match.group(1)),
                    "as_of": match.group(2),
                    "created_at": created_at,
                }
            )
    return markers


def _lifecycle(pr: dict[str, Any]) -> dict[str, Any]:
    """Summarize the PR's notification history as epochs and a current stage.

    The current epoch is the highest epoch number present; within it the
    furthest-advanced stage owns the grace period, and the newest marker of
    that stage anchors it. Exact ``(epoch, stage)`` dedup means at most one
    marker per pair in normal operation, so "newest" only matters when a
    duplicate exists — and then the newest is the least-noise choice, because
    it lengthens the wait rather than shortening it.
    """
    markers = _parse_markers(pr)
    if not markers:
        return {
            "current": None,
            "current_epoch": 0,
            "epoch_count": 0,
            "escalation_count": 0,
            "epochs": [],
        }
    epochs = sorted({int(marker["epoch"]) for marker in markers})
    current_epoch = epochs[-1]
    in_epoch = [marker for marker in markers if int(marker["epoch"]) == current_epoch]
    current = max(
        in_epoch, key=lambda item: (STAGE_RANK[item["stage"]], item["created_at"])
    )
    return {
        "current": current,
        "current_epoch": current_epoch,
        "epoch_count": len(epochs),
        "escalation_count": sum(
            1 for marker in markers if marker["stage"] == "escalation"
        ),
        "epochs": epochs,
    }


def _lifecycle_marker(pr: dict[str, Any]) -> dict[str, Any] | None:
    """The single marker that owns the PR's current grace period, or ``None``."""
    return _lifecycle(pr)["current"]


def _latest_owner_activity_after(pr: dict[str, Any], marker_at: str) -> str | None:
    activity_dates = []
    owner = _login(pr.get("author"))
    latest_commit_at = _latest_commit_at(pr)
    if latest_commit_at > marker_at:
        activity_dates.append(latest_commit_at)
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if _login(comment.get("author")) != owner:
            continue
        created_at = comment.get("createdAt")
        if isinstance(created_at, str) and created_at > marker_at:
            activity_dates.append(created_at)
    return max(activity_dates) if activity_dates else None


def _owner_responded_after(pr: dict[str, Any], marker_at: str, as_of: str) -> bool:
    """Did the owner answer ``marker_at`` recently enough to hold the epoch open?"""
    activity_at = _latest_owner_activity_after(pr, marker_at)
    return (
        activity_at is not None
        and _days_between(activity_at, as_of) < OWNER_RESPONSE_GRACE_DAYS
    )


def _observation_streak(
    previous: dict[str, Any] | None,
    as_of: str,
    score: int,
    next_actor: str,
) -> int:
    qualifies = score < HEALTHY_SCORE and next_actor == "OWNER"
    if not qualifies:
        return 0
    if not previous:
        return 1
    previous_as_of = previous.get("last_as_of")
    if not isinstance(previous_as_of, str):
        return 1
    try:
        stale = _date_ordinal(as_of) < _date_ordinal(previous_as_of)
    except ValueError:
        # Unparsable persisted date (hand-edited or written by a future
        # schema): restart this PR's streak rather than aborting the run.
        return 1
    if stale:
        # Backfill, clock skew, or a reopened PR carrying old state. Restarting
        # this PR's streak is the conservative direction — it delays the first
        # warning by one observation instead of aborting scoring for every
        # other PR in the run.
        return 1
    if as_of == previous_as_of:
        return int(previous.get("below60_owner_streak") or 1)
    previous_score = previous.get("last_score")
    prior_qualified = (
        int(100 if previous_score is None else previous_score) < HEALTHY_SCORE
        and previous.get("last_next_actor") == "OWNER"
    )
    return int(previous.get("below60_owner_streak") or 0) + 1 if prior_qualified else 1


def _fresh_ladder(
    score: int,
    next_actor: str,
    streak: int,
    reasons: list[str],
    epoch: int,
) -> tuple[int, str, list[str], int]:
    """The start of a notification epoch. Every epoch begins at ``warn_owner``.

    Re-entry deliberately restarts here rather than resuming mid-ladder: a PR
    that has been silent for a full cooldown gets the same first notice a
    never-notified PR would, and no epoch can open on an escalation or a
    closure recommendation.
    """
    if next_actor == "CI":
        return score, "await_ci", reasons, epoch
    if next_actor != "OWNER":
        return score, "alert_maintainers", reasons, epoch
    if streak >= 2:
        return score, "warn_owner", reasons, epoch
    return score, "observe_again", reasons, epoch


def _reenter_or_hold(
    lifecycle: dict[str, Any],
    anchor_at: str,
    as_of: str,
    score: int,
    next_actor: str,
    streak: int,
    reasons: list[str],
) -> tuple[int, str, list[str], int]:
    """Decide whether a settled epoch may reopen as the next one.

    ``anchor_at`` is whichever came last: the terminal advisory that closed the
    epoch, or the owner activity that answered it. Anchoring on the response is
    what stops a PR the owner engaged with from being re-warned on the terminal
    marker's older clock.
    """
    cooldown_age = _days_between(anchor_at, as_of)
    current_epoch = int(lifecycle["current_epoch"])
    reasons.append(f"epoch_settled_age={cooldown_age}")
    if cooldown_age < EPOCH_REENTRY_COOLDOWN_DAYS:
        reasons.append("awaiting_epoch_reentry_cooldown")
        return score, "await_owner_deadline", reasons, current_epoch
    if int(lifecycle["epoch_count"]) >= MAX_NOTIFICATION_EPOCHS:
        reasons.append("notification_epoch_cap_reached")
        return score, REPORT_ONLY_RECOMMENDATION, reasons, current_epoch
    next_epoch = current_epoch + 1
    reasons.append(f"epoch_reentry={next_epoch}")
    return _fresh_ladder(score, next_actor, streak, reasons, next_epoch)


def _escalate_or_report_only(
    lifecycle: dict[str, Any],
    score: int,
    reasons: list[str],
    epoch: int,
) -> tuple[int, str, list[str], int]:
    if int(lifecycle["escalation_count"]) >= MAX_ESCALATION_NOTIFICATIONS:
        reasons.append("escalation_cap_reached")
        return score, REPORT_ONLY_RECOMMENDATION, reasons, epoch
    return score, "escalate_protected_pr", reasons, epoch


def _recommend_action(
    pr: dict[str, Any],
    raw_score: int,
    priority: str,
    next_actor: str,
    lifecycle: dict[str, Any],
    streak: int,
    as_of: str,
) -> tuple[int, str, list[str], int]:
    """Return ``(score, action, reasons, notification_epoch)``.

    Every branch that observes a marker RETURNS. There is deliberately no fall
    through into the fresh ladder: that fall-through was how a warned PR came
    back around to ``warn_owner``, and how a PR scoring 51-59 matched no
    advancement branch at all and stalled forever one stage short of the ladder
    it had already entered.
    """
    score = raw_score
    reasons: list[str] = []
    healthy = raw_score >= HEALTHY_SCORE
    protected = (
        priority in {"P0", "P1"}
        or str(pr.get("reviewDecision") or "").upper() == "APPROVED"
    )
    current = lifecycle["current"]
    epoch = int(lifecycle["current_epoch"])

    if current is None:
        if healthy:
            return score, "none", reasons, epoch
        return _fresh_ladder(score, next_actor, streak, reasons, epoch)

    marker_age = _days_between(current["created_at"], as_of)
    stage = str(current["stage"])
    reasons.append(f"epoch={epoch}")
    reasons.append(f"{stage}_marker_age={marker_age}")
    owner_activity_at = _latest_owner_activity_after(pr, current["created_at"])

    if _owner_responded_after(pr, current["created_at"], as_of):
        reasons.append("owner_responded")
        return score, "monitor_response", reasons, epoch
    if healthy:
        reasons.append("recovered_above_notification_threshold")
        return score, "none", reasons, epoch
    if owner_activity_at is not None:
        # The owner did engage, so this epoch is settled on their answer rather
        # than on the advisory. It expires into a cooldown, NOT into the next
        # rung: escalating a PR whose owner replied — merely late — is the
        # single loudest thing this workflow could do.
        reasons.append("owner_response_epoch_expired")
        return _reenter_or_hold(
            lifecycle, owner_activity_at, as_of, score, next_actor, streak, reasons
        )
    if stage in TERMINAL_STAGES:
        reasons.append("epoch_closed_by_terminal_stage")
        return _reenter_or_hold(
            lifecycle, current["created_at"], as_of, score, next_actor, streak, reasons
        )

    if stage == "warning":
        if marker_age < WARNING_GRACE_DAYS:
            reasons.append("awaiting_warning_grace_period")
            return score, "await_owner_deadline", reasons, epoch
        if protected:
            return _escalate_or_report_only(lifecycle, score, reasons, epoch)
        if bool(pr.get("isDraft")):
            return score, "second_owner_notification", reasons, epoch
        return score, "propose_draft", reasons, epoch

    # ``stage`` can only be ``draft_recommendation`` here: STAGE_ALIASES maps
    # every readable marker onto warning / draft_recommendation / a terminal
    # stage, and the two others returned above.
    if marker_age < DRAFT_GRACE_DAYS:
        reasons.append("awaiting_draft_recommendation_grace_period")
        return score, "await_owner_deadline", reasons, epoch
    # The penalty is a statement about an unanswered advisory, so it only
    # applies while the PR is still insufficiently healthy — a recovered PR
    # returned "none" above and never reaches it.
    score = max(0, raw_score - STALE_DRAFT_PENALTY)
    reasons.append(f"unanswered_draft_recommendation=-{STALE_DRAFT_PENALTY}")
    if protected:
        return _escalate_or_report_only(lifecycle, score, reasons, epoch)
    return score, "propose_close", reasons, epoch


def _score_pr(
    pr: dict[str, Any],
    as_of: str,
    previous: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    number = pr.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        raise ValueError("PR number must be an integer")
    last_commit_at = _latest_commit_at(pr)
    idle_days = _days_between(last_commit_at, as_of)
    ci_points, ci_status = _ci_component(pr)
    merge_points, merge_status = _merge_component(pr)
    review_points, review_status = _review_component(pr)
    engagement_points = _engagement_component(idle_days)
    completeness_points, completeness_details = _completeness_component(pr)
    raw_score = (
        ci_points
        + merge_points
        + review_points
        + engagement_points
        + completeness_points
    )
    priority, priority_evidence = _priority(pr)
    next_actor = _next_actor(pr, ci_status, merge_status, review_status)
    lifecycle = _lifecycle(pr)
    streak = _observation_streak(previous, as_of, raw_score, next_actor)
    score, action, action_reasons, notification_epoch = _recommend_action(
        pr,
        raw_score,
        priority,
        next_actor,
        lifecycle,
        streak,
        as_of,
    )
    marker = lifecycle["current"]
    result = {
        "number": number,
        "title": str(pr.get("title") or ""),
        "url": str(pr.get("url") or ""),
        "owner": _login(pr.get("author")),
        "is_draft": bool(pr.get("isDraft")),
        "last_commit_at": last_commit_at,
        "idle_days": idle_days,
        "score": score,
        "raw_score": raw_score,
        "category": _category(score),
        "priority": priority,
        "priority_evidence": priority_evidence,
        "next_actor": next_actor,
        "recommended_action": action,
        "action_reasons": action_reasons,
        "below60_owner_streak": streak,
        "notification_epoch": notification_epoch,
        "lifecycle_marker": marker,
        "notification_epochs_used": lifecycle["epoch_count"],
        "escalations_used": lifecycle["escalation_count"],
        "components": {
            "ci": {"points": ci_points, "status": ci_status},
            "mergeability": {"points": merge_points, "status": merge_status},
            "review": {"points": review_points, "status": review_status},
            "engagement": {"points": engagement_points, "idle_days": idle_days},
            "completeness": {
                "points": completeness_points,
                **completeness_details,
            },
            "unanswered_draft_recommendation": score - raw_score,
        },
    }
    next_state = {
        "last_as_of": as_of,
        # The RAW score, never the penalized one: persisting the penalty would
        # re-apply it on the next run, compounding a one-time -25 into a drift
        # that eventually reads as "abandoned" on its own.
        "last_score": raw_score,
        "last_next_actor": next_actor,
        "below60_owner_streak": streak,
    }
    return result, next_state


def _fetch_snapshot(
    repo: str, as_of: str, snapshot_id: str, max_prs: int
) -> dict[str, Any]:
    rows = _run_gh(
        [
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--limit",
            str(max_prs + 1),
            "--json",
            "number",
        ]
    )
    if not isinstance(rows, list):
        raise RuntimeError("gh pr list did not return a JSON list")
    if len(rows) > max_prs:
        raise RuntimeError(f"open PR count exceeds max_prs={max_prs}")

    prs = []
    numbers = sorted(int(row["number"]) for row in rows)
    for number in numbers:
        value = _run_gh(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                PR_FIELDS,
            ]
        )
        if not isinstance(value, dict):
            raise RuntimeError(f"gh pr view {number} did not return an object")
        prs.append(value)
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "as_of": as_of,
        "snapshot_id": snapshot_id,
        "prs": prs,
    }


def _render_report(
    repo: str, as_of: str, scores: list[dict[str, Any]], mode: str
) -> str:
    lines = [
        "# PR Health Report",
        "",
        f"- Repository: `{repo}`",
        f"- As of: `{as_of}`",
        f"- Open PRs: {len(scores)}",
        f"- Scoring: deterministic schema v{SCHEMA_VERSION}",
        f"- Markers: `cao-pr-health:v{MARKER_SCHEMA_VERSION}` (v1 markers read as epoch 0)",
        "",
        "| PR | Score | Health | Priority | Idle | Next actor | Epoch | Recommendation |",
        "|---:|---:|---|---|---:|---|---:|---|",
    ]
    for item in sorted(scores, key=lambda value: (value["score"], value["number"])):
        lines.append(
            "| "
            f"[#{item['number']}]({item['url']}) | {item['score']} | "
            f"{item['category']} | {item['priority']} | {item['idle_days']}d | "
            f"{item['next_actor']} | {item['notification_epoch']} | "
            f"{item['recommended_action']} |"
        )
    lines.extend(
        [
            "",
            "## Score Rules",
            "",
            "- CI: 20 passing, 12 pending, 5 missing, 0 failing.",
            "- Mergeability: 15 clean, 10 blocked/behind, 5 unknown, 0 conflicting.",
            "- Review: 15 approved, 10 review required, 5 draft, 0 changes requested.",
            "- Engagement: 40/32/24/16/8/0 across <=3/7/14/21/30/>30 idle days.",
            "- Completeness: 3 description, 2 rationale, 3 tests, 2 focused scope.",
            (
                "- Leaving a draft recommendation unanswered for "
                f"{DRAFT_GRACE_DAYS} days applies -{STALE_DRAFT_PENALTY}, "
                f"and only while the score is under {HEALTHY_SCORE}."
            ),
            "",
            "## Notification Policy",
            "",
            (
                f"- Ladder: warning -> draft recommendation -> terminal advisory, with "
                f"{WARNING_GRACE_DAYS} and {DRAFT_GRACE_DAYS} day grace periods."
            ),
            (
                "- A terminal advisory (closure recommendation or escalation) closes the "
                "notification epoch."
            ),
            (
                f"- Owner activity answers the epoch for {OWNER_RESPONSE_GRACE_DAYS} days; "
                "an expired response never escalates, it settles the epoch."
            ),
            (
                f"- A closed epoch may reopen only {EPOCH_REENTRY_COOLDOWN_DAYS} days after "
                "its terminal advisory or the owner's last answer, whichever is later, "
                "and always restarts at a warning."
            ),
            (
                f"- Lifetime caps: {MAX_NOTIFICATION_EPOCHS} notification epochs and "
                f"{MAX_ESCALATION_NOTIFICATIONS} escalations per PR. At either cap the "
                "decision becomes `report_only` and no comment is posted."
            ),
            "",
            (
                "Dry-run mode: this report does not mutate GitHub."
                if mode == "dry_run"
                else "Apply mode: eligible comments are live-revalidated before posting."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _importance_prompt(
    report_path: Path, scores_path: Path, repo: str, as_of: str
) -> str:
    return f"""Review the deterministic PR health artifacts for {repo} as of {as_of}.

Read:
- {report_path}
- {scores_path}

Produce a concise maintainer synthesis grouped by:
1. protected P0/P1 PRs needing escalation or adoption,
2. owner-blocked PRs needing attention,
3. maintainer-blocked PRs,
4. closure candidates and the evidence supporting them.

The score, category, next_actor, priority, and recommended_action fields are
authoritative rule outputs. Do not recalculate, override, or invent scores.
Call out uncertain importance classifications as advisory. Return Markdown only.
Do not modify files or GitHub."""


def _action_marker(action: str, epoch: int, score: int, as_of: str) -> str:
    stage = ACTION_STAGES.get(action)
    if stage is None:
        raise ValueError(f"unsupported notification action: {action}")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or not 0 <= epoch <= 999:
        raise ValueError("notification epoch must be an integer from 0 through 999")
    return (
        f"<!-- cao-pr-health:v{MARKER_SCHEMA_VERSION} epoch={epoch} "
        f"stage={stage} score={score} as_of={as_of} -->"
    )


def _blocker_lines(item: dict[str, Any]) -> list[str]:
    components = item["components"]
    return [
        f"- CI: {components['ci']['status']}",
        f"- Mergeability: {components['mergeability']['status']}",
        f"- Review: {components['review']['status']}",
        f"- Last commit: {item['idle_days']} days ago",
    ]


def _comment_body(item: dict[str, Any], as_of: str) -> str:
    action = item["recommended_action"]
    owner = item["owner"]
    score = item["score"]
    blockers = "\n".join(_blocker_lines(item))
    marker = _action_marker(
        action, int(item.get("notification_epoch") or 0), score, as_of
    )

    if action == "warn_owner":
        message = f"""@{owner} This PR's automated health score is **{score}/100** and needs attention.

Current signals:
{blockers}

Please push an update or reply with your plan within 7 days. The score will be recalculated before any further notification."""
    elif action == "propose_draft":
        message = f"""@{owner} This PR's automated health score is now **{score}/100**. The previous notification has been open for at least 7 days without new activity.

Current signals:
{blockers}

Maintainers may consider moving this PR to draft while the outstanding issues are addressed. Please push an update or reply with a concrete plan within 14 days."""
    elif action == "second_owner_notification":
        message = f"""@{owner} This draft PR's automated health score is **{score}/100**. The previous notification has been open for at least 7 days without new activity.

Current signals:
{blockers}

Please push an update or reply with a concrete plan within 14 days."""
    elif action == "propose_close":
        message = f"""@{owner} This PR's automated health score is **{score}/100** after the warning and draft grace periods.

Current signals:
{blockers}

The PR remains blocked and no owner activity was detected. Maintainers may consider closing it to keep the active backlog current."""
    elif action == "escalate_protected_pr":
        protections = []
        if item["priority"] in {"P0", "P1"}:
            protections.append(f"priority {item['priority']}")
        if item["components"]["review"]["status"] == "approved":
            protections.append("approved review state")
        protection_text = " and ".join(protections) or "protected status"
        message = f"""@{owner} This PR's automated health score is **{score}/100** and requires attention.

Current signals:
{blockers}

This PR has {protection_text}, so maintainer escalation is requested. Please push an update or reply with the intended next step."""
    else:
        raise ValueError(f"unsupported notification action: {action}")

    return f"{COMMENT_HEADER}\n\n{COMMENT_ONLY_DISCLAIMER}\n\n{message}\n\n{marker}"


def _has_marker_for_stage(pr: dict[str, Any], stage: str, epoch: int) -> bool:
    """Has this workflow identity already posted ``stage`` in ``epoch``?

    Dedupe is EXACT on the ``(epoch, stage)`` pair, never on marker text and
    never on a date window. Text comparison always missed, because the marker
    embeds the emitting run's own score and date. A date window was worse: it
    expired, so a spent stage became postable again inside its own epoch, which
    is precisely the repeat notification the epoch model exists to prevent.
    Re-notifying requires a NEW epoch, and opening one requires the cooldown.
    """
    for marker in _parse_markers(pr):
        if marker["stage"] == stage and int(marker["epoch"]) == int(epoch):
            return True
    return False


def _has_marker_for_action(pr: dict[str, Any], action: str, epoch: int) -> bool:
    stage = ACTION_STAGES.get(action)
    if stage is None:
        raise ValueError(f"unsupported notification action: {action}")
    return _has_marker_for_stage(pr, stage, epoch)


def _fetch_pr(repo: str, number: int) -> dict[str, Any]:
    value = _run_gh(
        [
            "pr",
            "view",
            str(number),
            "--repo",
            repo,
            "--json",
            PR_FIELDS,
        ]
    )
    if not isinstance(value, dict):
        raise RuntimeError(f"gh pr view {number} did not return an object")
    return value


def _apply_recommendations(
    repo: str,
    as_of: str,
    scores: list[dict[str, Any]],
    persisted_state: dict[str, Any],
    journal_path: Path,
) -> list[dict[str, Any]]:
    results = []
    state_by_pr = persisted_state.get("prs") or {}

    for planned in sorted(scores, key=lambda item: int(item["number"])):
        action = str(planned["recommended_action"])
        if action not in ACTIONABLE_RECOMMENDATIONS:
            continue
        number = int(planned["number"])
        epoch = int(planned.get("notification_epoch") or 0)
        result: dict[str, Any] = {
            "number": number,
            "planned_action": action,
            "notification_epoch": epoch,
            "status": "pending",
        }
        try:
            live_pr = _fetch_pr(repo, number)
            if live_pr.get("state") != "OPEN":
                result["status"] = "skipped_not_open"
                results.append(result)
                _write_json(
                    journal_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "repo": repo,
                        "as_of": as_of,
                        "results": results,
                    },
                )
                continue
            if _has_marker_for_action(live_pr, action, epoch):
                # The replay/resume guard. Resume re-executes this script
                # top-to-bottom, so the marker already on the PR — not any
                # in-process bookkeeping — is what makes a second pass silent.
                result["status"] = "already_applied"
                results.append(result)
                _write_json(
                    journal_path,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "repo": repo,
                        "as_of": as_of,
                        "results": results,
                    },
                )
                continue
            live_score, _ = _score_pr(
                live_pr,
                as_of,
                state_by_pr.get(str(number)),
            )
            if (
                live_score["score"] != planned["score"]
                or live_score["recommended_action"] != action
                or int(live_score["notification_epoch"]) != epoch
            ):
                result.update(
                    {
                        "status": "skipped_live_drift",
                        "live_score": live_score["score"],
                        "live_recommendation": live_score["recommended_action"],
                        "live_notification_epoch": live_score["notification_epoch"],
                    }
                )
            else:
                body = _comment_body(planned, as_of)
                _run_gh_command(
                    ["pr", "comment", str(number), "--repo", repo, "--body", body]
                )
                result["status"] = "commented"
        except Exception as exc:
            result.update({"status": "error", "error": str(exc)})
        results.append(result)
        _write_json(
            journal_path,
            {
                "schema_version": SCHEMA_VERSION,
                "repo": repo,
                "as_of": as_of,
                "results": results,
            },
        )
    return results


def _migrate_state(state: dict[str, Any], repo: str) -> dict[str, Any]:
    """Accept any supported persisted-state schema and return it at the current one.

    Rejecting an older schema would abort every run for the whole repository
    until an operator deleted the file by hand — a much worse failure than
    carrying forward four scalar fields whose meaning did not change. The v1
    ``last_score`` was the penalized score rather than the raw one; carrying it
    forward can only shorten a streak by one observation, which is the quiet
    direction.
    """
    version = state.get("schema_version")
    if version not in SUPPORTED_STATE_SCHEMA_VERSIONS:
        raise ValueError("persisted PR health state has an unsupported schema")
    if state.get("repo") != repo or not isinstance(state.get("prs"), dict):
        raise ValueError("persisted PR health state has an unsupported schema")
    return {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "prs": dict(state["prs"]),
    }


def _decision_record(item: dict[str, Any], notified: bool) -> dict[str, Any]:
    """One PR's structured decision, small enough to live in the run output."""
    marker = item.get("lifecycle_marker")
    return {
        "number": item["number"],
        "url": item["url"],
        "score": item["score"],
        "raw_score": item["raw_score"],
        "category": item["category"],
        "priority": item["priority"],
        "next_actor": item["next_actor"],
        "recommended_action": item["recommended_action"],
        "notification_epoch": item["notification_epoch"],
        "marker_stage": marker["stage"] if marker else None,
        "marker_epoch": marker["epoch"] if marker else None,
        "notification_epochs_used": item["notification_epochs_used"],
        "escalations_used": item["escalations_used"],
        "action_reasons": list(item["action_reasons"]),
        "notified": notified,
    }


POLICY = {
    "warning_grace_days": WARNING_GRACE_DAYS,
    "draft_grace_days": DRAFT_GRACE_DAYS,
    "owner_response_grace_days": OWNER_RESPONSE_GRACE_DAYS,
    "epoch_reentry_cooldown_days": EPOCH_REENTRY_COOLDOWN_DAYS,
    "max_notification_epochs": MAX_NOTIFICATION_EPOCHS,
    "max_escalation_notifications": MAX_ESCALATION_NOTIFICATIONS,
}


def _run_locked(inputs: dict[str, Any]) -> None:
    repo = str(inputs.get("repo") or "").strip()
    as_of = str(inputs.get("as_of") or "").strip()
    snapshot_id = str(inputs.get("snapshot_id") or "").strip()
    max_prs = inputs.get("max_prs", 500)
    importance_analysis = inputs.get("importance_analysis", True)
    importance_provider = str(inputs.get("importance_provider") or "").strip()
    importance_agent = str(inputs.get("importance_agent") or "").strip()
    mode = str(inputs.get("mode") or "").strip()

    if repo.count("/") != 1 or any(not part for part in repo.split("/")):
        raise ValueError("repo must be in owner/name form")
    _validate_as_of(as_of)
    if not SAFE_ID_RE.fullmatch(snapshot_id) or snapshot_id in RESERVED_IDS:
        raise ValueError(
            "snapshot_id may contain only letters, numbers, dot, underscore, and dash, "
            "and may not be '.' or '..'"
        )
    if (
        isinstance(max_prs, bool)
        or not isinstance(max_prs, int)
        or not 1 <= max_prs <= 2000
    ):
        raise ValueError("max_prs must be an integer from 1 through 2000")
    if not isinstance(importance_analysis, bool):
        raise ValueError("importance_analysis must be boolean")
    if not SAFE_ID_RE.fullmatch(importance_provider):
        raise ValueError("importance_provider must be a nonempty safe identifier")
    if not SAFE_ID_RE.fullmatch(importance_agent):
        raise ValueError("importance_agent must be a nonempty safe identifier")
    if mode not in {"dry_run", "apply"}:
        raise ValueError("mode must be dry_run or apply")

    repo_key = _repo_storage_key(repo)
    root = Path.home() / ".local" / "state" / "cao" / "pr-health" / repo_key
    artifact_dir = root / "runs" / snapshot_id
    snapshot_path = artifact_dir / "snapshot.json"
    scores_path = artifact_dir / "scores.json"
    report_path = artifact_dir / "report.md"
    analysis_path = artifact_dir / "importance-analysis.md"
    decisions_path = artifact_dir / "decisions.json"
    enforcement_path = artifact_dir / "enforcement.json"
    manifest_path = artifact_dir / "manifest.json"
    state_path = root / "state.json"

    if manifest_path.is_file():
        manifest = _read_object(manifest_path)
        if (
            manifest.get("repo") != repo
            or manifest.get("as_of") != as_of
            or manifest.get("snapshot_id") != snapshot_id
            or manifest.get("mode") != mode
        ):
            raise ValueError("snapshot_id already exists with different inputs")
        emit_output(manifest)
        return

    artifact_dir.mkdir(parents=True, exist_ok=True)
    if snapshot_path.is_file():
        snapshot = _read_object(snapshot_path)
        if (
            snapshot.get("repo") != repo
            or snapshot.get("as_of") != as_of
            or snapshot.get("snapshot_id") != snapshot_id
        ):
            raise ValueError("existing snapshot does not match requested inputs")
    else:
        snapshot = _fetch_snapshot(repo, as_of, snapshot_id, max_prs)
        _write_json(snapshot_path, snapshot)

    state = _migrate_state(
        _read_object(state_path)
        if state_path.is_file()
        else {"schema_version": SCHEMA_VERSION, "repo": repo, "prs": {}},
        repo,
    )

    scores = []
    next_pr_state = dict(state["prs"])
    for pr in sorted(snapshot.get("prs") or [], key=lambda value: int(value["number"])):
        number_key = str(pr["number"])
        previous = state["prs"].get(number_key)
        if previous is not None and not isinstance(previous, dict):
            raise ValueError(f"persisted state for PR #{number_key} is invalid")
        score, pr_state = _score_pr(pr, as_of, previous)
        scores.append(score)
        next_pr_state[number_key] = pr_state

    open_numbers = {str(item["number"]) for item in scores}
    next_pr_state = {
        number: value
        for number, value in next_pr_state.items()
        if number in open_numbers
    }
    updated_state = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "prs": next_pr_state,
    }
    _write_json(state_path, updated_state)
    _write_json(
        scores_path,
        {
            "schema_version": SCHEMA_VERSION,
            "repo": repo,
            "as_of": as_of,
            "snapshot_id": snapshot_id,
            "scores": scores,
        },
    )
    report_path.write_text(
        _render_report(repo, as_of, scores, mode),
        encoding="utf-8",
    )

    analysis_error = None
    if importance_analysis:
        try:
            handle = run_step(
                importance_provider,
                importance_agent,
                _importance_prompt(report_path, scores_path, repo, as_of),
                step_id=f"importance-{snapshot_id}",
                timeout=1800.0,
            )
            analysis_path.write_text(
                f"{(handle.output or '').strip()}\n", encoding="utf-8"
            )
        except ShimError as exc:
            analysis_error = str(exc)

    enforcement_results = []
    if mode == "apply":
        enforcement_results = _apply_recommendations(
            repo,
            as_of,
            scores,
            updated_state,
            enforcement_path,
        )

    actions: dict[str, int] = {}
    for item in scores:
        actions[item["recommended_action"]] = (
            actions.get(item["recommended_action"], 0) + 1
        )
    notified_numbers = {
        int(result["number"])
        for result in enforcement_results
        if result.get("status") == "commented"
    }
    decisions = [
        _decision_record(item, int(item["number"]) in notified_numbers)
        for item in sorted(scores, key=lambda value: int(value["number"]))
    ]
    _write_json(
        decisions_path,
        {
            "schema_version": SCHEMA_VERSION,
            "repo": repo,
            "as_of": as_of,
            "snapshot_id": snapshot_id,
            "mode": mode,
            "policy": dict(POLICY),
            "decisions": decisions,
        },
    )
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "marker_schema_version": MARKER_SCHEMA_VERSION,
        "repo": repo,
        "as_of": as_of,
        "snapshot_id": snapshot_id,
        "mode": mode,
        "open_prs": len(scores),
        "actions": dict(sorted(actions.items())),
        "policy": dict(POLICY),
        "artifact_dir": str(artifact_dir),
        "snapshot_file": str(snapshot_path),
        "scores_file": str(scores_path),
        "report_file": str(report_path),
        "decisions_file": str(decisions_path),
        # The journal stores the run output verbatim, so the inline copy is
        # bounded; ``decisions_file`` always holds the complete list.
        "decisions": decisions[:MAX_INLINE_DECISIONS],
        "decisions_truncated": len(decisions) > MAX_INLINE_DECISIONS,
        "importance_analysis_file": (
            str(analysis_path) if analysis_path.is_file() else None
        ),
        "importance_analysis_error": analysis_error,
        "enforcement_file": (
            str(enforcement_path) if enforcement_path.is_file() else None
        ),
        "enforcement_results": enforcement_results,
        "posted_github_comments": any(
            result.get("status") == "commented" for result in enforcement_results
        ),
    }
    _write_json(manifest_path, manifest)
    emit_output(manifest)


def _acquire_repo_lock(repo: str) -> tuple[TextIO, Path]:
    root = (
        Path.home() / ".local" / "state" / "cao" / "pr-health" / _repo_storage_key(repo)
    )
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".workflow.lock"
    lock_handle = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
    return lock_handle, lock_path


def main() -> None:
    inputs = get_inputs()
    repo = str(inputs.get("repo") or "").strip()
    if repo.count("/") != 1 or any(not part for part in repo.split("/")):
        raise ValueError("repo must be in owner/name form")

    lock_handle, _ = _acquire_repo_lock(repo)
    try:
        _run_locked(inputs)
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


if __name__ == "__main__":
    main()
