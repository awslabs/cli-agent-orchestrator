"""Deterministic health analysis and guarded enforcement for open GitHub PRs.

Dry-run mode snapshots GitHub data, calculates scores with fixed rules, persists
the two-observation warning state, and asks a reviewer for an advisory importance
synthesis. Apply mode revalidates every candidate against live data before making
an idempotent comment or state change. Closure also requires an explicit allowlist.

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
    "close_allowlist": {
        "type": "string",
        "required": False,
        "default": "",
    },
}

SCHEMA_VERSION = 1
MARKER_RE = re.compile(
    r"<!--\s*cao-pr-health:v1\s+"
    r"stage=(warning|draft)\s+score=(\d{1,3})\s+"
    r"as_of=(\d{4}-\d{2}-\d{2})\s*-->"
)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")
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
ACTIONABLE_RECOMMENDATIONS = {
    "escalate_protected_pr",
    "propose_close",
    "propose_draft",
    "second_owner_notification",
    "warn_owner",
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


def _run_gh(args: list[str]) -> Any:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown gh error"
        raise RuntimeError(f"gh command failed ({completed.returncode}): {detail}")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh command did not return valid JSON") from exc


def _run_gh_command(args: list[str]) -> str:
    completed = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown gh error"
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
        elif state in PENDING_STATES or not conclusion and state not in SUCCESS_CONCLUSIONS:
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
    linked_issue = bool(pr.get("closingIssuesReferences")) or bool(ISSUE_REF_RE.search(body))
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


def _latest_marker(pr: dict[str, Any]) -> dict[str, Any] | None:
    markers = []
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if comment.get("viewerDidAuthor") is not True:
            continue
        body = str(comment.get("body") or "")
        created_at = comment.get("createdAt")
        if not isinstance(created_at, str):
            continue
        for match in MARKER_RE.finditer(body):
            markers.append(
                {
                    "stage": match.group(1),
                    "score": int(match.group(2)),
                    "as_of": match.group(3),
                    "created_at": created_at,
                }
            )
    return max(markers, key=lambda item: item["created_at"]) if markers else None


def _owner_responded_after(pr: dict[str, Any], marker_at: str) -> bool:
    owner = _login(pr.get("author"))
    if _latest_commit_at(pr) > marker_at:
        return True
    for comment in pr.get("comments") or []:
        if not isinstance(comment, dict):
            continue
        if _login(comment.get("author")) != owner:
            continue
        created_at = comment.get("createdAt")
        if isinstance(created_at, str) and created_at > marker_at:
            return True
    return False


def _observation_streak(
    previous: dict[str, Any] | None,
    as_of: str,
    score: int,
    next_actor: str,
) -> int:
    qualifies = score < 60 and next_actor == "OWNER"
    if not qualifies:
        return 0
    if not previous:
        return 1
    previous_as_of = previous.get("last_as_of")
    if not isinstance(previous_as_of, str):
        return 1
    if _date_ordinal(as_of) < _date_ordinal(previous_as_of):
        raise ValueError("as_of predates the persisted PR health state")
    if as_of == previous_as_of:
        return int(previous.get("below60_owner_streak") or 1)
    prior_qualified = (
        int(previous.get("last_score") or 100) < 60 and previous.get("last_next_actor") == "OWNER"
    )
    return int(previous.get("below60_owner_streak") or 0) + 1 if prior_qualified else 1


def _recommend_action(
    pr: dict[str, Any],
    raw_score: int,
    priority: str,
    next_actor: str,
    marker: dict[str, Any] | None,
    streak: int,
    as_of: str,
) -> tuple[int, str, list[str]]:
    score = raw_score
    reasons = []
    protected = (
        priority in {"P0", "P1"} or str(pr.get("reviewDecision") or "").upper() == "APPROVED"
    )

    if marker:
        marker_age = _days_between(marker["created_at"], as_of)
        responded = _owner_responded_after(pr, marker["created_at"])
        reasons.append(f"{marker['stage']}_marker_age={marker_age}")
        if responded:
            reasons.append("owner_responded")
            return score, "monitor_response", reasons
        if marker["stage"] == "draft" and marker_age >= 14:
            score = max(0, score - 25)
            reasons.append("ignored_intervention=-25")
            if score < 30:
                if protected:
                    return score, "escalate_protected_pr", reasons
                return score, "propose_close", reasons
        if marker["stage"] == "warning" and marker_age >= 7 and score <= 50:
            if bool(pr.get("isDraft")):
                return score, "second_owner_notification", reasons
            if protected:
                return score, "escalate_protected_pr", reasons
            return score, "propose_draft", reasons

    if score < 60:
        if next_actor == "CI":
            return score, "await_ci", reasons
        if next_actor != "OWNER":
            return score, "alert_maintainers", reasons
        if protected:
            return score, "escalate_protected_pr", reasons
        if streak >= 2:
            return score, "warn_owner", reasons
        return score, "observe_again", reasons
    return score, "none", reasons


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
    raw_score = ci_points + merge_points + review_points + engagement_points + completeness_points
    priority, priority_evidence = _priority(pr)
    next_actor = _next_actor(pr, ci_status, merge_status, review_status)
    marker = _latest_marker(pr)
    streak = _observation_streak(previous, as_of, raw_score, next_actor)
    score, action, action_reasons = _recommend_action(
        pr,
        raw_score,
        priority,
        next_actor,
        marker,
        streak,
        as_of,
    )
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
        "lifecycle_marker": marker,
        "components": {
            "ci": {"points": ci_points, "status": ci_status},
            "mergeability": {"points": merge_points, "status": merge_status},
            "review": {"points": review_points, "status": review_status},
            "engagement": {"points": engagement_points, "idle_days": idle_days},
            "completeness": {
                "points": completeness_points,
                **completeness_details,
            },
            "ignored_intervention": score - raw_score,
        },
    }
    next_state = {
        "last_as_of": as_of,
        "last_score": score,
        "last_next_actor": next_actor,
        "below60_owner_streak": streak,
    }
    return result, next_state


def _fetch_snapshot(repo: str, as_of: str, snapshot_id: str, max_prs: int) -> dict[str, Any]:
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


def _render_report(repo: str, as_of: str, scores: list[dict[str, Any]], mode: str) -> str:
    lines = [
        "# PR Health Report",
        "",
        f"- Repository: `{repo}`",
        f"- As of: `{as_of}`",
        f"- Open PRs: {len(scores)}",
        "- Scoring: deterministic schema v1",
        "",
        "| PR | Score | Health | Priority | Idle | Next actor | Recommendation |",
        "|---:|---:|---|---|---:|---|---|",
    ]
    for item in sorted(scores, key=lambda value: (value["score"], value["number"])):
        lines.append(
            "| "
            f"[#{item['number']}]({item['url']}) | {item['score']} | "
            f"{item['category']} | {item['priority']} | {item['idle_days']}d | "
            f"{item['next_actor']} | {item['recommended_action']} |"
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
            "- Ignoring a draft-stage notification for 14 days applies -25.",
            "",
            (
                "Dry-run mode: this report does not mutate GitHub."
                if mode == "dry_run"
                else "Apply mode: eligible recommendations are live-revalidated before enforcement."
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _importance_prompt(report_path: Path, scores_path: Path, repo: str, as_of: str) -> str:
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


def _action_marker(action: str, score: int, as_of: str) -> str:
    if action == "warn_owner":
        return f"<!-- cao-pr-health:v1 stage=warning score={score} as_of={as_of} -->"
    if action in {"propose_draft", "second_owner_notification"}:
        return f"<!-- cao-pr-health:v1 stage=draft score={score} as_of={as_of} -->"
    if action == "propose_close":
        return f"<!-- cao-pr-health:v1 stage=closed score={score} as_of={as_of} -->"
    if action == "escalate_protected_pr":
        return f"<!-- cao-pr-health:v1 action=escalation score={score} " f"as_of={as_of} -->"
    raise ValueError(f"unsupported enforcement action: {action}")


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
    marker = _action_marker(action, score, as_of)

    if action == "warn_owner":
        message = f"""@{owner} This PR's automated health score is **{score}/100** and needs attention.

