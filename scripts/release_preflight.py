#!/usr/bin/env python3
"""Decide whether ``release.yml`` cuts a release, and which version bump it uses.

Runs as the ``preflight`` job of ``.github/workflows/release.yml``. It reads git history and
asks GitHub and PyPI read-only questions; it never writes to git, GitHub or an index. Its
only outputs are ``key=value`` lines in ``$GITHUB_OUTPUT`` and a Markdown report in
``$GITHUB_STEP_SUMMARY``.

WHY A SCRIPT AND NOT A ``run:`` BLOCK
-------------------------------------
The policy below decides, with no human present, whether a weekly cron tags and publishes a
version. It used to be ~90 lines of shell inside the workflow, where nothing could test it:
the reviews on PR #652 found its bugs by replaying it by hand against ``main``. Here every
decision is a function with fixtures in ``test/scripts/test_release_preflight.py``.

WHAT A SCHEDULED RUN CHECKS (a manual dispatch skips all of it)
---------------------------------------------------------------
1. The newest stable ``vMAJOR.MINOR.PATCH`` tag reachable from the release SHA is the
   previous release. No such tag is a refusal. Other tags (``v2.6.0rc1``, benchmark tags)
   are ignored, matching ``cliff.toml``'s ``tag_pattern``.
2. No commits since that tag: skip the week (no empty releases).
3. The previous release must be a published GitHub Release and must be on PyPI. A release
   that stopped half way is recovered by a maintainer; a later schedule never skips it.
4. ``ci.yml`` must have completed successfully for the exact release SHA. Missing, pending
   or cancelled is a refusal: an unverified release is worse than a late one.
5. The bump is derived from Conventional Commits: a breaking marker selects ``major``,
   ``feat`` selects ``minor``, anything else ``patch``.
6. A derived ``major`` is a refusal, never a release. Nothing between the cron and a public
   tag, GitHub Release and TestPyPI upload involves a person (only the final PyPI step has
   required reviewers), so the run goes red, lists the breaking commits, and leaves the
   major to a maintainer: run Release by hand with ``bump=major``. Capping it to ``minor``
   was rejected: shipping breaking changes under a minor breaks everyone pinned ``<3``.

Every failed check is reported, not just the first, and any failure exits 1 so the run is
red. Exit 0 means the outputs say what to do (release, or skip an empty week).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Sequence

PACKAGE = "cli-agent-orchestrator"
PYPI = "https://pypi.org"
BUMPS = ("patch", "minor", "major")

#: Maintained allow-list of release-blocking workflows. Add a workflow file here only when
#: release policy requires it; ``ci.yml`` is the one that runs on every push to ``main``.
REQUIRED_WORKFLOWS = ("ci.yml",)

STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# The derivation the workflow shell used: every line of every message, subject or body.
BREAKING_LINE = re.compile(r"^[a-z]+(\([^)]*\))?!:|^BREAKING[ -]CHANGE:")
FEAT_LINE = re.compile(r"^feat(\([^)]*\))?:")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str

    @property
    def subject(self) -> str:
        return self.message.strip().split("\n", 1)[0].strip()


@dataclass(frozen=True)
class Derivation:
    bump: str
    breaking: tuple[Commit, ...] = ()
    features: tuple[Commit, ...] = ()


def derive_bump(commits: Sequence[Commit]) -> Derivation:
    """``major`` if any line carries a breaking marker, ``minor`` on a ``feat``, else patch."""
    breaking = tuple(
        c for c in commits if any(BREAKING_LINE.match(line) for line in c.message.splitlines())
    )
    features = tuple(
        c for c in commits if any(FEAT_LINE.match(line) for line in c.message.splitlines())
    )
    if breaking:
        return Derivation("major", breaking, features)
    if features:
        return Derivation("minor", breaking, features)
    return Derivation("patch", breaking, features)


def latest_stable_tag(tags: Sequence[str]) -> Optional[str]:
    """The highest ``vMAJOR.MINOR.PATCH`` tag, compared numerically; None if there is none."""
    best: Optional[tuple[int, int, int]] = None
    best_tag: Optional[str] = None
    for tag in tags:
        match = STABLE_TAG.match(tag.strip())
        if not match:
            continue
        key = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if best is None or key > best:
            best, best_tag = key, tag.strip()
    return best_tag


class Probe(Protocol):
    """Everything the decision needs to know about the world. Read-only by construction."""

    def stable_tags(self, sha: str) -> List[str]: ...

    def commits(self, base: str, sha: str) -> List[Commit]: ...

    def release_state(self, tag: str) -> str: ...

    def pypi_has(self, version: str) -> Optional[bool]: ...

    def ci_state(self, sha: str, workflow: str) -> str: ...


@dataclass
class Decision:
    should_release: bool
    bump: str
    last_tag: str = ""
    count: Optional[int] = None
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    derivation: Optional[Derivation] = None

    @property
    def refused(self) -> bool:
        return bool(self.problems)


def _check_prior_release(probe: Probe, tag: str) -> List[str]:
    problems: List[str] = []
    state = probe.release_state(tag)
    if state != "published":
        problems.append(
            f"the previous release {tag} is not a published GitHub Release ({state}); "
            "finish or repair that release first"
        )
    version = tag[1:]
    on_pypi = probe.pypi_has(version)
    if on_pypi is not True:
        detail = "not on PyPI" if on_pypi is False else "PyPI could not be asked"
        problems.append(
            f"the previous release {tag} is not verifiably on PyPI ({detail}); "
            "finish or repair that release first"
        )
    return problems


def _check_ci(probe: Probe, sha: str) -> List[str]:
    problems: List[str] = []
    for workflow in REQUIRED_WORKFLOWS:
        state = probe.ci_state(sha, workflow)
        if state != "completed:success":
            problems.append(f"{workflow} is '{state}' at {sha}; refusing to release")
    return problems


def decide(event: str, sha: str, probe: Probe) -> Decision:
    if event != "schedule":
        return Decision(should_release=True, bump="patch", notes=["manual dispatch"])

    tag = latest_stable_tag(probe.stable_tags(sha))
    if tag is None:
        return Decision(
            should_release=False,
            bump="patch",
            problems=[f"no stable v<major>.<minor>.<patch> tag is reachable from {sha}"],
        )

    commits = probe.commits(tag, sha)
    decision = Decision(should_release=False, bump="patch", last_tag=tag, count=len(commits))
    if not commits:
        decision.notes.append(f"no commits since {tag}; skipping this scheduled release")
        return decision

    decision.derivation = derive_bump(commits)
    decision.bump = decision.derivation.bump
    decision.problems.extend(_check_prior_release(probe, tag))
    decision.problems.extend(_check_ci(probe, sha))
    if decision.bump == "major":
        decision.problems.append(major_refusal(tag, decision.derivation))
    decision.should_release = not decision.refused
    return decision


def major_refusal(tag: str, derivation: Derivation) -> str:
    shas = ", ".join(c.sha[:8] for c in derivation.breaking)
    return (
        f"{len(derivation.breaking)} commit(s) since {tag} carry a breaking-change marker "
        f"({shas}); a scheduled run never cuts a major release. If the change is breaking, "
        "run the Release workflow by hand with bump=major; until then every scheduled run "
        "refuses"
    )


# --------------------------------------------------------------------------------------
# The real world: git, gh and PyPI. Every call here is a read.
# --------------------------------------------------------------------------------------


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0] if stripped else "no output"


@dataclass
class LiveProbe:
    repo: str
    repo_root: Path
    pypi: str = PYPI
    run: Runner = subprocess.run
    fetch: Optional[Callable[[str], int]] = None

    def _git(self, *args: str) -> str:
        result = self.run(
            ["git", "-C", str(self.repo_root), *args], capture_output=True, text=True, check=True
        )
        return result.stdout

    def stable_tags(self, sha: str) -> List[str]:
        return self._git("tag", "--merged", sha, "--list", "v[0-9]*").split()

    def commits(self, base: str, sha: str) -> List[Commit]:
        out = self._git("log", "-z", "--format=%H%n%B", f"{base}..{sha}")
        commits = []
        for record in out.split("\0"):
            record = record.strip("\n")
            if not record:
                continue
            commit_sha, _, message = record.partition("\n")
            commits.append(Commit(commit_sha.strip(), message))
        return commits

    def _gh(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return self.run(["gh", *args], capture_output=True, text=True, check=False)

    def release_state(self, tag: str) -> str:
        # The by-tag endpoint never returns drafts, so 200 means published; `draft` is
        # checked anyway so the answer does not depend on which token is asking.
        result = self._gh("api", f"repos/{self.repo}/releases/tags/{tag}")
        if result.returncode != 0:
            return f"missing or unreadable: {_first_line(result.stderr)}"
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "unreadable: the API answer was not JSON"
        if data.get("draft"):
            return "draft"
        return "published" if data.get("tag_name") == tag else "unreadable: tag mismatch"

    def _status(self, url: str) -> int:
        if self.fetch is not None:
            return self.fetch(url)
        request = urllib.request.Request(url, headers={"User-Agent": f"{PACKAGE} release"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                json.load(response)
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)

    def pypi_has(self, version: str) -> Optional[bool]:
        url = f"{self.pypi.rstrip('/')}/pypi/{PACKAGE}/{version}/json"
        try:
            status = self._status(url)
        except Exception as exc:  # network, DNS, TLS, not JSON: all "could not ask"
            print(f"WARNING: could not ask {self.pypi} about {version}: {exc}", file=sys.stderr)
            return None
        if status == 200:
            return True
        if status == 404:
            return False
        return None

    def ci_state(self, sha: str, workflow: str) -> str:
        result = self._gh(
            "run",
            "list",
            "--repo",
            self.repo,
            "--commit",
            sha,
            "--workflow",
            workflow,
            "--limit",
            "10",
            "--json",
            "conclusion,headSha,status",
        )
        if result.returncode != 0:
            return f"unknown ({_first_line(result.stderr)})"
        try:
            runs = json.loads(result.stdout)
        except json.JSONDecodeError:
            return "unknown (gh answered non-JSON)"
        matching = [r for r in runs if r.get("headSha") == sha]
        if not matching:
            return "missing"
        newest = matching[0]
        return f"{newest.get('status')}:{newest.get('conclusion') or 'none'}"


# --------------------------------------------------------------------------------------
# Outputs. Only values of a known shape ever reach $GITHUB_OUTPUT: commit text never does.
# --------------------------------------------------------------------------------------


def outputs(decision: Decision, event: str) -> dict[str, str]:
    manual = event != "schedule"
    values = {
        "bump": decision.bump,
        "count": "manual" if manual else str(decision.count if decision.count is not None else 0),
        "last_tag": "manual" if manual else decision.last_tag,
        "should_release": "true" if decision.should_release and not decision.refused else "false",
    }
    assert values["bump"] in BUMPS
    assert values["count"] == "manual" or values["count"].isdigit()
    assert values["last_tag"] in ("manual", "") or STABLE_TAG.match(values["last_tag"])
    return values


def summary(decision: Decision, event: str, sha: str) -> str:
    lines = [f"## Release preflight ({event} at `{sha[:12]}`)", ""]
    if decision.refused:
        lines.append("**Refused.** Nothing was bumped, tagged or published.")
    elif decision.should_release:
        lines.append(f"**Releasing** with a `{decision.bump}` bump.")
    else:
        lines.append("**Skipped.** Nothing to release.")
    if decision.last_tag:
        lines.append(f"- Previous release: `{decision.last_tag}`; commits since: {decision.count}")
    for problem in decision.problems:
        lines.append(f"- :x: {problem}")
    for warning in decision.warnings:
        lines.append(f"- :warning: {warning}")
    for note in decision.notes:
        lines.append(f"- {note}")
    derivation = decision.derivation
    if derivation is not None:
        for title, commits in (
            ("Breaking", derivation.breaking),
            ("Features", derivation.features),
        ):
            if commits:
                lines.extend(["", f"### {title}", ""])
                lines.extend(f"- `{c.sha[:8]}` {c.subject}" for c in commits)
    return "\n".join(lines) + "\n"


def _append(path_var: str, text: str) -> None:
    path = os.environ.get(path_var)
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: Optional[Sequence[str]] = None, probe: Optional[Probe] = None) -> int:
    parser = argparse.ArgumentParser(description="Decide whether release.yml cuts a release.")
    parser.add_argument("--event", required=True, help="GITHUB_EVENT_NAME")
    parser.add_argument("--sha", required=True, help="GITHUB_SHA, the commit being released")
    parser.add_argument("--repo", required=True, help="GITHUB_REPOSITORY, owner/name")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="the git checkout")
    parser.add_argument("--pypi", default=PYPI, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not FULL_SHA.match(args.sha):
        parser.error(f"--sha must be a full 40-character commit SHA, got {args.sha!r}")

    live = probe or LiveProbe(repo=args.repo, repo_root=args.repo_root, pypi=args.pypi)
    decision = decide(args.event, args.sha, live)

    values = outputs(decision, args.event)
    _append("GITHUB_OUTPUT", "".join(f"{k}={v}\n" for k, v in values.items()))
    report = summary(decision, args.event, args.sha)
    _append("GITHUB_STEP_SUMMARY", report)
    print(report, end="")
    for key, value in values.items():
        print(f"{key}={value}")
    for problem in decision.problems:
        print(f"::error::{problem}")
    for warning in decision.warnings:
        print(f"::warning::{warning}")
    return 1 if decision.refused else 0


if __name__ == "__main__":
    sys.exit(main())
