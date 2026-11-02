"""Which publish jobs run on which trigger, decided by EVALUATING the workflows' ``if:``s.

Review on PR #652, N1: a GitHub Release created by ``release.yml`` under ``GITHUB_TOKEN``
never emits ``on: release``, so the scheduled path could never reach PyPI. The fix is a
``release`` mode for ``publish-to-pypi.yml``'s dispatch, which ``GITHUB_TOKEN`` CAN start.
It must run the release-event chain with every gate in it: the version check, TestPyPI,
the smoke test, and the ``pypi`` environment's required reviewers.

Substring checks over ``if:`` expressions cannot show that, so this file parses the GitHub
expression subset these workflows use and simulates the job graph, including GitHub's
implicit ``success() &&`` on any ``if:`` without a status function. An identifier the
evaluator does not know fails the test rather than reading as empty.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _load(name: str) -> Dict[Any, Any]:
    document = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert True in document, "PyYAML folds `on:` to True; read the triggers from it"
    return document


# --------------------------------------------------------------------------------------
# A GitHub Actions expression evaluator for the subset these workflows use
# --------------------------------------------------------------------------------------

TOKEN = re.compile(
    r"\s*(?:(?P<op>==|!=|&&|\|\||!|\(|\))|'(?P<str>(?:[^']|'')*)'|(?P<id>[A-Za-z_][\w.\-]*))"
)
STATUS_FUNCTIONS = ("always", "success", "failure", "cancelled")


def _tokens(expression: str) -> List[Tuple[str, str]]:
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    out: List[Tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = TOKEN.match(text, pos)
        assert match, f"cannot parse {text[pos:]!r} in {expression!r}"
        pos = match.end()
        if match.group("op"):
            out.append(("op", match.group("op")))
        elif match.group("str") is not None:
            out.append(("str", match.group("str").replace("''", "'")))
        else:
            out.append(("id", match.group("id")))
    return out


class Evaluator:
    def __init__(self, context: Mapping[str, Optional[str]], status: Mapping[str, bool]):
        self.context, self.status = context, status

    def __call__(self, expression: str) -> bool:
        self.tokens, self.i = _tokens(expression), 0
        value = self._or()
        assert self.i == len(self.tokens), f"trailing tokens in {expression!r}"
        return bool(value)

    def _peek(self) -> Optional[Tuple[str, str]]:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def _take(self) -> Tuple[str, str]:
        token = self.tokens[self.i]
        self.i += 1
        return token

    def _or(self) -> Any:
        value = self._and()
        while self._peek() == ("op", "||"):
            self._take()
            right = self._and()
            value = value or right
        return value

    def _and(self) -> Any:
        value = self._unary()
        while self._peek() == ("op", "&&"):
            self._take()
            right = self._unary()
            value = value and right
        return value

    def _unary(self) -> Any:
        if self._peek() == ("op", "!"):
            self._take()
            return not self._unary()
        return self._compare()

    def _compare(self) -> Any:
        left = self._primary()
        if self._peek() in (("op", "=="), ("op", "!=")):
            op = self._take()[1]
            right = self._primary()
            same = _norm(left) == _norm(right)
            return same if op == "==" else not same
        return left

    def _primary(self) -> Any:
        kind, value = self._take()
        if (kind, value) == ("op", "("):
            inner = self._or()
            assert self._take() == ("op", ")")
            return inner
        if kind == "str":
            return value
        assert kind == "id", f"unexpected {value!r}"
        if self._peek() == ("op", "("):
            self._take()
            assert self._take() == ("op", ")")
            assert value in self.status, f"unknown function {value}()"
            return self.status[value]
        assert value in self.context, f"the evaluator does not know {value!r}"
        return self.context[value]


def _norm(value: Any) -> Any:
    # GitHub compares strings case-insensitively, and null equals only null.
    return value.lower() if isinstance(value, str) else value


def _needs(job: Mapping[str, Any]) -> List[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def simulate(
    workflow: Mapping[str, Any],
    context: Mapping[str, Optional[str]],
    outcome: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Each job's result: 'skipped', or its `outcome` (default 'success') when it runs."""
    jobs = workflow["jobs"]
    results: Dict[str, str] = {}
    pending = list(jobs)
    while pending:
        for name in pending:
            if all(n in results for n in _needs(jobs[name])):
                break
        else:  # pragma: no cover - a cycle
            raise AssertionError(f"needs cycle among {pending}")
        pending.remove(name)
        job = jobs[name]
        needs = _needs(job)
        needs_ok = all(results[n] == "success" for n in needs)
        condition = str(job.get("if", "success()"))
        if not any(f"{fn}(" in condition for fn in STATUS_FUNCTIONS):
            condition = f"success() && ({condition})"
        ctx = dict(context)
        for n in needs:
            ctx[f"needs.{n}.result"] = results[n]
        evaluate = Evaluator(ctx, {"always": True, "success": needs_ok, "failure": False})
        if evaluate(condition):
            results[name] = (outcome or {}).get(name, "success")
        else:
            results[name] = "skipped"
    return results