Current signals:
{blockers}

No state change is being made now. Please push an update or reply with your plan within 7 days. The score will be recalculated before any further action."""
    elif action == "propose_draft":
        message = f"""@{owner} This PR's automated health score is now **{score}/100**. The previous notification has been open for at least 7 days without new activity.

Current signals:
{blockers}

The PR is being moved to draft while the outstanding issues are addressed. Please push an update or reply with a concrete plan within 14 days. No closure will occur without another evaluation."""
    elif action == "second_owner_notification":
        message = f"""@{owner} This draft PR's automated health score is **{score}/100**. The previous notification has been open for at least 7 days without new activity.

Current signals:
{blockers}

Please push an update or reply with a concrete plan within 14 days. No closure will occur without another evaluation."""
    elif action == "propose_close":
        message = f"""@{owner} This PR's automated health score is **{score}/100** after the warning and draft grace periods.

Current signals:
{blockers}

The PR remains blocked and no owner activity was detected, so it is being closed to keep the active backlog current. This is not a rejection of the proposal. It can be reopened or resubmitted when the work is ready to continue."""
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

This PR is protected from automated draft or closure because of its {protection_text}. Maintainer escalation is requested. Please push an update or reply with the intended next step."""
    else:
        raise ValueError(f"unsupported enforcement action: {action}")

    return f"{message}\n\n{marker}"


