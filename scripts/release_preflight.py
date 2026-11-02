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

WHAT A SCHEDULED RUN CHECKS
---------------------------
0. The run is on ``refs/heads/main``. Releases are cut from the trunk only.
1. The newest stable ``vMAJOR.MINOR.PATCH`` tag reachable from the release SHA is the
   previous release. No such tag is a refusal. Other tags (``v2.6.0rc1``, benchmark tags)
   are ignored, matching ``cliff.toml``'s ``tag_pattern``.
2. No commits since that tag: skip the week (no empty releases).
3. The previous release must be a published GitHub Release and must be on PyPI. A release
   that stopped half way is recovered by a maintainer; a later schedule never skips it.
4. ``ci.yml`` must have completed successfully for the exact release SHA. Missing, pending
   or cancelled is a refusal: an unverified release is worse than a late one.
5. The bump is derived from Conventional Commits, reading only where the spec puts the
   markers: a ``!`` subject or a ``BREAKING CHANGE:`` footer selects ``major``, a ``feat``
   subject ``minor``, anything else ``patch``. A marker elsewhere in a body (a quote, a
   pasted commit list) never changes the bump; on a schedule it is a refusal instead, since
   a squash merge can carry a real break in a squashed commit's footer.
6. A derived ``major`` is a refusal, never a release. Nothing between the cron and a public
   tag, GitHub Release and TestPyPI upload involves a person (only the final PyPI step has
   required reviewers), so the run goes red, lists the breaking commits, and leaves the
   major to a maintainer: run Release by hand with ``bump=major``. Capping it to ``minor``
   was rejected: shipping breaking changes under a minor breaks everyone pinned ``<3``.

WHAT A MANUAL DISPATCH CHECKS (review on #652: it used to skip everything)
--------------------------------------------------------------------------
A person chose the bump, and may be deliberately superseding a broken release, so only the
checks that protect what ships are enforced: the run is on ``main``, the bump is one of
``patch``/``minor``/``major``, and ``ci.yml`` is green at the exact SHA (``main`` has no
required status checks of its own). The previous-release check, an empty window, and a
derived bump that differs from the chosen one are WARNINGS in the summary.

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
MAIN_REF = "refs/heads/main"
EVENTS = ("schedule", "workflow_dispatch")

#: Maintained allow-list of release-blocking workflows. Add a workflow file here only when
#: release policy requires it; ``ci.yml`` is the one that runs on every push to ``main``.
REQUIRED_WORKFLOWS = ("ci.yml",)

STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

# WHERE A MARKER COUNTS (review on #652, N3). The shell matched every line of every message,
# so a body that quoted `BREAKING CHANGE:` or pasted `fix!:` escalated the bump. This repo
# squash-merges, so bodies routinely carry PR descriptions and nested commit lists.
# Conventional Commits puts `!` and the type in the SUBJECT and `BREAKING CHANGE:` in the
# FOOTER, so only those two places are read. See `parse_message` for what "footer" means.
BANG_SUBJECT = re.compile(r"^[A-Za-z]+(?:\([^)]*\))?!:")
FEAT_SUBJECT = re.compile(r"^feat(?:\([^)]*\))?!?:", re.IGNORECASE)
FOOTER_START = re.compile(r"^(?:BREAKING[ -]CHANGE|[A-Za-z][A-Za-z0-9-]*)(?::\s|\s#)", re.I)
BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.IGNORECASE)
DASH_RULE = re.compile(r"^\s*-{3,}\s*$")
FENCE = re.compile(r"^\s*(?:```|~~~)")
# A marker anywhere else in the body. It does not change the bump, but a scheduled run cannot
# tell a quote from a real break inside a squashed commit, so it hands the week to a person.
UNCLEAR_MARKER = re.compile(
    r"^\s*(?:BREAKING[ -]CHANGE\b|(?:[*-]\s+)?[A-Za-z]+(?:\([^)]*\))?!:)", re.IGNORECASE
)

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


@dataclass(frozen=True)
class Commit:
    sha: str
    message: str

    @property
    def subject(self) -> str:
        return self.message.strip().split("\n", 1)[0].strip()


@dataclass(frozen=True)
class Parsed:
    subject: str
    footer: tuple[str, ...]
    body: tuple[str, ...]


