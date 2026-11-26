"""GitHub and Git side of the CAO pull request review workflow.

``pr_review.py`` is the CAO workflow. It reads local evidence files and never
calls GitHub. This helper makes the GitHub and Git calls around it, so the
agents never hold a GitHub write token and the publisher never runs an agent:

- ``prepare`` reads the PR, refuses a closed PR, skips a draft, and writes
  ``pr.json``.
- ``collect`` checks that the checkout is the head that ``prepare`` read. Then
  it writes ``diff.patch`` and ``changed_files.json`` from the merge base with
  the current tip of the target branch. Git reads the attributes of the merge
  base, so a ``.gitattributes`` file in the PR cannot hide a change. Git still
  shows a file with a NUL byte only as "Binary files differ", so
  ``changed_files.json`` marks each such file with ``binary``.
- ``check-secrets`` refuses a file that holds the value of a named environment
  variable (raw, Base64 or hex) or a known token pattern. It never prints a value.
  It does not check a variable that is not set or is shorter than 8 characters.
- ``publish`` validates ``review.json`` and reads the PR again. Then it posts
  one review comment on the PR, or updates the comment of an earlier run in
  place, and adds a neutral check run to the reviewed head. It never approves,
  requests changes or blocks a merge.
- ``report-failure`` adds only a neutral check run with fixed text, when the
  review did not finish or ``publish`` did not post the comment.
- ``summary`` runs the local checks of ``publish`` on ``review.json`` and adds
  the review to the job summary of the workflow run. It makes no GitHub call
  and never prints the review to the job log.

Standard library only, plus the ``gh`` and ``git`` executables. Exit status 0
means done or skipped, 1 means blocked, stale or invalid, and 2 means a usage
error.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterator, Optional

SCHEMA_VERSION = 1
# Check values with fullmatch: with match, "$" also accepts a final newline.
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,254}$")
NUMBER_RE = re.compile(r"^[1-9][0-9]{0,9}$")
RUN_NUMBER_RE = re.compile(r"^[1-9][0-9]{0,19}$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?$")
SERVER_URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/-]{0,100}$")
FINDING_ID_RE = re.compile(r"^F[1-9][0-9]{0,2}$")
CHECK_ID_RE = re.compile(r"^[A-Z][1-9][0-9]?$")
POLICY_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
ENGINES = ("kiro_cli", "copilot_cli")
REVIEWER_MODES = ("single", "panel")
SEVERITIES = ("P1", "P2", "P3")
# Precedence order: the first state is the result.
RESULT_STATES = (
    "changes_required",
    "owner_decision_required",
    "incomplete",
    "no_blocking_findings",
)
STATE_TITLES = {
    "changes_required": "Changes required",
    "owner_decision_required": "Owner decision required",
    "incomplete": "Incomplete",
    "no_blocking_findings": "No blocking findings",
}
RISK_LEVELS = ("high", "medium", "low")
PANEL_SEATS = ("reviewer-a", "reviewer-b", "vote-1", "vote-2", "adjudicator")
MODE_SEATS = {"single": ("reviewer-a",), "panel": PANEL_SEATS}
ID_GROUPS = (
    "accepted_finding_ids",
    "rejected_finding_ids",
    "owner_question_ids",
    "duplicate_finding_ids",
)
# needs.review.result of the workflow, or publish_failure when the review job
# succeeded and the publish step did not post the comment.
JOB_RESULTS = ("failure", "cancelled", "skipped", "publish_failure")
DEFAULT_REVIEWER_LOGIN = "github-actions[bot]"
MARKER_PREFIX = "<!-- cao-pr-review: "
MARKER_RE = re.compile(r"<!-- cao-pr-review: (\{[^\n]*?\}) -->")
FIRST_LINE = (
    "AI-assisted review run by the `pr-review` workflow (CAO, `{engine}`, "
    "reviewers `{reviewers}`), started by @{actor}. This is not an approval. "
    "A later run of the workflow updates this comment in place."
)
SUMMARY_FIRST_LINE = (
    "AI-assisted review of {repo}#{number} at `{head_sha}`, run by the `pr-review` "
    "workflow (CAO, `{engine}`, reviewers `{reviewers}`). This is not an approval. "
    "The `publish` job reports whether it posted or updated the review comment on the PR."
)
CHECK_RUN_NAME = "PR review (advisory)"
ADVISORY_NOTE = "This check is advisory. It is always neutral and never blocks a merge."
# The check run holds no agent text: only fixed text and validated values.
CHECK_SUMMARY = (
    "Result: {states}.\n"
    "\n"
    "- Accepted findings: P1 {p1}, P2 {p2}, P3 {p3}\n"
    "- Questions for the owner: {questions}\n"
    "- Risk level: {risk}\n"
    "- Reviewers: {reviewers}\n"
    "- Engine: {engine}\n"
    "- Not checked: {not_checked} of {checks} REVIEW.md checks\n"
    "- Review comment: {comment_url}\n"
    "- Workflow run: {run_url}\n"
    "\n"
    f"{ADVISORY_NOTE}\n"
)
FAILURE_TITLE = "Review did not finish (advisory)"
PUBLISH_FAILURE_TITLE = "Review not published (advisory)"
FAILURE_TEXT = (
    "The review job of {repo}#{number} at `{head_sha}` ended with the result "
    "`{job_result}`. This run did not post or update a review comment."
)
PUBLISH_FAILURE_TEXT = (
    "The review of {repo}#{number} at `{head_sha}` finished, but this run did not post "
    "or update the review comment. The workflow run log gives the reason."
)
FAILURE_SUMMARY = (
    "{text} A review comment on the PR, if there is one, is from an earlier run.\n"
    "\n"
    "- Reviewers: {reviewers}\n"
    "- Engine: {engine}\n"
    "- Workflow run: {run_url}\n"
    "\n"
    f"{ADVISORY_NOTE}\n"
)
# GitHub refuses a comment body over 65,536 characters.
MAX_BODY_UTF16 = 65_000
# Leaves room for the first line, the drift warning, the run link and the marker.
MAX_SECTIONS_UTF16 = 60_000
MAX_FINDINGS = 999
MAX_CHECK_IDS = 40
MAX_RISK_REASONS = 5
MAX_RISK_REASON_LENGTH = 200
MIN_STEP_TIMEOUT = 300
MAX_STEP_TIMEOUT = 2_700
# GitHub's "list commits on a pull request" endpoint returns at most 250 commits.
PR_COMMITS_CAP = 250
COMMENT_PAGE_LIMIT = 50
MIN_SECRET_LENGTH = 8
TOKEN_PATTERNS = (
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("GitHub fine-grained token", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("Kiro API key", re.compile(r"ksk_[A-Za-z0-9_-]{16,}")),
    ("AWS access key ID", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}")),
)


class Stop(Exception):
    """End the command with an exit status and a one-line reason."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class GhError(Exception):
    """A ``gh api`` call failed. ``status`` is the HTTP status, or 0 if unknown."""

    def __init__(self, path: str, stderr: str) -> None:
        match = re.search(r"\(HTTP (\d{3})\)", stderr)
        self.status = int(match.group(1)) if match else 0
        detail = " ".join(stderr.split())[:300]
        super().__init__(f"gh api {path} failed: {detail}")


