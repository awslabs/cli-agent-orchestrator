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


class TestManualDispatch:
    def test_a_manual_dispatch_releases_without_asking_anything(self):
        probe = FakeProbe()
        decision = rp.decide("workflow_dispatch", SHA, probe)
        assert decision.should_release and probe.asked == []


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


BASE_ARGS = ["--sha", SHA, "--repo", "owner/name"]


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
        with pytest.raises(SystemExit) as exc:
            rp.main(["--event", "schedule", "--sha", "abc123", "--repo", "o/n"], probe=FakeProbe())
        assert exc.value.code == 2


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
        for flag in ('--event "$GITHUB_EVENT_NAME"', '--sha "$GITHUB_SHA"'):
            assert flag in step["run"]

    def test_the_preflight_job_cannot_write(self):
        permissions = _workflow()["jobs"]["preflight"]["permissions"]
        assert set(permissions.values()) == {"read"}

    def test_the_release_job_waits_for_a_positive_decision(self):
        job = _workflow()["jobs"]["release"]
        assert job["needs"] == "preflight"
        assert "needs.preflight.outputs.should_release == 'true'" in job["if"]