def _unfence(lines: Sequence[str]) -> List[str]:
    """Drop fenced code blocks, keeping one prose line in their place.

    An example inside ``` is not a footer and not a marker. An UNTERMINATED fence is left
    as text: swallowing the rest of a message could hide a real footer after it.
    """
    openers = [i for i, line in enumerate(lines) if FENCE.match(line)]
    if len(openers) % 2:
        openers = openers[:-1]
    fenced: set[int] = set()
    for start, stop in zip(openers[0::2], openers[1::2]):
        fenced.update(range(start + 1, stop + 1))
    return [
        "(fenced block)" if i in openers[0::2] else line
        for i, line in enumerate(lines)
        if i not in fenced
    ]


def parse_message(message: str) -> Parsed:
    """Split a commit message into its subject, its footer lines and the rest of the body.

    The footer is the trailing run of paragraphs whose first line is a ``Token: value`` or
    ``Token #value`` footer (``BREAKING CHANGE`` included), walking back from the end. A
    paragraph that is only a dash rule is stepped over, because a GitHub squash ends with
    ``---------`` and a ``Co-authored-by:`` paragraph AFTER the real footer. That is the
    shape of e2e6318 on main, whose genuine ``BREAKING CHANGE:`` footer is followed by a
    ``Co-Authored-By`` paragraph, the rule and the squash trailers: reading only the last
    paragraph would miss it. The first paragraph that is not footer-shaped ends the footer.
    """
    lines = message.replace("\r\n", "\n").strip("\n").split("\n")
    subject = lines[0].strip()
    paragraphs: List[List[str]] = []
    current: List[str] = []
    for line in _unfence(lines[1:]):
        if line.strip():
            current.append(line.rstrip())
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)

    footer_at = len(paragraphs)
    for index in range(len(paragraphs) - 1, -1, -1):
        first = paragraphs[index][0]
        if len(paragraphs[index]) == 1 and DASH_RULE.match(first):
            continue
        if not FOOTER_START.match(first):
            break
        footer_at = index
    footer = tuple(line for p in paragraphs[footer_at:] for line in p)
    body = tuple(line for p in paragraphs[:footer_at] for line in p)
    return Parsed(subject, footer, body)


@dataclass(frozen=True)
class Derivation:
    bump: str
    breaking: tuple[Commit, ...] = ()
    features: tuple[Commit, ...] = ()
    #: (commit, line) pairs: a marker outside the subject and the footer.
    unclear: tuple[tuple[Commit, str], ...] = ()


def derive_bump(commits: Sequence[Commit]) -> Derivation:
    """``major`` on a ``!`` subject or a ``BREAKING CHANGE`` footer, ``minor`` on a ``feat``
    subject, else ``patch``. Markers elsewhere in a body never change the bump."""
    breaking: List[Commit] = []
    features: List[Commit] = []
    unclear: List[tuple[Commit, str]] = []
    for c in commits:
        parsed = parse_message(c.message)
        if BANG_SUBJECT.match(parsed.subject) or any(
            BREAKING_FOOTER.match(line) for line in parsed.footer
        ):
            # Already breaking: a marker in its body (a squash repeats its own `!` subject
            # as the first bullet, as 93700ab4 does) adds nothing to report.
            breaking.append(c)
            continue
        if FEAT_SUBJECT.match(parsed.subject):
            features.append(c)
        unclear.extend((c, line.strip()) for line in parsed.body if UNCLEAR_MARKER.match(line))
    bump = "major" if breaking else "minor" if features else "patch"
    return Derivation(bump, tuple(breaking), tuple(features), tuple(unclear))


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


