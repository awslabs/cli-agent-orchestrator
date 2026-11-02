"""Tests for ``scripts/release_preflight.py``, the policy behind ``release.yml``'s cron.

The policy used to be shell inside the workflow, and the reviews on PR #652 could only test
it by replaying it by hand against ``main``. Every decision is now a function: these tests
drive it with a fake ``Probe`` (no network, no ``gh``), and drive the git half against real
throwaway repositories, because tag reachability and message parsing are git's behaviour,
not ours.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_preflight.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

SHA = "a" * 40


def _load_module():
    spec = importlib.util.spec_from_file_location("release_preflight", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: dataclasses resolves string annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rp = _load_module()


def commit(message: str, sha: str = "") -> Any:
    return rp.Commit(sha or f"{abs(hash(message)):040x}"[:40], message)


@dataclass
class FakeProbe:
    """The world as a test states it. Records every question so tests can pin which were asked."""

    tags: List[str] = field(default_factory=lambda: ["v2.5.3", "v2.5.4"])
    history: List[Any] = field(default_factory=lambda: [commit("fix: a bug")])
    release: str = "published"
    on_pypi: Optional[bool] = True
    ci: str = "completed:success"
    asked: List[str] = field(default_factory=list)

    def stable_tags(self, sha: str) -> List[str]:
        self.asked.append("tags")
        return list(self.tags)

    def commits(self, base: str, sha: str) -> List[Any]:
        self.asked.append(f"commits {base}")
        return list(self.history)

    def release_state(self, tag: str) -> str:
        self.asked.append(f"release {tag}")
        return self.release

    def pypi_has(self, version: str) -> Optional[bool]:
        self.asked.append(f"pypi {version}")
        return self.on_pypi

    def ci_state(self, sha: str, workflow: str) -> str:
        self.asked.append(f"ci {workflow}")
        return self.ci


# --------------------------------------------------------------------------------------
# Real git repositories
# --------------------------------------------------------------------------------------


def _git_env(home: Path) -> Dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


class Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.env = _git_env(root.parent)
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def commit(self, message: str) -> str:
        path = self.root.parent / "message.txt"
        path.write_text(message, encoding="utf-8")
        self.git("commit", "-q", "--allow-empty", "--cleanup=verbatim", "-F", str(path))
        return self.git("rev-parse", "HEAD")

    def probe(self) -> Any:
        return rp.LiveProbe(repo="owner/name", repo_root=self.root, run=_git_only(self.env))


def _git_only(env: Dict[str, str]):
    """A runner that executes git for real and refuses anything else (no gh in tests)."""

    def run(argv, **kwargs):
        assert argv[0] == "git", f"tests must not call {argv[0]}"
        kwargs.setdefault("env", env)
        return subprocess.run(argv, **kwargs)

    return run


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    root = tmp_path / "repo"
    root.mkdir()
    return Repo(root)


# --------------------------------------------------------------------------------------
# Tag selection
# --------------------------------------------------------------------------------------


class TestLatestStableTag:
    def test_picks_the_numerically_highest(self):
        assert rp.latest_stable_tag(["v2.5.4", "v2.10.0", "v2.9.9"]) == "v2.10.0"

    @pytest.mark.parametrize("tag", ["v2.6.0rc1", "benchmark-2026-08", "v2.6", "2.6.0", "v2.6.0-1"])
    def test_ignores_anything_that_is_not_a_stable_release_tag(self, tag):
        assert rp.latest_stable_tag(["v2.5.4", tag]) == "v2.5.4"

    def test_none_when_there_is_no_stable_tag(self):
        assert rp.latest_stable_tag(["v3.0.0rc1", "nightly"]) is None

    def test_only_tags_merged_into_the_release_sha_count(self, repo):
        repo.commit("chore: init")
        repo.git("tag", "v1.0.0")
        base = repo.git("rev-parse", "HEAD")
        repo.git("checkout", "-q", "-b", "side")
        repo.commit("feat: unmerged work")
        repo.git("tag", "v9.0.0")
        repo.git("checkout", "-q", "main")
        repo.commit("fix: on main")
        head = repo.git("rev-parse", "HEAD")
        tags = repo.probe().stable_tags(head)
        assert "v9.0.0" not in tags
        assert rp.latest_stable_tag(tags) == "v1.0.0"
        assert base != head


# --------------------------------------------------------------------------------------
# Reading history
# --------------------------------------------------------------------------------------


class TestReadingHistory:
    def test_commits_since_the_tag_keep_their_whole_message(self, repo):
        repo.commit("chore: init")
        repo.git("tag", "v1.0.0")
        repo.commit("fix: one\n\n#123 starts a body line\n")
        repo.commit("feat: two\n\nbody\n\nBREAKING CHANGE: gone\n")
        head = repo.git("rev-parse", "HEAD")
        commits = repo.probe().commits("v1.0.0", head)
        assert [c.subject for c in commits] == ["feat: two", "fix: one"]
        assert "BREAKING CHANGE: gone" in commits[0].message
        assert "#123 starts a body line" in commits[1].message
        assert all(rp.FULL_SHA.match(c.sha) for c in commits)

    def test_an_empty_week_has_no_commits(self, repo):
        repo.commit("chore: init")
        repo.git("tag", "v1.0.0")
        head = repo.git("rev-parse", "HEAD")
        assert repo.probe().commits("v1.0.0", head) == []


# --------------------------------------------------------------------------------------
# The bump derivation
# --------------------------------------------------------------------------------------


class TestDeriveBump:
    def test_fixes_only_is_a_patch(self):
        assert rp.derive_bump([commit("fix: a"), commit("docs: b")]).bump == "patch"

    def test_a_feature_is_a_minor(self):
        assert rp.derive_bump([commit("fix: a"), commit("feat(cli): b")]).bump == "minor"

    def test_a_bang_subject_is_a_major(self):
        assert rp.derive_bump([commit("feat(cli)!: drop a flag")]).bump == "major"

    def test_a_breaking_change_footer_is_a_major(self):
        message = "feat: x\n\nbody\n\nBREAKING CHANGE: the --json shape changed\n"
        derivation = rp.derive_bump([commit(message)])
        assert derivation.bump == "major"
        assert [c.subject for c in derivation.breaking] == ["feat: x"]

    def test_an_empty_window_is_a_patch(self):
        assert rp.derive_bump([]).bump == "patch"


#: The shape of e2e6318 on main (#525), abridged: a genuine footer that is NOT the last
#: paragraph, because a GitHub squash appends a rule and its own trailers after it.
E2E6318_SHAPE = """feat(workflow): async run submit, discovery, and live event following (#505) (#525)

* feat(workflow): async run submit

Records both breaking changes in the CHANGELOG.

BREAKING CHANGE: `cao workflow run --json` emits `{run_id, state}` rather
than the full WorkflowRunResult. Use `cao workflow result <id>`
for full detail; per-step outputs are unaffected.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>

---------

Co-authored-by: Claude Opus 5 (1M context) <noreply@anthropic.com>
"""

#: The shape of 4f1b8f16 on main (#634): a squashed commit's `!` subject inside the body.
SQUASHED_BANG = """feat(cli): add cao agent CLI commands for in-session orchestration (#616) (#634)

* feat(cli): add cao agent CLI commands

* feat(cli)!: drop --idempotency-key; keep the substrate for the run record

---------

Co-authored-by: Someone <someone@example.invalid>
"""


class TestWhereAMarkerCounts:
    """Review on #652, N3: only the subject and the footer carry Conventional Commits markers."""

    @pytest.mark.parametrize(
        "message",
        [
            "feat: x\n\nbody text\n\nBREAKING CHANGE: the --json shape changed\n",
            "fix: x\n\nBREAKING CHANGE: one paragraph body that is the footer\n",
            "fix: x\n\nbody\n\nRefs: #12\nBREAKING-CHANGE: the synonym spelling\n",
            "fix: x\n\nbody\n\nbreaking change: lower case still reads as breaking\n",
            "fix: x\n\nbody\n\nBREAKING CHANGE: real\n\nSigned-off-by: A <a@example.invalid>\n",
            E2E6318_SHAPE,
        ],
        ids=["footer", "only-paragraph", "synonym", "lower-case", "then-trailers", "e2e6318"],
    )
    def test_a_breaking_change_footer_is_a_major(self, message):
        derivation = rp.derive_bump([commit(message)])
        assert derivation.bump == "major"
        assert derivation.unclear == ()

    @pytest.mark.parametrize(
        "subject", ["feat(cli)!: drop a flag", "fix!: x", "FIX!: shouted", "build(deps)!: py3.11"]
    )
    def test_a_bang_subject_is_a_major(self, subject):
        assert rp.derive_bump([commit(subject)]).bump == "major"

    @pytest.mark.parametrize(
        "message",
        [
            "docs: explain footers\n\n> BREAKING CHANGE: an example in a quote\n\nMore prose.\n",
            "docs: explain footers\n\n```\nBREAKING CHANGE: an example\n\nfix!: another\n```\n",
            "docs: x\n\nSee the guide.\n\n```text\nBREAKING CHANGE: example\n```\n\nDone.\n",
            "docs: x\n\nThe `BREAKING CHANGE:` footer is documented here.\n",
        ],
        ids=["markdown-quote", "fenced-last", "fenced-middle", "inline-code"],
    )
    def test_a_quoted_breaking_change_is_not_a_major_and_not_unclear(self, message):
        derivation = rp.derive_bump([commit(message)])
        assert derivation.bump == "patch"
        assert derivation.breaking == () and derivation.unclear == ()

    def test_a_revert_subject_quoting_a_bang_is_not_a_major(self):
        assert rp.derive_bump(
            [commit('Revert "feat!: drop a flag"\n\nThis reverts abc.')]
        ).bump == ("patch")

    def test_a_marker_in_the_middle_of_a_body_does_not_change_the_bump(self):
        message = "fix: x\n\nBREAKING CHANGE: quoted from the issue\n\nThis paragraph is prose.\n"
        derivation = rp.derive_bump([commit(message)])
        assert derivation.bump == "patch"
        assert [line for _, line in derivation.unclear] == [
            "BREAKING CHANGE: quoted from the issue"
        ]

    def test_a_squashed_commits_bang_subject_is_unclear_not_major(self):
        derivation = rp.derive_bump([commit(SQUASHED_BANG)])
        assert derivation.bump == "minor"
        assert [line for _, line in derivation.unclear] == [
            "* feat(cli)!: drop --idempotency-key; keep the substrate for the run record"
        ]

    def test_feat_counts_only_in_the_subject(self):
        message = "fix: y\n\nfeat: this line only mentions a feature\n\nMore prose.\n"
        assert rp.derive_bump([commit(message)]).bump == "patch"

    @pytest.mark.parametrize("subject", ["feat: x", "feat(tui): x", "Feat: x"])
    def test_a_feat_subject_is_a_minor(self, subject):
        assert rp.derive_bump([commit(subject)]).bump == "minor"

    def test_an_unterminated_fence_does_not_hide_a_footer(self):
        message = "fix: x\n\n```\nnever closed\n\nBREAKING CHANGE: real\n"
        assert rp.derive_bump([commit(message)]).bump == "major"

    def test_the_footer_stops_at_the_first_prose_paragraph(self):
        parsed = rp.parse_message(E2E6318_SHAPE)
        assert parsed.footer[0].startswith("BREAKING CHANGE:")
        assert parsed.footer[-1].startswith("Co-authored-by:")
        assert "Records both breaking changes in the CHANGELOG." in parsed.body
        assert "---------" in parsed.footer

    def test_an_empty_week_derives_nothing(self):
        derivation = rp.derive_bump([])
        assert (derivation.bump, derivation.breaking, derivation.unclear) == ("patch", (), ())

    def test_real_git_messages_parse_the_same_way(self, repo):
        repo.commit("chore: init")
        repo.git("tag", "v1.0.0")
        repo.commit(E2E6318_SHAPE)
        repo.commit("docs: x\n\n> BREAKING CHANGE: quoted\n")
        head = repo.git("rev-parse", "HEAD")
        derivation = rp.derive_bump(repo.probe().commits("v1.0.0", head))
        assert derivation.bump == "major"
        assert [c.subject for c in derivation.breaking] == [E2E6318_SHAPE.split("\n", 1)[0]]


class TestUnclearMarkersOnASchedule:
    def test_an_unclear_marker_hands_the_week_to_a_person(self):
        probe = FakeProbe(history=[commit(SQUASHED_BANG, sha="4" * 40)])
        decision = rp.decide("schedule", SHA, probe)
        assert decision.refused and not decision.should_release
        (problem,) = decision.problems
        assert "44444444" in problem
        assert "bump=major if it is breaking, otherwise bump=minor" in problem

    def test_the_summary_shows_the_line(self, tmp_path, monkeypatch):
        code, _, _, report = _run_main(
            tmp_path,
            monkeypatch,
            ["--event", "schedule", *BASE_ARGS],
            FakeProbe(history=[commit(SQUASHED_BANG)]),
        )
        assert code == 1
        assert "### Markers outside a subject or footer" in report
        assert "drop --idempotency-key" in report

    def test_a_quoted_marker_releases_normally(self):
        message = "docs: explain\n\n> BREAKING CHANGE: an example\n\nMore prose.\n"
        decision = rp.decide("schedule", SHA, FakeProbe(history=[commit(message)]))
        assert decision.should_release and decision.bump == "patch"


# --------------------------------------------------------------------------------------
# The scheduled decision
# --------------------------------------------------------------------------------------


class TestScheduledDecision:
    def test_releases_with_the_derived_bump_when_every_gate_passes(self):
        probe = FakeProbe(history=[commit("feat: x"), commit("fix: y")])
        decision = rp.decide("schedule", SHA, probe)
        assert decision.should_release and not decision.refused
        assert decision.bump == "minor"
        assert decision.last_tag == "v2.5.4" and decision.count == 2

    def test_no_stable_tag_is_a_refusal_with_its_own_message(self):
        decision = rp.decide("schedule", SHA, FakeProbe(tags=["v3.0.0rc1"]))
        assert decision.refused and not decision.should_release
        assert "no stable" in decision.problems[0]

    def test_an_empty_week_skips_without_asking_github_or_pypi(self):
        probe = FakeProbe(history=[])
        decision = rp.decide("schedule", SHA, probe)
        assert not decision.should_release and not decision.refused
        assert decision.count == 0
        assert probe.asked == ["tags", "commits v2.5.4"]

    @pytest.mark.parametrize("state", ["draft", "missing or unreadable: HTTP 404"])
    def test_a_previous_release_that_is_not_published_refuses(self, state):
        decision = rp.decide("schedule", SHA, FakeProbe(release=state))
        assert decision.refused and not decision.should_release
        assert "v2.5.4 is not a published GitHub Release" in decision.problems[0]

    @pytest.mark.parametrize("on_pypi", [False, None])
    def test_a_previous_release_missing_from_pypi_refuses_even_when_pypi_is_down(self, on_pypi):
        decision = rp.decide("schedule", SHA, FakeProbe(on_pypi=on_pypi))
        assert decision.refused
        assert "not verifiably on PyPI" in decision.problems[0]

    @pytest.mark.parametrize(
        "state", ["missing", "in_progress:none", "completed:failure", "completed:cancelled"]
    )
    def test_ci_that_is_not_green_at_the_exact_sha_refuses(self, state):
        decision = rp.decide("schedule", SHA, FakeProbe(ci=state))
        assert decision.refused
        assert decision.problems == [f"ci.yml is '{state}' at {SHA}; refusing to release"]

    def test_every_failed_gate_is_reported_not_just_the_first(self):
        decision = rp.decide(
            "schedule", SHA, FakeProbe(release="draft", on_pypi=False, ci="completed:failure")
        )
        assert len(decision.problems) == 3


class TestNoUnattendedMajor:
    """Review on #652: as written, the next scheduled run would have cut v3.0.0 unattended.

    The tag, the GitHub Release and the TestPyPI upload all happen before the first person
    (the `pypi` approver) is involved, so a major must be refused, not merely flagged.
    """

    BREAKING = [
        commit("feat(cli)!: drop the --json flag", sha="1" * 40),
        commit("fix: x\n\nbody\n\nBREAKING CHANGE: the API moved\n", sha="2" * 40),
    ]

    @pytest.mark.parametrize("breaking", BREAKING, ids=["bang-subject", "footer"])
    def test_a_derived_major_is_refused(self, breaking):
        probe = FakeProbe(history=[commit("feat: ok"), breaking])
        decision = rp.decide("schedule", SHA, probe)
        assert decision.refused and not decision.should_release
        assert decision.bump == "major"
        (problem,) = decision.problems
        assert "never cuts a major" in problem
        assert breaking.sha[:8] in problem
        assert "bump=major" in problem

    def test_the_refusal_is_red_and_lists_the_breaking_commits(self, tmp_path, monkeypatch):
        code, values, _, report = _run_main(
            tmp_path,
            monkeypatch,
            ["--event", "schedule", *BASE_ARGS],
            FakeProbe(history=list(self.BREAKING)),
        )
        assert code == 1
        assert values["should_release"] == "false"
        assert values["bump"] == "major"
        assert "### Breaking" in report
        assert "drop the --json flag" in report

    def test_a_major_is_reported_alongside_the_other_gates(self):
        decision = rp.decide("schedule", SHA, FakeProbe(history=self.BREAKING[:1], ci="missing"))
        assert len(decision.problems) == 2

    def test_minor_and_patch_windows_still_release_unattended(self):
        for history, bump in (([commit("feat: a")], "minor"), ([commit("fix: a")], "patch")):
            decision = rp.decide("schedule", SHA, FakeProbe(history=history))
            assert decision.should_release and decision.bump == bump


def dispatch(probe: Any, bump: str = "patch", ref: str = "refs/heads/main") -> Any:
    return rp.decide("workflow_dispatch", SHA, probe, ref, bump)


class TestManualDispatch:
    """Review on #652 (haofeif): `workflow_dispatch` used to bypass every gate.

    `main` has no required status checks, so without this a person could cut a release from
    a commit whose CI is red or still running. The checks that protect what ships are now
    enforced on a dispatch too; the ones a person can judge are warnings.
    """

    @pytest.mark.parametrize("bump", ["patch", "minor", "major"])
    def test_releases_the_chosen_bump_when_ci_is_green(self, bump):
        probe = FakeProbe(history=[commit("fix: x")])
        decision = dispatch(probe, bump)
        assert decision.should_release and decision.bump == bump
        assert "ci ci.yml" in probe.asked

    @pytest.mark.parametrize("state", ["missing", "in_progress:none", "completed:failure"])
    def test_red_or_pending_ci_refuses_a_dispatch_too(self, state):
        decision = dispatch(FakeProbe(ci=state))
        assert decision.refused and not decision.should_release
        assert decision.problems == [f"ci.yml is '{state}' at {SHA}; refusing to release"]

    @pytest.mark.parametrize("bump", ["", "huge", "Major"])
    def test_a_bump_outside_the_three_choices_refuses(self, bump):
        decision = dispatch(FakeProbe(), bump)
        assert decision.refused
        assert "the bump must be one of patch, minor, major" in decision.problems[0]

    def test_an_incomplete_previous_release_is_a_warning_not_a_refusal(self):
        decision = dispatch(FakeProbe(release="draft", on_pypi=False))
        assert decision.should_release and not decision.refused
        assert len(decision.warnings) == 2
        assert "v2.5.4 is not a published GitHub Release" in decision.warnings[0]

    def test_a_bump_that_differs_from_the_derived_one_is_a_warning(self):
        probe = FakeProbe(history=[commit("feat!: drop a flag")])
        decision = dispatch(probe, "patch")
        assert decision.should_release and decision.bump == "patch"
        assert decision.warnings == [
            "the commits since v2.5.4 call for a major bump; this " "dispatch chose patch"
        ]

    def test_a_major_can_be_cut_by_hand(self):
        probe = FakeProbe(history=[commit("feat!: drop a flag")])
        decision = dispatch(probe, "major")
        assert decision.should_release and decision.bump == "major" and not decision.warnings

    def test_an_unclear_marker_is_a_warning_on_a_dispatch(self):
        probe = FakeProbe(history=[commit(SQUASHED_BANG)])
        decision = dispatch(probe, "minor")
        assert decision.should_release
        assert any("outside a subject or footer" in w for w in decision.warnings)

    def test_an_empty_window_is_a_warning_on_a_dispatch(self):
        decision = dispatch(FakeProbe(history=[]))
        assert decision.should_release and decision.count == 0
        assert "no commits since v2.5.4" in decision.warnings[0]

    def test_no_previous_tag_still_enforces_ci(self):
        decision = dispatch(FakeProbe(tags=[], ci="completed:failure"))
        assert decision.refused
        assert "no stable" in decision.warnings[0]


class TestReleasesComeFromMain:
    @pytest.mark.parametrize("ref", ["refs/heads/feature", "refs/tags/v2.5.4", "refs/heads/v9.9.9"])
    @pytest.mark.parametrize("event", ["schedule", "workflow_dispatch"])
    def test_any_other_ref_refuses(self, event, ref):
        decision = rp.decide(event, SHA, FakeProbe(history=[commit("fix: x")]), ref, "patch")
        assert decision.refused and not decision.should_release
        assert f"releases are cut from refs/heads/main only; this run is on '{ref}'" in (
            decision.problems
        )

    @pytest.mark.parametrize("event", ["push", "release", ""])
    def test_an_event_the_workflow_does_not_declare_refuses(self, event):
        decision = rp.decide(event, SHA, FakeProbe())
        assert decision.refused and "runs on schedule or workflow_dispatch only" in (
            decision.problems[0]
        )


# --------------------------------------------------------------------------------------
# The live probe's gh and PyPI halves, with the network replaced
# --------------------------------------------------------------------------------------


class _GhRunner:
    """Stands in for ``subprocess.run`` on the gh calls, and records every argv."""

    def __init__(self, stdout: str = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr
        self.calls: List[List[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


_gh_runner = _GhRunner


class TestLiveProbeAsksGitHubReadOnly:
    def _probe(self, runner) -> Any:
        return rp.LiveProbe(repo="owner/name", repo_root=Path("."), run=runner)

    def test_a_published_release(self):
        runner = _gh_runner(json.dumps({"tag_name": "v2.5.4", "draft": False}))
        assert self._probe(runner).release_state("v2.5.4") == "published"
        assert runner.calls == [["gh", "api", "repos/owner/name/releases/tags/v2.5.4"]]

    def test_a_draft_is_not_published(self):
        runner = _gh_runner(json.dumps({"tag_name": "v2.5.4", "draft": True}))
        assert self._probe(runner).release_state("v2.5.4") == "draft"

    def test_a_missing_release_names_the_error(self):
        runner = _gh_runner(returncode=1, stderr="gh: Not Found (HTTP 404)\n")
        assert "HTTP 404" in self._probe(runner).release_state("v2.5.4")

    def test_ci_state_reads_the_newest_run_for_the_exact_sha(self):
        runs = [
            {"headSha": "b" * 40, "status": "completed", "conclusion": "success"},
            {"headSha": SHA, "status": "in_progress", "conclusion": ""},
            {"headSha": SHA, "status": "completed", "conclusion": "success"},
        ]
        runner = _gh_runner(json.dumps(runs))
        assert self._probe(runner).ci_state(SHA, "ci.yml") == "in_progress:none"
        argv = runner.calls[0]
        assert argv[:3] == ["gh", "run", "list"]
        assert argv[argv.index("--commit") + 1] == SHA
        assert argv[argv.index("--workflow") + 1] == "ci.yml"

    def test_no_run_for_the_sha_is_missing(self):
        assert self._probe(_gh_runner("[]")).ci_state(SHA, "ci.yml") == "missing"

    def test_a_gh_failure_is_unknown_not_success(self):
        state = self._probe(_gh_runner(returncode=1, stderr="HTTP 502")).ci_state(SHA, "ci.yml")
        assert state.startswith("unknown")

    @pytest.mark.parametrize("status,expected", [(200, True), (404, False), (503, None)])
    def test_pypi_answers(self, status, expected):
        probe = rp.LiveProbe(repo="o/n", repo_root=Path("."), fetch=lambda url: status)
        assert probe.pypi_has("2.5.4") is expected

    def test_pypi_unreachable_is_unknown(self):
        def boom(url: str) -> int:
            raise OSError("network is down")

        probe = rp.LiveProbe(repo="o/n", repo_root=Path("."), fetch=boom)
        assert probe.pypi_has("2.5.4") is None

    def test_pypi_is_asked_for_the_exact_version_json(self):
        seen: List[str] = []
        probe = rp.LiveProbe(repo="o/n", repo_root=Path("."), fetch=lambda u: seen.append(u) or 200)
        probe.pypi_has("2.5.4")
        assert seen == ["https://pypi.org/pypi/cli-agent-orchestrator/2.5.4/json"]


# --------------------------------------------------------------------------------------
# The command line: outputs, summary and exit codes
# --------------------------------------------------------------------------------------


def _run_main(tmp_path: Path, monkeypatch, argv: List[str], probe: Any):
    out, summ = tmp_path / "output", tmp_path / "summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    code = rp.main(argv, probe=probe)
    lines = out.read_text(encoding="utf-8").splitlines()
    return code, dict(line.split("=", 1) for line in lines), lines, summ.read_text("utf-8")


BASE_ARGS = ["--sha", SHA, "--repo", "owner/name", "--ref", "refs/heads/main"]


class TestCommandLine:
    def test_a_release_writes_exactly_four_constrained_outputs(self, tmp_path, monkeypatch):
        hostile = commit("feat: x\n\nshould_release=false\nbump=major\n")
        code, values, lines, report = _run_main(
            tmp_path, monkeypatch, ["--event", "schedule", *BASE_ARGS], FakeProbe(history=[hostile])
        )
        assert code == 0
        assert len(lines) == 4, "commit text must never reach $GITHUB_OUTPUT"
        assert values == {
            "bump": "minor",
            "count": "1",
            "last_tag": "v2.5.4",
            "should_release": "true",
        }
        assert "Releasing" in report

    def test_a_refusal_exits_1_and_says_why(self, tmp_path, monkeypatch, capsys):
        code, values, _, report = _run_main(
            tmp_path, monkeypatch, ["--event", "schedule", *BASE_ARGS], FakeProbe(ci="missing")
        )
        assert code == 1
        assert values["should_release"] == "false"
        assert "Refused" in report and "ci.yml is 'missing'" in report
        assert "::error::ci.yml is 'missing'" in capsys.readouterr().out

    def test_an_empty_week_exits_0_and_does_not_release(self, tmp_path, monkeypatch):
        code, values, _, report = _run_main(
            tmp_path, monkeypatch, ["--event", "schedule", *BASE_ARGS], FakeProbe(history=[])
        )
        assert code == 0
        assert values["should_release"] == "false" and values["count"] == "0"
        assert "Skipped" in report

    def test_a_short_sha_is_a_usage_error(self):
        argv = [
            "--event",
            "schedule",
            "--sha",
            "abc123",
            "--repo",
            "o/n",
            "--ref",
            "refs/heads/main",
        ]
        with pytest.raises(SystemExit) as exc:
            rp.main(argv, probe=FakeProbe())
        assert exc.value.code == 2

    def test_the_ref_is_required(self):
        with pytest.raises(SystemExit) as exc:
            rp.main(["--event", "schedule", "--sha", SHA, "--repo", "o/n"], probe=FakeProbe())
        assert exc.value.code == 2

    def test_a_dispatch_outputs_the_chosen_bump(self, tmp_path, monkeypatch):
        argv = ["--event", "workflow_dispatch", *BASE_ARGS, "--requested-bump", "major"]
        code, values, _, report = _run_main(tmp_path, monkeypatch, argv, FakeProbe())
        assert code == 0
        assert values == {
            "bump": "major",
            "count": "1",
            "last_tag": "v2.5.4",
            "should_release": "true",
        }
        assert "chosen by the dispatch" in report


# --------------------------------------------------------------------------------------
# The workflow wires the script in, and stays thin
# --------------------------------------------------------------------------------------


def _workflow() -> Dict[Any, Any]:
    import yaml

    document = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))
    assert True in document, "PyYAML folds `on:` to True; the triggers must be read from it"
    return document


class TestReleaseWorkflow:
    def test_a_schedule_runs_next_to_the_manual_dispatch(self):
        triggers = _workflow()[True]
        assert "workflow_dispatch" in triggers
        (entry,) = triggers["schedule"]
        minute = entry["cron"].split()[0]
        assert minute.isdigit() and minute != "0", "GitHub delays and drops top-of-hour crons"

    def test_runs_are_serialised_and_never_cancelled(self):
        concurrency = _workflow()["concurrency"]
        assert concurrency == {"group": "release", "cancel-in-progress": False}

    def test_every_job_is_guarded_to_the_upstream_repository(self):
        for name, job in _workflow()["jobs"].items():
            assert "github.repository == 'awslabs/cli-agent-orchestrator'" in job["if"], name

    def test_the_preflight_step_runs_the_script_with_inputs_from_the_environment(self):
        steps = _workflow()["jobs"]["preflight"]["steps"]
        (step,) = [s for s in steps if "release_preflight.py" in s.get("run", "")]
        assert "${{" not in step["run"]
        for flag in (
            '--event "$GITHUB_EVENT_NAME"',
            '--ref "$GITHUB_REF"',
            '--sha "$GITHUB_SHA"',
            '--requested-bump "$REQUESTED_BUMP"',
        ):
            assert flag in step["run"]
        assert step["env"]["REQUESTED_BUMP"] == "${{ inputs.bump }}"

    def test_the_bump_comes_from_the_preflight_for_both_events(self):
        """The dispatch input is validated by the preflight, then read from its output."""
        steps = _workflow()["jobs"]["release"]["steps"]
        (step,) = [s for s in steps if "bump_version.py" in s.get("run", "")]
        assert step["env"]["BUMP"] == "${{ needs.preflight.outputs.bump }}"
        assert 'bump_version.py "$BUMP"' in step["run"]
        assert "inputs.bump" not in step["run"]

    def test_the_preflight_job_cannot_write(self):
        permissions = _workflow()["jobs"]["preflight"]["permissions"]
        assert set(permissions.values()) == {"read"}

    def test_the_release_job_waits_for_a_positive_decision(self):
        job = _workflow()["jobs"]["release"]
        assert job["needs"] == "preflight"
        assert "needs.preflight.outputs.should_release == 'true'" in job["if"]


class TestTheReleaseJobCannotTagAVersionPublishWouldRefuse:
    """`verify-release-version` refuses a tag whose tree disagrees; the release job must not
    be able to produce one. 2.5.3 and 2.5.4 went out by hand because it could (#899, #946)."""

    def _steps(self) -> List[Dict[str, Any]]:
        return _workflow()["jobs"]["release"]["steps"]

    def _index(self, predicate) -> int:
        return next(i for i, s in enumerate(self._steps()) if predicate(s))

    def test_uv_is_set_up_before_the_bump(self):
        uv = self._index(lambda s: "astral-sh/setup-uv@" in s.get("uses", ""))
        bump = self._index(lambda s: "bump_version.py" in s.get("run", ""))
        assert uv < bump

    def test_the_publish_check_runs_after_the_bump_and_before_anything_is_tagged(self):
        check = self._index(lambda s: "assert_release_version.py" in s.get("run", ""))
        bump = self._index(lambda s: "bump_version.py" in s.get("run", ""))
        # The commit, the tag and the push all happen in scripts/release_commit.py.
        tag = self._index(lambda s: "release_commit.py" in s.get("run", ""))
        assert bump < check < tag
        run = self._steps()[check]["run"]
        assert '--tag "v$VERSION"' in run
        assert "--index https://test.pypi.org --index https://pypi.org" in run
        assert "${{" not in run

    def test_the_release_commit_carries_every_version_file(self):
        spec = importlib.util.spec_from_file_location(
            "release_commit", REPO_ROOT / "scripts" / "release_commit.py"
        )
        assert spec is not None and spec.loader is not None
        release_commit = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release_commit)
        assert set(release_commit.RELEASE_FILES) == {
            "pyproject.toml",
            "uv.lock",
            "CHANGELOG.md",
            ".devcontainer/features/cao/devcontainer-feature.json",
        }

    def test_uv_runs_locked_everywhere_else(self):
        assert str(_workflow()["env"]["UV_LOCKED"]) == "1"


class TestReleasingDocDescribesTheRealBehaviour:
    """The PR title said "weekly patch release" long after the bump was derived (review on
    #652); the operator doc must say what Monday actually does, and how to answer a refusal."""

    TEXT = (REPO_ROOT / "docs" / "RELEASING.md").read_text(encoding="utf-8")

    def test_it_describes_the_schedule_and_the_derived_bump(self):
        assert "every Monday at 09:17 UTC" in self.TEXT
        assert "release.yml (manual)" not in self.TEXT
        for bump in ("major", "minor", "patch"):
            assert f"| {bump}" in self.TEXT or f"**{bump}**" in self.TEXT

    def test_it_says_a_major_is_never_cut_unattended_and_what_to_do(self):
        assert "a scheduled run never cuts a major release" in self.TEXT
        assert "`bump=major`" in self.TEXT

    def test_every_refusal_the_preflight_prints_has_a_row(self):
        for phrase in (
            "a scheduled run never cuts a major release",
            "carry a breaking-change marker outside the subject and the footer",
            "is not a published GitHub Release",
            "not verifiably on PyPI",
        ):
            assert phrase in self.TEXT, phrase
            assert phrase in SCRIPT.read_text(encoding="utf-8"), phrase

    def test_it_documents_the_release_dispatch_mode(self):
        assert "environment: release" in self.TEXT
        assert "use workflow from the tag" in self.TEXT