def _has_exact_marker(pr: dict[str, Any], marker: str) -> bool:
    return any(
        isinstance(comment, dict)
        and comment.get("viewerDidAuthor") is True
        and marker in str(comment.get("body") or "")
        for comment in pr.get("comments") or []
    )


def _has_marker_for_action(
    pr: dict[str, Any],
    action: str,
    marker: str,
) -> bool:
    if _has_exact_marker(pr, marker):
        return True
    if action != "escalate_protected_pr":
        return False
    return any(
        isinstance(comment, dict)
        and comment.get("viewerDidAuthor") is True
        and "<!-- cao-pr-health:v1 action=escalation " in str(comment.get("body") or "")
        for comment in pr.get("comments") or []
    )


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


def _parse_close_allowlist(value: str) -> set[int]:
    if not value.strip():
        return set()
    result = set()
    for token in value.split(","):
        token = token.strip()
        if not token.isdigit() or int(token) < 1:
            raise ValueError("close_allowlist must be a comma-separated list of PR numbers")
        result.add(int(token))
    return result


def _apply_recommendations(
    repo: str,
    as_of: str,
    scores: list[dict[str, Any]],
    persisted_state: dict[str, Any],
    close_allowlist: set[int],
    journal_path: Path,
) -> list[dict[str, Any]]:
    results = []
    state_by_pr = persisted_state.get("prs") or {}

    for planned in sorted(scores, key=lambda item: int(item["number"])):
        action = str(planned["recommended_action"])
        if action not in ACTIONABLE_RECOMMENDATIONS:
            continue
        number = int(planned["number"])
        result: dict[str, Any] = {
            "number": number,
            "planned_action": action,
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
            marker = _action_marker(action, int(planned["score"]), as_of)
            if _has_marker_for_action(live_pr, action, marker):
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
            ):
                result.update(
                    {
                        "status": "skipped_live_drift",
                        "live_score": live_score["score"],
                        "live_recommendation": live_score["recommended_action"],
                    }
                )
            elif action == "propose_close" and number not in close_allowlist:
                result["status"] = "skipped_closure_not_allowlisted"
            else:
                body = _comment_body(planned, as_of)
                if action == "propose_draft":
                    if not bool(live_pr.get("isDraft")):
                        _run_gh_command(["pr", "ready", str(number), "--repo", repo, "--undo"])
                    _run_gh_command(["pr", "comment", str(number), "--repo", repo, "--body", body])
                    result["status"] = (
                        "commented_existing_draft"
                        if bool(live_pr.get("isDraft"))
                        else "drafted_and_commented"
                    )
                elif action == "propose_close":
                    _run_gh_command(
                        [
                            "pr",
                            "close",
                            str(number),
                            "--repo",
                            repo,
                            "--comment",
                            body,
                        ]
                    )
                    result["status"] = "closed_and_commented"
                else:
                    _run_gh_command(["pr", "comment", str(number), "--repo", repo, "--body", body])
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


