"""GitHub and Git side of the CAO pull request review workflow.

``pr_review.py`` is the CAO workflow. It reads local evidence files and never
calls GitHub. This helper makes the GitHub and Git calls around it, so the
agents never hold a GitHub write token and the publisher never runs an agent:

- ``prepare`` reads the PR, refuses a closed PR, skips a draft, and writes
  ``pr.json``.
- ``collect`` checks that the checkout is the head that ``prepare`` read. Then
  it writes ``diff.patch`` and ``changed_files.json`` from the merge base with
  the current tip of the target branch.
- ``check-secrets`` refuses a file that holds the value of a named environment
  variable (raw, Base64 or hex) or a known token pattern. It never prints a value.
- ``publish`` validates ``review.json``, reads the PR again, and posts one
  ``COMMENT`` review on the reviewed head. It never approves or requests
  changes, and it posts at most one review for each workflow run.
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
ENGINES = ("kiro_cli", "copilot_cli")
SEVERITIES = ("P1", "P2", "P3")
# Precedence order: the first state is the result.
RESULT_STATES = (
    "changes_required",
    "owner_decision_required",
    "incomplete",
    "no_blocking_findings",
)
PANEL_SEATS = ("reviewer-a", "reviewer-b", "vote-1", "vote-2", "adjudicator")
ID_GROUPS = (
    "accepted_finding_ids",
    "rejected_finding_ids",
    "owner_question_ids",
    "duplicate_finding_ids",
)
DEFAULT_REVIEWER_LOGIN = "github-actions[bot]"
MARKER_PREFIX = "<!-- cao-pr-review: "
MARKER_RE = re.compile(r"<!-- cao-pr-review: (\{[^\n]*?\}) -->")
FIRST_LINE = (
    "AI-assisted review run by the `pr-review` workflow (CAO, `{engine}`), "
    "started by @{actor}. This is not an approval."
)
SUMMARY_FIRST_LINE = (
    "AI-assisted review of {repo}#{number} at `{head_sha}`, run by the `pr-review` "
    "workflow (CAO, `{engine}`). This is not an approval. The `publish` job reports "
    "whether it posted this review on the PR."
)
INLINE_FALLBACK_NOTE = (
    "GitHub refused the inline comments, so this review has none. "
    "The body gives every accepted finding."
)
# GitHub refuses a review body or comment over 65,536 characters.
MAX_BODY_UTF16 = 65_000
# Leaves room for the first line, the drift warning, the run link and the marker.
MAX_SECTIONS_UTF16 = 60_000
MAX_COMMENTS = 100
MAX_COMMENT_UTF16 = 4_000
MAX_PATH_LENGTH = 1_000
MAX_FINDINGS = 999
# GitHub's "list commits on a pull request" endpoint returns at most 250 commits.
PR_COMMITS_CAP = 250
REVIEW_PAGE_LIMIT = 50
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
    diff = [
        "-c",
        "core.quotePath=false",
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


def validate_review(review: Any, repo: str, number: int, head_sha: str, engine: str) -> None:
    """Check the review job's output before any GitHub write. Raises ``Stop``."""
    _require(isinstance(review, dict), "the document is not an object")
    _require(review.get("schema_version") == SCHEMA_VERSION, "schema_version is not supported")
    _require(review.get("publishable") is True, "publishable is not true")
    _require(review.get("repo") == repo, "repo is not the dispatched repository")
    _require(review.get("pr") == number, "pr is not the dispatched PR")
    _require(review.get("head_sha") == head_sha, "head_sha is not the prepared head")
    _require(review.get("engine") == engine, "engine is not the dispatched engine")
    for key in ("base_sha", "merge_base"):
        _require(isinstance(review.get(key), str) and bool(SHA_RE.fullmatch(review[key])), key)
    _require(
        isinstance(review.get("base_ref"), str) and bool(BRANCH_RE.fullmatch(review["base_ref"])),
        "base_ref",
    )
    models = review.get("models")
    _require(
        isinstance(models, dict)
        and set(models) == set(PANEL_SEATS)
        and all(isinstance(v, str) and MODEL_RE.fullmatch(v) for v in models.values()),
        "models",
    )
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
    _require(type(review.get("review_complete")) is bool, "review_complete")
    sections = review.get("sections")
    _require(
        isinstance(sections, str) and 0 < utf16_length(sections) <= MAX_SECTIONS_UTF16,
        "sections are empty or too long",
    )
    _require("<!--" not in sections, "sections hold an HTML comment")
    comments = review.get("comments")
    _require(isinstance(comments, list) and len(comments) <= MAX_COMMENTS, "comments")
    for comment in comments:
        _require(
            isinstance(comment, dict) and set(comment) == {"path", "line", "body"},
            "a comment has unexpected fields",
        )
        path, line, body = comment["path"], comment["line"], comment["body"]
        _require(
            isinstance(path, str)
            and 0 < len(path) <= MAX_PATH_LENGTH
            and not path.startswith("/")
            and not any(ord(ch) < 32 for ch in path),
            "a comment path is invalid",
        )
        _require(type(line) is int and line >= 1, "a comment line is invalid")
        _require(
            isinstance(body, str) and 0 < utf16_length(body) <= MAX_COMMENT_UTF16,
            "a comment body is empty or too long",
        )
        _require("<!--" not in body, "a comment holds an HTML comment")