def decide(
    event: str,
    sha: str,
    probe: Probe,
    ref: str = MAIN_REF,
    requested_bump: str = "",
) -> Decision:
    if event not in EVENTS:
        return Decision(
            should_release=False,
            bump="patch",
            problems=[f"release.yml runs on {' or '.join(EVENTS)} only, not {event!r}"],
        )
    manual = event == "workflow_dispatch"
    decision = Decision(should_release=False, bump="patch")
    if ref != MAIN_REF:
        decision.problems.append(f"releases are cut from {MAIN_REF} only; this run is on {ref!r}")
    if manual:
        if requested_bump in BUMPS:
            decision.bump = requested_bump
        else:
            decision.problems.append(
                f"the bump must be one of {', '.join(BUMPS)}, not {requested_bump!r}"
            )

    tag = latest_stable_tag(probe.stable_tags(sha))
    if tag is None:
        missing = f"no stable v<major>.<minor>.<patch> tag is reachable from {sha}"
        if not manual:
            decision.problems.append(missing)
            return decision
        decision.warnings.append(missing)
        decision.problems.extend(_check_ci(probe, sha))
        decision.should_release = not decision.refused
        return decision

    commits = probe.commits(tag, sha)
    decision.last_tag, decision.count = tag, len(commits)
    if not commits and not manual:
        decision.notes.append(f"no commits since {tag}; skipping this scheduled release")
        return decision
    if not commits:
        decision.warnings.append(f"no commits since {tag}; this repeats its tree as a new version")

    derivation = derive_bump(commits)
    decision.derivation = derivation
    prior = _check_prior_release(probe, tag)
    ci = _check_ci(probe, sha)
    if manual:
        decision.problems.extend(ci)
        decision.warnings.extend(prior)
        if requested_bump in BUMPS and derivation.bump != requested_bump:
            decision.warnings.append(
                f"the commits since {tag} call for a {derivation.bump} bump; "
                f"this dispatch chose {requested_bump}"
            )
        if derivation.unclear:
            decision.warnings.append(
                "breaking-change markers outside a subject or footer; see the list below"
            )
    else:
        decision.bump = derivation.bump
        decision.problems.extend(prior)
        decision.problems.extend(ci)
        if decision.bump == "major":
            decision.problems.append(major_refusal(tag, derivation))
        elif derivation.unclear:
            # Only when the window is not already a major: that refusal already sends the
            # week to a person, and "otherwise bump=major" would be no choice at all.
            decision.problems.append(unclear_refusal(derivation))
    decision.should_release = not decision.refused
    return decision


def unclear_refusal(derivation: Derivation) -> str:
    shas = sorted({c.sha[:8] for c, _ in derivation.unclear})
    return (
        f"{len(shas)} commit(s) ({', '.join(shas)}) "
        "carry a breaking-change marker outside the subject and the footer. That "
        "is usually a quote, but in a squash merge it can be a real break from one of the "
        "squashed commits, and a scheduled run cannot tell which. Run the Release workflow "
        f"by hand: bump=major if it is breaking, otherwise bump={derivation.bump}"
    )


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


def outputs(decision: Decision) -> dict[str, str]:
    values = {
        "bump": decision.bump,
        "count": str(decision.count if decision.count is not None else 0),
        "last_tag": decision.last_tag,
        "should_release": "true" if decision.should_release and not decision.refused else "false",
    }
    assert values["bump"] in BUMPS
    assert values["count"].isdigit()
    assert values["last_tag"] == "" or STABLE_TAG.match(values["last_tag"])
    return values


def summary(decision: Decision, event: str, sha: str) -> str:
    lines = [f"## Release preflight ({event} at `{sha[:12]}`)", ""]
    if decision.refused:
        lines.append("**Refused.** Nothing was bumped, tagged or published.")
    elif decision.should_release:
        chosen = "chosen by the dispatch" if event == "workflow_dispatch" else "derived"
        lines.append(f"**Releasing** with a `{decision.bump}` bump ({chosen}).")
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
        if derivation.unclear:
            lines.extend(["", "### Markers outside a subject or footer", ""])
            lines.extend(
                f"- `{c.sha[:8]}` {c.subject}: `{line[:120].replace('`', chr(39))}`"
                for c, line in derivation.unclear
            )
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
    parser.add_argument("--ref", required=True, help="GITHUB_REF; releases come from main")
    parser.add_argument(
        "--requested-bump", default="", help="inputs.bump on a manual dispatch (else empty)"
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="the git checkout")
    parser.add_argument("--pypi", default=PYPI, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if not FULL_SHA.match(args.sha):
        parser.error(f"--sha must be a full 40-character commit SHA, got {args.sha!r}")

    live = probe or LiveProbe(repo=args.repo, repo_root=args.repo_root, pypi=args.pypi)
    decision = decide(args.event, args.sha, live, args.ref, args.requested_bump)

    values = outputs(decision)
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