def _run_self_tests() -> None:
    assert _days_between("2024-02-28", "2024-03-01") == 2
    assert _days_between("2025-02-28", "2025-03-01") == 1
    assert [_engagement_component(day) for day in (3, 4, 7, 8, 14, 15, 21, 22, 30, 31)] == [
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
    assert [_category(score) for score in (100, 85, 84, 70, 69, 60, 59, 51, 50, 30, 29)] == [
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
    assert sum((20, 15, 15, 40, 10)) == 100
    assert sum((20, 10, 0, 40, 10)) == 80
    assert sum((20, 10, 0, 24, 10)) == 64
    assert sum((20, 10, 0, 16, 10)) == 56
    assert sum((0, 10, 0, 40, 10)) == 60
    assert sum((0, 10, 0, 32, 10)) == 52
    assert sum((0, 10, 0, 24, 10)) == 44
    assert sum((20, 0, 15, 0, 10)) == 45
    assert sum((5, 0, 0, 0, 5)) == 10

    base_pr = {
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
    healthy, _ = _score_pr(base_pr, "2026-07-31", None)
    assert healthy["score"] == 100
    assert healthy["category"] == "healthy"
    assert healthy["next_actor"] == "MAINTAINER"
    assert healthy["recommended_action"] == "none"

    at_risk_pr = {
        **base_pr,
        "reviewDecision": "CHANGES_REQUESTED",
        "mergeStateStatus": "BLOCKED",
        "commits": [{"committedDate": "2026-07-14T00:00:00Z"}],
    }
    at_risk, at_risk_state = _score_pr(at_risk_pr, "2026-07-31", None)
    assert at_risk["score"] == 56
    assert at_risk["recommended_action"] == "observe_again"
    assert at_risk["below60_owner_streak"] == 1

    warning, _ = _score_pr(
        at_risk_pr,
        "2026-08-01",
        {
            **at_risk_state,
            "last_as_of": "2026-07-31",
        },
    )
    assert warning["below60_owner_streak"] == 2
    assert warning["recommended_action"] == "warn_owner"

    warning_marker = "<!-- cao-pr-health:v1 stage=warning score=44 as_of=2026-07-24 -->"
    stalled_pr = {
        **at_risk_pr,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "FAILURE"}],
        "commits": [{"committedDate": "2026-07-23T00:00:00Z"}],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-07-24T00:00:00Z",
                "body": warning_marker,
                "viewerDidAuthor": True,
            }
        ],
    }
    stalled, _ = _score_pr(stalled_pr, "2026-07-31", None)
    assert stalled["score"] == 44
    assert stalled["recommended_action"] == "propose_draft"

    recently_posted_marker_pr = {
        **stalled_pr,
        "comments": [
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-07-30T00:00:00Z",
                "body": warning_marker,
                "viewerDidAuthor": True,
            }
        ],
    }
    recently_marked, _ = _score_pr(recently_posted_marker_pr, "2026-07-31", None)
    assert "warning_marker_age=1" in recently_marked["action_reasons"]
    assert recently_marked["recommended_action"] == "observe_again"

    draft_marker = "<!-- cao-pr-health:v1 stage=draft score=50 as_of=2026-07-17 -->"
    abandoned_pr = {
        **base_pr,
        "isDraft": True,
        "reviewDecision": "REVIEW_REQUIRED",
        "commits": [{"committedDate": "2026-06-01T00:00:00Z"}],
        "comments": [
            {
                "author": {"login": "maintainer"},
                "createdAt": "2026-07-17T00:00:00Z",
                "body": draft_marker,
                "viewerDidAuthor": True,
            }
        ],
    }
    abandoned, _ = _score_pr(abandoned_pr, "2026-07-31", None)
    assert abandoned["raw_score"] == 50
    assert abandoned["score"] == 25
    assert abandoned["category"] == "abandoned"
    assert abandoned["recommended_action"] == "propose_close"

    protected_pr = {
        **abandoned_pr,
        "title": "fix(security): prevent command injection",
        "labels": [{"name": "security"}],
    }
    protected, _ = _score_pr(protected_pr, "2026-07-31", None)
    assert protected["priority"] == "P0"
    assert protected["score"] == 25
    assert protected["recommended_action"] == "escalate_protected_pr"
    protected_comment = _comment_body(protected, "2026-07-31")
    assert protected_comment.startswith("@owner ")
    assert "priority P0" in protected_comment
    assert (
        "<!-- cao-pr-health:v1 action=escalation score=25 as_of=2026-07-31 -->" in protected_comment
    )

    forged_marker_pr = {
        **stalled_pr,
        "comments": [
            {
                "author": {"login": "owner"},
                "createdAt": "2026-07-01T00:00:00Z",
                "body": warning_marker,
                "viewerDidAuthor": False,
            }
        ],
    }
    forged_marker, _ = _score_pr(forged_marker_pr, "2026-07-31", None)
    assert forged_marker["lifecycle_marker"] is None
    assert forged_marker["recommended_action"] == "observe_again"

    assert _parse_close_allowlist("") == set()
    assert _parse_close_allowlist("222, 231,222") == {222, 231}