def build_marker(review: dict[str, Any], run_id: str, run_attempt: str) -> str:
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
        "models": review["models"],
        "result_states": review["result_states"],
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
    actor: str,
    run_url: str,
    marker: str,
    warning: str = "",
    inline_note: str = "",
) -> str:
    parts = [FIRST_LINE.format(engine=engine, actor=actor)]
    if warning:
        parts.append(warning)
    parts.append(f"Workflow run: {run_url}")
    parts.append(review["sections"].strip())
    if inline_note:
        parts.append(inline_note)
    parts.append(marker)
    body = "\n\n".join(parts) + "\n"
    if utf16_length(body) > MAX_BODY_UTF16:
        raise Stop(1, "Blocked: the review body is longer than GitHub accepts.")
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


def _already_published(repo: str, number: int, run_id: str, reviewer_login: str) -> bool:
    reviews, truncated = _paged(f"repos/{repo}/pulls/{number}/reviews", REVIEW_PAGE_LIMIT)
    if truncated:
        raise Stop(1, "Blocked: the PR has too many reviews to look for an earlier post.")
    for item in reviews:
        user = item.get("user") or {}
        if user.get("login") != reviewer_login:
            continue
        if reviewer_login.endswith("[bot]") and user.get("type") != "Bot":
            continue
        if marker_run_id(str(item.get("body") or "")) == run_id:
            return True
    return False


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


def _comment_key(path: Any, line: Any, body: Any) -> tuple[str, int, str]:
    return (str(path), line if type(line) is int else -1, _normalize_body(str(body)))


def _verify_published(
    repo: str,
    number: int,
    review_id: int,
    head_sha: str,
    body: str,
    comments: list[dict[str, Any]],
    reviewer_login: str,
) -> None:
    published = _gh_api(f"repos/{repo}/pulls/{number}/reviews/{review_id}") or {}
    problems = []
    if published.get("commit_id") != head_sha:
        problems.append("commit")
    if published.get("state") != "COMMENTED":
        problems.append("state")
    if (published.get("user") or {}).get("login") != reviewer_login:
        problems.append("author")
    if _normalize_body(str(published.get("body") or "")) != _normalize_body(body):
        problems.append("body")
    # The per-review comments endpoint returns "line": null, so read the PR's
    # review comments, newest first, and keep the ones of this review.
    recent, _ = _paged(f"repos/{repo}/pulls/{number}/comments?sort=created&direction=desc", 3)
    posted = [c for c in recent if c.get("pull_request_review_id") == review_id]
    expected = sorted(_comment_key(c["path"], c["line"], c["body"]) for c in comments)
    actual = sorted(_comment_key(c.get("path"), c.get("line"), c.get("body")) for c in posted)
    if expected != actual or any(c.get("side") != "RIGHT" for c in posted):
        problems.append("inline comments")
    if problems:
        raise Stop(
            1,
            f"Published review {review_id} does not match the request ({', '.join(problems)}). "
            "It is not posted again. Check it on the PR.",
        )