def _ran(results: Mapping[str, str]) -> set:
    return {name for name, result in results.items() if result != "skipped"}


# --------------------------------------------------------------------------------------
# publish-to-pypi.yml
# --------------------------------------------------------------------------------------

PUBLISH = _load("publish-to-pypi.yml")
FULL_CHAIN = {
    "verify-release-version",
    "build-wheels",
    "build-sdist",
    "publish-testpypi",
    "smoke-test",
    "publish-pypi",
    "regenerate-agent-plugin",
}


def publish_context(event: str, environment: Optional[str]) -> Dict[str, Optional[str]]:
    return {
        "github.event_name": event,
        "github.event.inputs.environment": environment,
        "inputs.environment": environment,
    }


SCENARIOS = {
    "release event": (("release", None), FULL_CHAIN),
    "dispatch release": (("workflow_dispatch", "release"), FULL_CHAIN),
    "dispatch testpypi": (
        ("workflow_dispatch", "testpypi"),
        {"verify-release-version", "build-wheels", "build-sdist", "publish-testpypi"},
    ),
    "dispatch pypi": (
        ("workflow_dispatch", "pypi"),
        {
            "verify-release-version",
            "build-wheels",
            "build-sdist",
            "publish-pypi",
            "regenerate-agent-plugin",
        },
    ),
}


class TestWhichJobsRun:
    @pytest.mark.parametrize("scenario", SCENARIOS, ids=list(SCENARIOS))
    def test_each_trigger_runs_exactly_its_chain(self, scenario):
        (event, environment), expected = SCENARIOS[scenario]
        assert _ran(simulate(PUBLISH, publish_context(event, environment))) == expected

    def test_the_release_dispatch_runs_the_same_chain_as_a_release_event(self):
        event = simulate(PUBLISH, publish_context("release", None))
        dispatch = simulate(PUBLISH, publish_context("workflow_dispatch", "release"))
        assert event == dispatch

    @pytest.mark.parametrize("trigger", [("release", None), ("workflow_dispatch", "release")])
    @pytest.mark.parametrize(
        "failing", ["verify-release-version", "build-wheels", "publish-testpypi", "smoke-test"]
    )
    def test_pypi_is_never_reached_past_a_failed_gate(self, trigger, failing):
        results = simulate(PUBLISH, publish_context(*trigger), {failing: "failure"})
        assert results["publish-pypi"] == "skipped", f"{failing} failed and PyPI still ran"
        assert results["regenerate-agent-plugin"] == "skipped"

    def test_the_release_chain_cannot_skip_the_smoke_test_into_pypi(self):
        """The `pypi` escape hatch accepts a SKIPPED smoke test; the release chain must not."""
        results = simulate(
            PUBLISH, publish_context("workflow_dispatch", "release"), {"smoke-test": "skipped"}
        )
        assert results["publish-pypi"] == "skipped"

    def test_an_unknown_mode_publishes_nothing(self):
        ran = _ran(simulate(PUBLISH, publish_context("workflow_dispatch", "prod")))
        assert not ran & {"publish-testpypi", "smoke-test", "publish-pypi"}


class TestTheGatesAreUnchanged:
    """Every gate the release event had is on the new path, configured as before."""

    def test_the_dispatch_offers_the_release_mode(self):
        options = PUBLISH[True]["workflow_dispatch"]["inputs"]["environment"]["options"]
        assert options == ["testpypi", "pypi", "release"]
        assert PUBLISH[True]["release"] == {"types": ["published"]}

    def test_the_environments_and_oidc_are_where_they_were(self):
        jobs = PUBLISH["jobs"]
        assert jobs["publish-testpypi"]["environment"]["name"] == "testpypi"
        assert jobs["publish-pypi"]["environment"]["name"] == "pypi"
        for name, job in jobs.items():
            has_oidc = (job.get("permissions") or {}).get("id-token") == "write"
            assert has_oidc == (name in ("publish-testpypi", "publish-pypi")), name

    def test_pypi_waits_on_the_smoke_test_which_waits_on_testpypi(self):
        jobs = PUBLISH["jobs"]
        assert "smoke-test" in _needs(jobs["publish-pypi"])
        assert _needs(jobs["smoke-test"]) == ["publish-testpypi"]
        assert set(_needs(jobs["publish-testpypi"])) == {"build-wheels", "build-sdist"}

    def _guard_steps(self) -> List[Dict[str, Any]]:
        steps = PUBLISH["jobs"]["verify-release-version"]["steps"]
        return [s for s in steps if "assert_release_version.py" in s.get("run", "")]

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=list(SCENARIOS))
    def test_exactly_one_version_check_runs_on_every_trigger(self, scenario):
        (event, environment), _ = SCENARIOS[scenario]
        evaluate = Evaluator(publish_context(event, environment), {"success": True})
        running = [s for s in self._guard_steps() if evaluate(s["if"])]
        assert len(running) == 1
        both = "--index https://test.pypi.org --index https://pypi.org" in running[0]["run"]
        assert both == (environment in (None, "release")), "the release chain asks both"

    def test_the_release_mode_must_be_dispatched_from_a_tag(self):
        evaluate = Evaluator(publish_context("workflow_dispatch", "release"), {"success": True})
        (step,) = [s for s in self._guard_steps() if evaluate(s["if"])]
        assert '[ "$GITHUB_REF_TYPE" != "tag" ]' in step["run"]
        assert step["run"].index("GITHUB_REF_TYPE") < step["run"].index("assert_release_version")
        assert "${{" not in step["run"]