def _run_locked(inputs: dict[str, Any]) -> None:
    _run_self_tests()
    repo = str(inputs.get("repo") or "").strip()
    as_of = str(inputs.get("as_of") or "").strip()
    snapshot_id = str(inputs.get("snapshot_id") or "").strip()
    max_prs = inputs.get("max_prs", 500)
    importance_analysis = inputs.get("importance_analysis", True)
    importance_provider = str(inputs.get("importance_provider") or "").strip()
    importance_agent = str(inputs.get("importance_agent") or "").strip()
    mode = str(inputs.get("mode") or "").strip()
    close_allowlist_text = str(inputs.get("close_allowlist") or "").strip()

    if repo.count("/") != 1 or any(not part for part in repo.split("/")):
        raise ValueError("repo must be in owner/name form")
    _validate_as_of(as_of)
    if not SAFE_ID_RE.fullmatch(snapshot_id):
        raise ValueError("snapshot_id may contain only letters, numbers, dot, underscore, and dash")
    if isinstance(max_prs, bool) or not isinstance(max_prs, int) or not 1 <= max_prs <= 2000:
        raise ValueError("max_prs must be an integer from 1 through 2000")
    if not isinstance(importance_analysis, bool):
        raise ValueError("importance_analysis must be boolean")
    if not SAFE_ID_RE.fullmatch(importance_provider):
        raise ValueError("importance_provider must be a nonempty safe identifier")
    if not SAFE_ID_RE.fullmatch(importance_agent):
        raise ValueError("importance_agent must be a nonempty safe identifier")
    if mode not in {"dry_run", "apply"}:
        raise ValueError("mode must be dry_run or apply")
    close_allowlist = _parse_close_allowlist(close_allowlist_text)
    if mode == "dry_run" and close_allowlist:
        raise ValueError("close_allowlist is only valid in apply mode")

    repo_key = _repo_storage_key(repo)
    root = Path.home() / ".local" / "state" / "cao" / "pr-health" / repo_key
    artifact_dir = root / "runs" / snapshot_id
    snapshot_path = artifact_dir / "snapshot.json"
    scores_path = artifact_dir / "scores.json"
    report_path = artifact_dir / "report.md"
    analysis_path = artifact_dir / "importance-analysis.md"
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

    state = (
        _read_object(state_path)
        if state_path.is_file()
        else {"schema_version": SCHEMA_VERSION, "repo": repo, "prs": {}}
    )
    if (
        state.get("schema_version") != SCHEMA_VERSION
        or state.get("repo") != repo
        or not isinstance(state.get("prs"), dict)
    ):
        raise ValueError("persisted PR health state has an unsupported schema")

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
        number: value for number, value in next_pr_state.items() if number in open_numbers
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
            analysis_path.write_text(f"{(handle.output or '').strip()}\n", encoding="utf-8")
        except ShimError as exc:
            analysis_error = str(exc)

    enforcement_results = []
    if mode == "apply":
        enforcement_results = _apply_recommendations(
            repo,
            as_of,
            scores,
            updated_state,
            close_allowlist,
            enforcement_path,
        )

    actions: dict[str, int] = {}
    for item in scores:
        actions[item["recommended_action"]] = actions.get(item["recommended_action"], 0) + 1
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "as_of": as_of,
        "snapshot_id": snapshot_id,
        "mode": mode,
        "open_prs": len(scores),
        "actions": dict(sorted(actions.items())),
        "snapshot_file": str(snapshot_path),
        "scores_file": str(scores_path),
        "report_file": str(report_path),
        "importance_analysis_file": (str(analysis_path) if analysis_path.is_file() else None),
        "importance_analysis_error": analysis_error,
        "enforcement_file": (str(enforcement_path) if enforcement_path.is_file() else None),
        "enforcement_results": enforcement_results,
        "mutated_github": any(
            result.get("status")
            in {
                "closed_and_commented",
                "commented",
                "commented_existing_draft",
                "drafted_and_commented",
            }
            for result in enforcement_results
        ),
    }
    _write_json(manifest_path, manifest)
    emit_output(manifest)


def _acquire_repo_lock(repo: str) -> tuple[TextIO, Path]:
    root = Path.home() / ".local" / "state" / "cao" / "pr-health" / _repo_storage_key(repo)
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