def _checked_review(
    review_path: Path, repo: str, number: int, head_sha: str, engine: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The review and its inline comments after the local checks. Raises ``Stop``."""
    review = _load_json(review_path)
    validate_review(review, repo, number, head_sha, engine)
    comments = [
        {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": c["body"]}
        for c in review["comments"]
    ]
    leaks = find_secrets(json.dumps({"sections": review["sections"], "comments": comments}), [])
    if leaks:
        raise Stop(1, f"Blocked: the review holds a token pattern: {', '.join(leaks)}.")
    return review, comments


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
    actor: str,
    reviewer_login: str,
    dry_run_out: Optional[Path],
) -> int:
    review, comments = _checked_review(review_path, repo, number, head_sha, engine)
    marker = build_marker(review, run_id, run_attempt)
    if _already_published(repo, number, run_id, reviewer_login):
        return _report(0, f"Workflow run {run_id} already published its review. Nothing to do.")

    live = _gh_api(f"repos/{repo}/pulls/{number}")
    if not isinstance(live, dict) or live.get("state") != "open":
        return _report(1, f"Blocked: {repo}#{number} is not open. Nothing is published.")
    if live.get("draft"):
        return _report(0, f"Skipped: {repo}#{number} is now a draft. Nothing is published.")
    warning = drift_warning(repo, number, head_sha, live)

    run_url = f"{server_url}/{repo}/actions/runs/{run_id}/attempts/{run_attempt}"
    body = compose_body(
        review, engine=engine, actor=actor, run_url=run_url, marker=marker, warning=warning
    )
    payload = {"commit_id": head_sha, "body": body, "event": "COMMENT", "comments": comments}
    if dry_run_out is not None:
        _write_json(dry_run_out, payload)
        return _report(0, f"Dry run: the review request is in {dry_run_out}. Nothing is posted.")

    path = f"repos/{repo}/pulls/{number}/reviews"
    try:
        created = _gh_api(path, method="POST", payload=payload)
    except GhError as exc:
        # 422 is how GitHub refuses an inline comment on a line outside the diff.
        # A body-only retry is safe because GitHub made no review.
        if exc.status != 422 or not comments:
            raise Stop(1, f"Blocked: {exc}. Nothing is posted again.") from exc
        comments = []
        body = compose_body(
            review,
            engine=engine,
            actor=actor,
            run_url=run_url,
            marker=marker,
            warning=warning,
            inline_note=INLINE_FALLBACK_NOTE,
        )
        payload = {"commit_id": head_sha, "body": body, "event": "COMMENT", "comments": []}
        try:
            created = _gh_api(path, method="POST", payload=payload)
        except GhError as retry_exc:
            raise Stop(1, f"Blocked: {retry_exc}. Nothing is posted again.") from retry_exc
    review_id = created.get("id") if isinstance(created, dict) else None
    if type(review_id) is not int:
        raise Stop(1, "Blocked: GitHub returned no review ID. Check the PR by hand.")
    _verify_published(repo, number, review_id, head_sha, body, comments, reviewer_login)
    return _report(
        0,
        f"Published review {review_id} on {repo}#{number} at {head_sha} "
        f"with {len(comments)} inline comments.",
    )


# --------------------------------------------------------------------------
# summary
# --------------------------------------------------------------------------


def summary(*, repo: str, number: int, review_path: Path, head_sha: str, engine: str) -> int:
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        raise Stop(1, "Blocked: GITHUB_STEP_SUMMARY is not set, so there is no run summary.")
    review, _ = _checked_review(review_path, repo, number, head_sha, engine)
    first_line = SUMMARY_FIRST_LINE.format(
        repo=repo, number=number, head_sha=head_sha, engine=engine
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

    pub = commands.add_parser("publish", help="post one COMMENT review")
    pub.add_argument("--repo", required=True, type=repo_arg)
    pub.add_argument("--pr", required=True, type=pr_arg)
    pub.add_argument("--review", required=True, type=Path)
    pub.add_argument("--head", required=True, type=_pattern(SHA_RE, "head SHA"))
    pub.add_argument("--run-id", required=True, type=_pattern(RUN_NUMBER_RE, "run ID"))
    pub.add_argument("--run-attempt", required=True, type=_pattern(RUN_NUMBER_RE, "attempt"))
    pub.add_argument("--server-url", required=True, type=_pattern(SERVER_URL_RE, "server URL"))
    pub.add_argument("--engine", required=True, choices=ENGINES)
    pub.add_argument("--actor", required=True, type=_pattern(LOGIN_RE, "login"))
    pub.add_argument(
        "--reviewer-login", default=DEFAULT_REVIEWER_LOGIN, type=_pattern(LOGIN_RE, "login")
    )
    pub.add_argument("--dry-run", type=Path, help="write the request to this file; post nothing")

    summ = commands.add_parser("summary", help="add the review to the job summary")
    summ.add_argument("--repo", required=True, type=repo_arg)
    summ.add_argument("--pr", required=True, type=pr_arg)
    summ.add_argument("--review", required=True, type=Path)
    summ.add_argument("--head", required=True, type=_pattern(SHA_RE, "head SHA"))
    summ.add_argument("--engine", required=True, choices=ENGINES)
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
            actor=args.actor,
            reviewer_login=args.reviewer_login,
            dry_run_out=args.dry_run,
        )
    except Stop as stop:
        return _report(stop.code, stop.message)
    except GhError as exc:
        return _report(1, f"Blocked: {exc}")


if __name__ == "__main__":
    sys.exit(main())