# --------------------------------------------------------------------------------------
# release.yml starts the chain on a schedule, and only there
# --------------------------------------------------------------------------------------

RELEASE = _load("release.yml")


def release_context(event: str, should_release: str = "true") -> Dict[str, Optional[str]]:
    return {
        "github.repository": "awslabs/cli-agent-orchestrator",
        "github.event_name": event,
        "needs.preflight.outputs.should_release": should_release,
    }


class TestTheScheduleStartsThePublish:
    def test_a_scheduled_release_dispatches_the_publish_chain(self):
        results = simulate(RELEASE, release_context("schedule"))
        assert results["start-publish"] == "success"

    def test_a_manual_release_leaves_the_publish_to_the_maintainer(self):
        """So the maintainer is the publish run's actor, and prevent_self_review holds."""
        results = simulate(RELEASE, release_context("workflow_dispatch"))
        assert results["release"] == "success"
        assert results["start-publish"] == "skipped"

    @pytest.mark.parametrize("failing", ["preflight", "release"])
    def test_nothing_is_dispatched_after_a_failure(self, failing):
        results = simulate(RELEASE, release_context("schedule"), {failing: "failure"})
        assert results["start-publish"] == "skipped"

    def test_nothing_is_dispatched_when_the_preflight_declines(self):
        results = simulate(RELEASE, release_context("schedule", should_release="false"))
        assert results["release"] == "skipped"
        assert results["start-publish"] == "skipped"

    def test_a_fork_does_nothing(self):
        context = release_context("schedule")
        context["github.repository"] = "someone/fork"
        assert _ran(simulate(RELEASE, context)) == set()

    def _job(self) -> Dict[str, Any]:
        return RELEASE["jobs"]["start-publish"]

    def test_the_dispatch_targets_the_new_tag_in_release_mode(self):
        (step,) = self._job()["steps"]
        run = step["run"]
        assert "gh workflow run publish-to-pypi.yml" in run
        assert '--ref "v$VERSION"' in run
        assert "-f environment=release" in run
        assert "${{" not in run
        assert step["env"]["VERSION"] == "${{ needs.release.outputs.version }}"
        assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
        assert RELEASE["jobs"]["release"]["outputs"]["version"] == (
            "${{ steps.bump.outputs.version }}"
        )

    def test_the_dispatching_job_holds_only_actions_write_and_runs_no_action(self):
        job = self._job()
        assert job["permissions"] == {"actions": "write"}
        assert all("uses" not in step for step in job["steps"])

    def test_no_new_secret_is_introduced(self):
        text = (WORKFLOWS / "release.yml").read_text(encoding="utf-8")
        assert set(re.findall(r"secrets\.(\w+)", text)) == {"RELEASE_DEPLOY_KEY", "GITHUB_TOKEN"}

    def test_the_release_job_cannot_start_workflows_itself(self):
        assert "actions" not in RELEASE["jobs"]["release"]["permissions"]


def _iter_ifs(workflow: Mapping[str, Any]) -> Iterable[str]:
    for job in workflow["jobs"].values():
        if "if" in job:
            yield str(job["if"])
        for step in job.get("steps") or []:
            if "if" in step:
                yield str(step["if"])


@pytest.mark.parametrize("name", ["publish-to-pypi.yml", "release.yml"])
def test_the_evaluator_parses_every_condition_in_both_workflows(name):
    """A guard against the evaluator silently not covering a condition."""
    for condition in _iter_ifs(_load(name)):
        assert _tokens(condition), condition