# --------------------------------------------------------------------------
# Process and output helpers (the tests replace ``_run``)
# --------------------------------------------------------------------------


def _run(
    cmd: list[str], *, input_text: Optional[str] = None, cwd: Optional[str] = None
) -> tuple[int, str, str]:
    proc = subprocess.run(
        cmd,
        input=input_text,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _gh_api(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    cmd = ["gh", "api", "--method", method, path]
    input_text = None
    if payload is not None:
        cmd += ["--input", "-"]
        input_text = json.dumps(payload)
    code, out, err = _run(cmd, input_text=input_text)
    if code != 0:
        raise GhError(path, err)
    try:
        return json.loads(out) if out.strip() else None
    except ValueError as exc:
        raise Stop(1, f"Blocked: gh api {path} did not return JSON.") from exc


def _paged(path: str, max_pages: int) -> tuple[list[Any], bool]:
    """Items of a paginated list, and whether ``max_pages`` cut the list short."""
    items: list[Any] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        batch = _gh_api(f"{path}{separator}per_page=100&page={page}")
        if not isinstance(batch, list):
            raise Stop(1, f"Blocked: gh api {path} did not return a list.")
        items.extend(batch)
        if len(batch) < 100:
            return items, False
    return items, True


def _git(cwd: Path, *args: str) -> str:
    code, out, err = _run(["git", "-C", str(cwd), *args])
    if code != 0:
        raise Stop(1, f"Blocked: a git command failed: {' '.join(err.split())[:300]}")
    return out


def _report(code: int, message: str) -> int:
    print(message)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"{message}\n")
    return code


def _set_outputs(path: Optional[str], **values: str) -> None:
    if not path:
        return
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise Stop(1, f"Blocked: output {key} holds a line break.")
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Stop(1, f"Blocked: cannot read {path.name}: {exc}") from exc


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _normalize_body(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


# --------------------------------------------------------------------------
# prepare
# --------------------------------------------------------------------------


def prepare(repo: str, number: int, out_dir: Path, github_output: Optional[str]) -> int:
    data = _gh_api(f"repos/{repo}/pulls/{number}")
    if not isinstance(data, dict):
        raise Stop(1, "Blocked: GitHub did not return the PR.")
    if data.get("state") != "open":
        _set_outputs(github_output, proceed="false")
        return _report(1, f"Blocked: {repo}#{number} is not open. Reopen it, then dispatch again.")
    if data.get("draft"):
        _set_outputs(github_output, proceed="false")
        return _report(
            0,
            f"Skipped: {repo}#{number} is a draft. Mark it ready for review, then dispatch again.",
        )
    head = data.get("head") or {}
    base = data.get("base") or {}
    record = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "number": number,
        "html_url": str(data.get("html_url") or ""),
        "title": str(data.get("title") or ""),
        "body": str(data.get("body") or ""),
        "author": str((data.get("user") or {}).get("login") or ""),
        "base_ref": str(base.get("ref") or ""),
        "head_sha": str(head.get("sha") or ""),
        "head_repo": str((head.get("repo") or {}).get("full_name") or ""),
        "head_ref": str(head.get("ref") or ""),
    }
    if not SHA_RE.fullmatch(record["head_sha"]):
        raise Stop(1, "Blocked: GitHub returned an invalid head SHA.")
    if not BRANCH_RE.fullmatch(record["base_ref"]) or ".." in record["base_ref"]:
        raise Stop(1, "Blocked: the target branch name has characters this workflow refuses.")
    html_url = record["html_url"]
    if not html_url.startswith("https://") or not html_url.endswith(f"/{repo}/pull/{number}"):
        raise Stop(1, "Blocked: GitHub returned an unexpected PR URL.")
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_json(out_dir / "pr.json", record)
    _set_outputs(github_output, proceed="true", head_sha=record["head_sha"])
    return _report(0, f"Prepared {repo}#{number} at head {record['head_sha']}.")


# --------------------------------------------------------------------------
# collect
# --------------------------------------------------------------------------


def parse_name_status(output: str) -> list[dict[str, Any]]:
    """Parse ``git diff --name-status -z --find-renames`` into numbered entries."""
    tokens = output.split("\0")
    files: list[dict[str, Any]] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        letter = tokens[index][0]
        width = 3 if letter in ("R", "C") else 2
        if index + width - 1 >= len(tokens) or not tokens[index + width - 1]:
            raise Stop(1, "Blocked: git returned a truncated changed-file entry.")
        old_path = tokens[index + 1] if width == 3 else ""
        path = tokens[index + width - 1]
        files.append(
            {"index": len(files) + 1, "status": letter, "path": path, "old_path": old_path}
        )
        index += width
    return files


def parse_binary_paths(output: str) -> set[str]:
    """The paths that ``git diff --numstat -z`` shows as binary, with ``-`` counts."""
    tokens = output.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(tokens) and tokens[index]:
        added, _, rest = tokens[index].partition("\t")
        deleted, _, path = rest.partition("\t")
        # A rename has an empty path here, then the old and the new path.
        width = 1 if path else 3
        if index + width - 1 >= len(tokens) or not tokens[index + width - 1]:
            raise Stop(1, "Blocked: git returned a truncated changed-file entry.")
        if added == "-" and deleted == "-":
            paths.add(tokens[index + width - 1] if width == 3 else path)
        index += width
    return paths


def collect(evidence_dir: Path, source_dir: Path) -> int:
    record = _load_json(evidence_dir / "pr.json")
    head = _git(source_dir, "rev-parse", "--verify", "HEAD^{commit}").strip()
    if head != record.get("head_sha"):
        return _report(
            1,
            "Stale: the checkout is not the head that prepare read, so the PR changed "
            "during setup. Dispatch again.",
        )
    base_ref = str(record.get("base_ref") or "")
    if (
        not BRANCH_RE.fullmatch(base_ref)
        or _git(source_dir, "check-ref-format", "--branch", base_ref).strip() != base_ref
    ):
        raise Stop(1, "Blocked: the target branch name is not a plain branch name.")
    # The API's base.sha can lag the branch, so review against the fetched tip.
    base_sha = _git(
        source_dir, "rev-parse", "--verify", f"refs/remotes/origin/{base_ref}^{{commit}}"
    ).strip()
    merge_base = _git(source_dir, "merge-base", base_sha, head).strip()
    if not SHA_RE.fullmatch(base_sha) or not SHA_RE.fullmatch(merge_base):
        raise Stop(1, "Blocked: git returned an invalid target tip or merge base.")
    # Attributes come from the merge base, not from the PR checkout: a PR that
    # adds `* -diff` to .gitattributes must not turn its change into "Binary files differ".
    diff = [
        "-c",
        "core.quotePath=false",
        f"--attr-source={merge_base}",
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--find-renames",
    ]
    patch = _git(
        source_dir, *diff, "--unified=3", "--src-prefix=a/", "--dst-prefix=b/", merge_base, head
    )
    files = parse_name_status(_git(source_dir, *diff, "--name-status", "-z", merge_base, head))
    if not files:
        return _report(
            1, f"Blocked: the PR changes no file between merge base {merge_base} and the head."
        )
    binary = parse_binary_paths(_git(source_dir, *diff, "--numstat", "-z", merge_base, head))
    for item in files:
        item["binary"] = item["path"] in binary
    (evidence_dir / "diff.patch").write_text(patch, encoding="utf-8")
    _write_json(
        evidence_dir / "changed_files.json", {"schema_version": SCHEMA_VERSION, "files": files}
    )
    record["base_sha"] = base_sha
    record["merge_base"] = merge_base
    _write_json(evidence_dir / "pr.json", record)
    return _report(
        0, f"Collected {len(files)} changed files from merge base {merge_base} to head {head}."
    )


# --------------------------------------------------------------------------
# check-secrets
# --------------------------------------------------------------------------


def _string_leaves(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _string_leaves(item)
    elif isinstance(value, list):
        for item in value:
            yield from _string_leaves(item)


def secret_needles(value: str) -> list[tuple[str, str]]:
    """Forms of ``value`` to look for: raw, hex, and Base64 at each byte alignment.

    For each alignment, only the Base64 characters that depend on the value alone
    are kept, so the value is found inside a longer encoded string too.
    """
    raw = value.encode("utf-8")
    needles = [("raw", value), ("hex", raw.hex()), ("hex", raw.hex().upper())]
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + raw).decode("ascii")
        first_group = -(-offset // 3)
        last_group = (offset + len(raw)) // 3
        stable = encoded[4 * first_group : 4 * last_group]
        if len(stable) >= 8:
            needles.append(("Base64", stable))
            needles.append(("Base64", stable.replace("+", "-").replace("/", "_")))
    return needles


def find_secrets(text: str, env_names: list[str]) -> list[str]:
    """Names of what leaked: an env var and its form, or a token pattern. Never a value."""
    haystacks = [text]
    try:
        haystacks.extend(_string_leaves(json.loads(text)))
    except ValueError:
        pass
    found: list[str] = []
    for name in env_names:
        value = os.environ.get(name, "")
        if len(value) < MIN_SECRET_LENGTH:
            continue
        for form, needle in secret_needles(value):
            if any(needle in haystack for haystack in haystacks):
                found.append(f"{name} ({form})")
                break
    for label, pattern in TOKEN_PATTERNS:
        if any(pattern.search(haystack) for haystack in haystacks):
            found.append(label)
    return found


def check_secrets(path: Path, env_names: list[str]) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        raise Stop(1, f"Blocked: cannot read {path.name}: {exc}") from exc
    found = find_secrets(text, env_names)
    if found:
        return _report(
            1,
            f"Blocked: {path.name} holds a credential or a token pattern: {', '.join(found)}. "
            "Nothing is published. Rotate the credential if it is real.",
        )
    return _report(0, f"No credential value or token pattern found in {path.name}.")


# --------------------------------------------------------------------------
# publish
# --------------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise Stop(1, f"Blocked: review.json is invalid: {message}.")


def _id_list(review: dict[str, Any], key: str) -> list[str]:
    value = review.get(key)
    _require(isinstance(value, list) and len(value) <= MAX_FINDINGS, f"{key} is not a list")
    _require(
        all(isinstance(item, str) and FINDING_ID_RE.fullmatch(item) for item in value),
        f"{key} has an invalid ID",
    )
    return list(value)


def _unique_strings(value: Any, pattern: re.Pattern[str]) -> bool:
    return (
        isinstance(value, list)
        and all(isinstance(item, str) and pattern.fullmatch(item) for item in value)
        and len(set(value)) == len(value)
    )


def _valid_risk(risk: Any) -> bool:
    if not isinstance(risk, dict) or set(risk) != {"level", "reasons"}:
        return False
    reasons = risk["reasons"]
    return (
        risk["level"] in RISK_LEVELS
        and isinstance(reasons, list)
        and 0 < len(reasons) <= MAX_RISK_REASONS
        and all(
            isinstance(reason, str) and 0 < len(reason) <= MAX_RISK_REASON_LENGTH
            for reason in reasons
        )
    )


def validate_review(
    review: Any, repo: str, number: int, head_sha: str, engine: str, reviewers: str
) -> None:
    """Check the review job's output before any GitHub write. Raises ``Stop``."""
    _require(isinstance(review, dict), "the document is not an object")
    _require(review.get("schema_version") == SCHEMA_VERSION, "schema_version is not supported")
    _require(review.get("publishable") is True, "publishable is not true")
    _require(review.get("repo") == repo, "repo is not the dispatched repository")
    _require(review.get("pr") == number, "pr is not the dispatched PR")
    _require(review.get("head_sha") == head_sha, "head_sha is not the prepared head")
    _require(review.get("engine") == engine, "engine is not the dispatched engine")
    _require(
        reviewers in REVIEWER_MODES and review.get("reviewers") == reviewers,
        "reviewers is not the dispatched mode",
    )
    for key in ("base_sha", "merge_base"):
        _require(isinstance(review.get(key), str) and bool(SHA_RE.fullmatch(review[key])), key)
    _require(
        isinstance(review.get("base_ref"), str) and bool(BRANCH_RE.fullmatch(review["base_ref"])),
        "base_ref",
    )
    models = review.get("models")
    _require(
        isinstance(models, dict)
        and set(models) == set(MODE_SEATS[reviewers])
        and all(isinstance(v, str) and MODEL_RE.fullmatch(v) for v in models.values()),
        "models do not match the seats of the reviewers mode",
    )
    step_timeout = review.get("step_timeout")
    _require(
        type(step_timeout) is int and MIN_STEP_TIMEOUT <= step_timeout <= MAX_STEP_TIMEOUT,
        "step_timeout",
    )
    policy_sha = review.get("policy_sha256")
    _require(
        isinstance(policy_sha, str) and bool(POLICY_SHA_RE.fullmatch(policy_sha)), "policy_sha256"
    )
    check_ids = review.get("check_ids")
    _require(
        _unique_strings(check_ids, CHECK_ID_RE) and 0 < len(check_ids) <= MAX_CHECK_IDS,
        "check_ids",
    )
    not_checked = review.get("not_checked_ids")
    _require(
        _unique_strings(not_checked, CHECK_ID_RE) and set(not_checked) <= set(check_ids),
        "not_checked_ids are not unique check_ids",
    )
    _require(_valid_risk(review.get("risk")), "risk")
    states = review.get("result_states")
    _require(
        isinstance(states, list)
        and bool(states)
        and states == [state for state in RESULT_STATES if state in states],
        "result_states are not known states in precedence order",
    )
    _require(
        ("no_blocking_findings" in states) == (states == ["no_blocking_findings"]),
        "no_blocking_findings cannot be combined with another state",
    )
    counts = review.get("severity_counts")
    _require(
        isinstance(counts, dict)
        and set(counts) == set(SEVERITIES)
        and all(type(v) is int and 0 <= v <= MAX_FINDINGS for v in counts.values()),
        "severity_counts",
    )
    finding_ids = _id_list(review, "finding_ids")
    grouped = [item for key in ID_GROUPS for item in _id_list(review, key)]
    _require(
        len(set(finding_ids)) == len(finding_ids) and sorted(grouped) == sorted(finding_ids),
        "every finding ID must be in exactly one group",
    )
    complete = review.get("review_complete")
    _require(type(complete) is bool, "review_complete")
    _require(
        complete == ("incomplete" not in states), "review_complete does not match result_states"
    )
    _require(
        not (complete and not_checked), "review_complete is true, but not_checked_ids is not empty"
    )
    sections = review.get("sections")
    _require(
        isinstance(sections, str) and 0 < utf16_length(sections) <= MAX_SECTIONS_UTF16,
        "sections are empty or too long",
    )
    _require("<!--" not in sections, "sections hold an HTML comment")


def build_marker(review: dict[str, Any], run_id: str, run_attempt: str) -> str:
    """The HTML comment that identifies the review comment. Only validated values, no agent text."""
    record = {
        "v": SCHEMA_VERSION,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "pr": review["pr"],
        "head_sha": review["head_sha"],
        "base_ref": review["base_ref"],
        "base_sha": review["base_sha"],
        "merge_base": review["merge_base"],
        "engine": review["engine"],
        "reviewers": review["reviewers"],
        "models": review["models"],
        "step_timeout": review["step_timeout"],
        "policy_sha256": review["policy_sha256"],
        "result_states": review["result_states"],
        "risk_level": review["risk"]["level"],
        "not_checked_ids": review["not_checked_ids"],
        "finding_ids": review["finding_ids"],
        **{key: review[key] for key in ID_GROUPS},
        "severity_counts": review["severity_counts"],
        "review_complete": review["review_complete"],
    }
    text = json.dumps(record, separators=(",", ":"), sort_keys=True)
    if "-->" in text or "--!>" in text or "\n" in text:
        raise Stop(1, "Blocked: the marker would end its HTML comment early.")
    return f"{MARKER_PREFIX}{text} -->"


def compose_body(
    review: dict[str, Any],
    *,
    engine: str,
    reviewers: str,
    actor: str,
    run_url: str,
    marker: str,
    warning: str = "",
) -> str:
    parts = [FIRST_LINE.format(engine=engine, reviewers=reviewers, actor=actor)]
    if warning:
        parts.append(warning)
    parts.append(f"Workflow run: {run_url}")
    parts.append(review["sections"].strip())
    parts.append(marker)
    body = "\n\n".join(parts) + "\n"
    if utf16_length(body) > MAX_BODY_UTF16:
        raise Stop(1, "Blocked: the review comment is longer than GitHub accepts.")
    return body


def marker_run_id(body: str) -> Optional[str]:
    match = MARKER_RE.search(body or "")
    if not match:
        return None
    try:
        record = json.loads(match.group(1))
    except ValueError:
        return None
    run_id = record.get("run_id") if isinstance(record, dict) else None
    return run_id if isinstance(run_id, str) else None


def _is_reviewer(user: Any, reviewer_login: str) -> bool:
    """True if ``user`` is the publishing account. A ``[bot]`` login must be a Bot."""
    if not isinstance(user, dict) or user.get("login") != reviewer_login:
        return False
    return not reviewer_login.endswith("[bot]") or user.get("type") == "Bot"


def _our_comments(repo: str, number: int, reviewer_login: str) -> list[tuple[int, int]]:
    """(comment ID, run ID) of each review comment of this workflow on the PR, oldest first."""
    comments, truncated = _paged(f"repos/{repo}/issues/{number}/comments", COMMENT_PAGE_LIMIT)
    if truncated:
        raise Stop(
            1,
            f"Blocked: {repo}#{number} has too many comments to find the review comment. "
            "Nothing is published.",
        )
    ours = []
    for item in comments:
        if not isinstance(item, dict) or not _is_reviewer(item.get("user"), reviewer_login):
            continue
        comment_id = item.get("id")
        run_id = marker_run_id(str(item.get("body") or ""))
        if type(comment_id) is int and run_id is not None and RUN_NUMBER_RE.fullmatch(run_id):
            ours.append((comment_id, int(run_id)))
    return sorted(ours)


def drift_warning(repo: str, number: int, head_sha: str, live: dict[str, Any]) -> str:
    """'' if the head did not move, a warning if it moved within the PR. Else ``Stop``."""
    live_head = str((live.get("head") or {}).get("sha") or "")
    if live_head == head_sha:
        return ""
    commits, _ = _paged(f"repos/{repo}/pulls/{number}/commits", 3)
    if head_sha in {str(item.get("sha") or "") for item in commits}:
        return (
            f"Warning: the PR head moved to `{live_head}` after this review started. "
            f"This review applies to `{head_sha}` only. Dispatch again to review the new head."
        )
    if len(commits) >= PR_COMMITS_CAP:
        raise Stop(
            1,
            f"Stale: the PR head moved, and GitHub lists only the first {PR_COMMITS_CAP} "
            "commits of a PR, so the reviewed head cannot be found. Nothing is published.",
        )
    raise Stop(
        1,
        "Stale: the reviewed head is no longer a commit of the PR. Nothing is published. "
        "Dispatch again to review the new head.",
    )


def _verify_comment(
    repo: str, number: int, comment_id: int, body: str, reviewer_login: str
) -> None:
    comment = _gh_api(f"repos/{repo}/issues/comments/{comment_id}")
    if not isinstance(comment, dict):
        comment = {}
    problems = []
    if not _is_reviewer(comment.get("user"), reviewer_login):
        problems.append("author")
    if _normalize_body(str(comment.get("body") or "")) != _normalize_body(body):
        problems.append("body")
    if not str(comment.get("issue_url") or "").endswith(f"/issues/{number}"):
        problems.append("PR")
    if problems:
        raise Stop(
            1,
            f"Blocked: review comment {comment_id} does not match the request "
            f"({', '.join(problems)}). It is not changed again. Check it on the PR.",
        )


def _run_url(server_url: str, repo: str, run_id: str, run_attempt: str) -> str:
    return f"{server_url}/{repo}/actions/runs/{run_id}/attempts/{run_attempt}"


def _check_run(head_sha: str, run_id: str, run_url: str, title: str, text: str) -> dict[str, Any]:
    """A completed, neutral check run. ``title`` and ``text`` must hold no agent text."""
    return {
        "name": CHECK_RUN_NAME,
        "head_sha": head_sha,
        "status": "completed",
        "conclusion": "neutral",
        "details_url": run_url,
        "external_id": run_id,
        "output": {"title": title, "summary": text},
    }


def review_check_run(
    review: dict[str, Any], *, run_id: str, run_url: str, comment_url: str
) -> dict[str, Any]:
    """The check run of a published review, from validated values only."""
    states = review["result_states"]
    counts = review["severity_counts"]
    text = CHECK_SUMMARY.format(
        states=", ".join(STATE_TITLES[state] for state in states),
        p1=counts["P1"],
        p2=counts["P2"],
        p3=counts["P3"],
        questions=len(review["owner_question_ids"]),
        risk=review["risk"]["level"],
        reviewers=review["reviewers"],
        engine=review["engine"],
        not_checked=len(review["not_checked_ids"]),
        checks=len(review["check_ids"]),
        comment_url=comment_url,
        run_url=run_url,
    )
    title = f"{STATE_TITLES[states[0]]} (advisory)"
    return _check_run(review["head_sha"], run_id, run_url, title, text)


def _create_check_run(repo: str, payload: dict[str, Any], note: str) -> int:
    try:
        created = _gh_api(f"repos/{repo}/check-runs", method="POST", payload=payload)
    except GhError as exc:
        raise Stop(1, f"Blocked: {exc}. {note}") from exc
    check_id = created.get("id") if isinstance(created, dict) else None
    if type(check_id) is not int:
        raise Stop(1, f"Blocked: GitHub returned no check run ID. {note}")
    return check_id


def _checked_review(
    review_path: Path, repo: str, number: int, head_sha: str, engine: str, reviewers: str
) -> dict[str, Any]:
    """The review after the local checks. Raises ``Stop``."""
    review: dict[str, Any] = _load_json(review_path)
    validate_review(review, repo, number, head_sha, engine, reviewers)
    leaks = find_secrets(json.dumps({"sections": review["sections"]}), [])
    if leaks:
        raise Stop(1, f"Blocked: the review holds a token pattern: {', '.join(leaks)}.")
    return review


def publish(
    *,
    repo: str,
    number: int,
    review_path: Path,
    head_sha: str,
    run_id: str,
    run_attempt: str,
    server_url: str,
    engine: str,
    reviewers: str,
    actor: str,
    reviewer_login: str,
    github_output: Optional[str],
    dry_run_out: Optional[Path],
) -> int:
    review = _checked_review(review_path, repo, number, head_sha, engine, reviewers)
    marker = build_marker(review, run_id, run_attempt)

    live = _gh_api(f"repos/{repo}/pulls/{number}")
    if not isinstance(live, dict) or live.get("state") != "open":
        return _report(1, f"Blocked: {repo}#{number} is not open. Nothing is published.")
    if live.get("draft"):
        return _report(0, f"Skipped: {repo}#{number} is now a draft. Nothing is published.")
    warning = drift_warning(repo, number, head_sha, live)

    ours = _our_comments(repo, number, reviewer_login)
    newer = [other for _, other in ours if other > int(run_id)]
    if newer:
        return _report(
            0,
            f"Skipped: a newer workflow run ({max(newer)}) published the review comment on "
            f"{repo}#{number}. Nothing is published.",
        )

    run_url = _run_url(server_url, repo, run_id, run_attempt)
    body = compose_body(
        review,
        engine=engine,
        reviewers=reviewers,
        actor=actor,
        run_url=run_url,
        marker=marker,
        warning=warning,
    )
    pr_url = f"{server_url}/{repo}/pull/{number}"
    # Update the oldest earlier comment in place, so the PR keeps one review comment.
    target = ours[0][0] if ours else None
    if target is not None:
        method, path = "PATCH", f"repos/{repo}/issues/comments/{target}"
    else:
        method, path = "POST", f"repos/{repo}/issues/{number}/comments"
    if dry_run_out is not None:
        # A new comment has no ID yet, so its check run links to the PR.
        comment_url = f"{pr_url}#issuecomment-{target}" if target is not None else pr_url
        check_run = review_check_run(
            review, run_id=run_id, run_url=run_url, comment_url=comment_url
        )
        _write_json(
            dry_run_out,
            {"comment": {"method": method, "path": path, "body": body}, "check_run": check_run},
        )
        return _report(0, f"Dry run: the requests are in {dry_run_out}. Nothing is posted.")

    try:
        written = _gh_api(path, method=method, payload={"body": body})
    except GhError as exc:
        raise Stop(1, f"Blocked: {exc}. Nothing is posted again.") from exc
    # From here on, a failure must not report that this run changed no comment.
    _set_outputs(github_output, comment_written="true")
    comment_id = written.get("id") if isinstance(written, dict) else None
    if type(comment_id) is not int or (target is not None and comment_id != target):
        raise Stop(1, "Blocked: GitHub returned an unexpected comment ID. Check the PR by hand.")
    _verify_comment(repo, number, comment_id, body, reviewer_login)
    check_run = review_check_run(
        review, run_id=run_id, run_url=run_url, comment_url=f"{pr_url}#issuecomment-{comment_id}"
    )
    check_id = _create_check_run(
        repo,
        check_run,
        f"Review comment {comment_id} is posted, but the check run is not added.",
    )
    action = "Posted" if target is None else "Updated"
    return _report(
        0,
        f"{action} review comment {comment_id} on {repo}#{number} for head {head_sha}. "
        f"Added neutral check run {check_id}.",
    )


# --------------------------------------------------------------------------
# report-failure
# --------------------------------------------------------------------------


def report_failure(
    *,
    repo: str,
    number: int,
    head_sha: str,
    run_id: str,
    run_attempt: str,
    server_url: str,
    engine: str,
    reviewers: str,
    job_result: str,
    dry_run_out: Optional[Path],
) -> int:
    """Add a neutral check run with fixed text. It never reads or writes a comment."""
    run_url = _run_url(server_url, repo, run_id, run_attempt)
    if job_result == "publish_failure":
        title = PUBLISH_FAILURE_TITLE
        text = PUBLISH_FAILURE_TEXT.format(repo=repo, number=number, head_sha=head_sha)
    else:
        title = FAILURE_TITLE
        text = FAILURE_TEXT.format(
            repo=repo, number=number, head_sha=head_sha, job_result=job_result
        )
    summary_text = FAILURE_SUMMARY.format(
        text=text, reviewers=reviewers, engine=engine, run_url=run_url
    )
    payload = _check_run(head_sha, run_id, run_url, title, summary_text)
    if dry_run_out is not None:
        _write_json(dry_run_out, {"check_run": payload})
        return _report(0, f"Dry run: the request is in {dry_run_out}. Nothing is posted.")
    check_id = _create_check_run(repo, payload, "The check run is not added.")
    return _report(0, f"Added neutral check run {check_id} to {head_sha}: {title}.")


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def summary(
    *, repo: str, number: int, review_path: Path, head_sha: str, engine: str, reviewers: str
) -> int:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        raise Stop(1, "Blocked: GITHUB_STEP_SUMMARY is not set, so there is no run summary.")
    review = _checked_review(review_path, repo, number, head_sha, engine, reviewers)
    first_line = SUMMARY_FIRST_LINE.format(
        repo=repo, number=number, head_sha=head_sha, engine=engine, reviewers=reviewers
    )
    # validate_review limits the sections to MAX_SECTIONS_UTF16 code units, far
    # below the 1 MiB that GitHub accepts for the summary of one step.
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{first_line}\n\n{review['sections'].strip()}\n")
    # print, not _report: the summary must end with the review, not this line.
    print(f"The run summary shows the review of {repo}#{number} at {head_sha}.")
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------


def _pattern(regex: re.Pattern[str], name: str) -> Any:
    def check(value: str) -> str:
        if not regex.fullmatch(value):
            raise argparse.ArgumentTypeError(f"invalid {name}")
        return value

    return check


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GitHub side of the CAO PR review workflow.")
    commands = parser.add_subparsers(dest="command", required=True)
    repo_arg = _pattern(REPO_RE, "repository")

    def pr_arg(value: str) -> int:
        return int(_pattern(NUMBER_RE, "PR number")(value))

    def run_args(command: argparse.ArgumentParser) -> None:
        command.add_argument("--run-id", required=True, type=_pattern(RUN_NUMBER_RE, "run ID"))
        command.add_argument(
            "--run-attempt", required=True, type=_pattern(RUN_NUMBER_RE, "attempt")
        )
        command.add_argument(
            "--server-url", required=True, type=_pattern(SERVER_URL_RE, "server URL")
        )

    prep = commands.add_parser("prepare", help="read the PR and write pr.json")
    prep.add_argument("--repo", required=True, type=repo_arg)
    prep.add_argument("--pr", required=True, type=pr_arg)
    prep.add_argument("--out", required=True, type=Path)

    coll = commands.add_parser("collect", help="write diff.patch and changed_files.json")
    coll.add_argument("--evidence", required=True, type=Path)
    coll.add_argument("--source", required=True, type=Path)

    sec = commands.add_parser("check-secrets", help="refuse a file that holds a credential")
    sec.add_argument("--file", required=True, type=Path)
    sec.add_argument("--env", action="append", default=[], help="name of an env var to check")

    pub = commands.add_parser(
        "publish", help="post or update the review comment, then add a neutral check run"
    )
    pub.add_argument("--repo", required=True, type=repo_arg)
    pub.add_argument("--pr", required=True, type=pr_arg)
    pub.add_argument("--review", required=True, type=Path)
    pub.add_argument("--head", required=True, type=_pattern(SHA_RE, "head SHA"))
    run_args(pub)
    pub.add_argument("--engine", required=True, choices=ENGINES)
    pub.add_argument("--reviewers", required=True, choices=REVIEWER_MODES)
    pub.add_argument("--actor", required=True, type=_pattern(LOGIN_RE, "login"))
    pub.add_argument(
        "--reviewer-login", default=DEFAULT_REVIEWER_LOGIN, type=_pattern(LOGIN_RE, "login")
    )
    pub.add_argument("--dry-run", type=Path, help="write the requests to this file; post nothing")

    fail = commands.add_parser(
        "report-failure", help="add a neutral check run that says the review was not published"
    )
    fail.add_argument("--repo", required=True, type=repo_arg)
    fail.add_argument("--pr", required=True, type=pr_arg)
    fail.add_argument("--head", required=True, type=_pattern(SHA_RE, "head SHA"))
    run_args(fail)
    fail.add_argument("--engine", required=True, choices=ENGINES)
    fail.add_argument("--reviewers", required=True, choices=REVIEWER_MODES)
    fail.add_argument(
        "--job-result",
        required=True,
        choices=JOB_RESULTS,
        help="result of the review job, or publish_failure if the publish step failed",
    )
    fail.add_argument("--dry-run", type=Path, help="write the request to this file; post nothing")

    summ = commands.add_parser("summary", help="add the review to the job summary")
    summ.add_argument("--repo", required=True, type=repo_arg)
    summ.add_argument("--pr", required=True, type=pr_arg)
    summ.add_argument("--review", required=True, type=Path)
    summ.add_argument("--head", required=True, type=_pattern(SHA_RE, "head SHA"))
    summ.add_argument("--engine", required=True, choices=ENGINES)
    summ.add_argument("--reviewers", required=True, choices=REVIEWER_MODES)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "prepare":
            return prepare(args.repo, args.pr, args.out, os.environ.get("GITHUB_OUTPUT"))
        if args.command == "collect":
            return collect(args.evidence, args.source)
        if args.command == "check-secrets":
            return check_secrets(args.file, args.env)
        if args.command == "summary":
            return summary(
                repo=args.repo,
                number=args.pr,
                review_path=args.review,
                head_sha=args.head,
                engine=args.engine,
                reviewers=args.reviewers,
            )
        if args.command == "report-failure":
            return report_failure(
                repo=args.repo,
                number=args.pr,
                head_sha=args.head,
                run_id=args.run_id,
                run_attempt=args.run_attempt,
                server_url=args.server_url,
                engine=args.engine,
                reviewers=args.reviewers,
                job_result=args.job_result,
                dry_run_out=args.dry_run,
            )
        return publish(
            repo=args.repo,
            number=args.pr,
            review_path=args.review,
            head_sha=args.head,
            run_id=args.run_id,
            run_attempt=args.run_attempt,
            server_url=args.server_url,
            engine=args.engine,
            reviewers=args.reviewers,
            actor=args.actor,
            reviewer_login=args.reviewer_login,
            github_output=os.environ.get("GITHUB_OUTPUT"),
            dry_run_out=args.dry_run,
        )
    except Stop as stop:
        return _report(stop.code, stop.message)
    except GhError as exc:
        return _report(1, f"Blocked: {exc}")


if __name__ == "__main__":
    sys.exit(main())
