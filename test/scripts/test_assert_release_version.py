"""Tests for ``scripts/assert_release_version.py`` and the publish-workflow job that runs it.

The defect these pin was verified on three real release runs (v2.5.1, v2.5.2, v2.5.4): a
GitHub Release tag placed on a commit whose ``pyproject.toml`` still carried the previous
version. The wheel matrix built already-published filenames for ~80 minutes and TestPyPI
answered ``400 File already exists``, naming the artifact rather than the cause.

Every test here either exercises the comparison on a synthetic tree, or asserts the guard
FAILS on the exact shape that reached CI. A guard that cannot fail reports success.

The index tests pin WHICH index each workflow path asks. The review on PR #947 showed the
two real indexes diverge in both directions (2.1.0 and 2.2.0 only on TestPyPI, 2.4.1 only
on PyPI), so asking the wrong one both misses the collision this guard exists for and
refuses uploads that would succeed.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS_DIR / "assert_release_version.py"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"
RELEASING_DOC = REPO_ROOT / "docs" / "RELEASING.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("assert_release_version", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the script uses `@dataclass` under
    # `from __future__ import annotations`, and dataclasses resolves those string
    # annotations through sys.modules[cls.__module__], which is None for an
    # unregistered module.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


arv = _load_module()


def _publish_workflow() -> Dict[str, Any]:
    import yaml

    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))


def _guard_job() -> Dict[str, Any]:
    return _publish_workflow()["jobs"]["verify-release-version"]


def _guard_steps() -> list:
    return [s for s in _guard_job()["steps"] if "assert_release_version.py" in s.get("run", "")]


def _index_args(run: str) -> list:
    """Every value passed to ``--index`` in a run block, in order."""
    return re.findall(r"--index\s+(\S+)", run)


def _release_step() -> Dict[str, Any]:
    (step,) = [s for s in _guard_steps() if s.get("if") == "github.event_name == 'release'"]
    return step


def _dispatch_step() -> Dict[str, Any]:
    (step,) = [s for s in _guard_steps() if s.get("if") == "github.event_name != 'release'"]
    return step


def _write_tree(
    root: Path,
    *,
    pyproject: str = "2.5.4",
    lock: str = "2.5.4",
    devcontainer: str | None = "2.5.4",
    name: str = "cli-agent-orchestrator",
) -> Dict[str, Path]:
    """A minimal tree carrying the three version sources the guard reads."""
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "pyproject": root / "pyproject.toml",
        "lock": root / "uv.lock",
        "devcontainer": root / ".devcontainer" / "features" / "cao" / "devcontainer-feature.json",
    }
    paths["pyproject"].write_text(
        f'[project]\nname = "{name}"\nversion = "{pyproject}"\n', encoding="utf-8"
    )
    # A dependency that shares nothing with the project, plus the root entry, plus a
    # decoy whose NAME matches but whose source is a registry: the guard must pick the
    # editable root entry and only that one.
    paths["lock"].write_text(
        "version = 1\n\n"
        '[[package]]\nname = "fastapi"\nversion = "0.1.0"\n'
        'source = { registry = "https://pypi.org/simple" }\n\n'
        f'[[package]]\nname = "{name}"\nversion = "{lock}"\nsource = {{ editable = "." }}\n\n'
        f'[[package]]\nname = "{name}"\nversion = "0.0.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n',
        encoding="utf-8",
    )
    paths["devcontainer"].parent.mkdir(parents=True)
    if devcontainer is not None:
        paths["devcontainer"].write_text(
            json.dumps({"id": "cao", "version": devcontainer}), encoding="utf-8"
        )
    return paths


def _check(paths: Dict[str, Path], tag: str | None, **kwargs):
    return arv.check(
        tag,
        pyproject=paths["pyproject"],
        lock=paths["lock"],
        devcontainer=paths["devcontainer"],
        **kwargs,
    )


def _cli(paths: Dict[str, Path], *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--pyproject",
            str(paths["pyproject"]),
            "--lock",
            str(paths["lock"]),
            "--devcontainer",
            str(paths["devcontainer"]),
            *extra,
        ],
        capture_output=True,
        text=True,
    )


class _FakeIndexes:
    """Answers 200 for (index, version) pairs that 'exist', 404 otherwise; records every URL."""

    def __init__(self, present: Dict[str, set]):
        self.present = present
        self.asked: list[str] = []

    def __call__(self, url: str) -> int:
        self.asked.append(url)
        for index, versions in self.present.items():
            prefix = f"{index}/pypi/cli-agent-orchestrator/"
            if url.startswith(prefix):
                version = url[len(prefix) :].removesuffix("/json")
                return 200 if version in versions else 404
        raise AssertionError(f"unexpected index in {url}")


# The real divergence observed during review (read-only JSON GETs, 2026-10-15).
REAL_DIVERGENCE = {
    arv.TESTPYPI: {"2.1.0", "2.2.0", "2.5.3"},
    arv.PYPI: {"2.4.1", "2.5.3"},
}


class TestTagParsing:
    @pytest.mark.parametrize("tag", ["v2.5.4", " v2.5.4\n", "v10.0.0"])
    def test_release_tags_yield_their_version(self, tag):
        assert arv.version_from_tag(tag) == tag.strip()[1:]

    @pytest.mark.parametrize(
        "tag", ["2.5.4", "main", "v2.5", "v2.5.4-rc1", "release/v2.5.4", "", "v2.5.4.1"]
    )
    def test_anything_else_is_refused(self, tag):
        """GITHUB_REF_NAME is the only version source on the release path; a non-tag ref
        must not be allowed to publish by accident."""
        with pytest.raises(ValueError):
            arv.version_from_tag(tag)


class TestTreeComparison:
    def test_a_consistent_tree_passes(self, tmp_path):
        assert _check(_write_tree(tmp_path), "v2.5.4").ok

    def test_the_v2_5_4_shape_fails_and_names_both_versions(self, tmp_path):
        """The real failure: tag v2.5.4 on a tree still at 2.5.3 everywhere."""
        paths = _write_tree(tmp_path, pyproject="2.5.3", lock="2.5.3", devcontainer="2.5.3")
        report = _check(paths, "v2.5.4")
        assert not report.ok, "the guard passed the exact shape that failed three releases"
        joined = "\n".join(report.problems)
        assert "v2.5.4" in joined and "2.5.3" in joined
        assert "pyproject.toml" in joined
        assert report.remedy == arv.REMEDY_MISMATCH

    def test_a_lock_that_lags_the_bump_fails(self, tmp_path):
        """UV_LOCKED=1 would fail this later and less clearly; it must fail here."""
        report = _check(_write_tree(tmp_path, lock="2.5.3"), "v2.5.4")
        assert len(report.problems) == 1
        assert "uv.lock" in report.problems[0] and "2.5.3" in report.problems[0]

    def test_a_devcontainer_that_lags_the_bump_fails(self, tmp_path):
        report = _check(_write_tree(tmp_path, devcontainer="2.5.3"), "v2.5.4")
        assert len(report.problems) == 1 and "devcontainer-feature.json" in report.problems[0]

    def test_a_missing_devcontainer_feature_fails_rather_than_passing_silently(self, tmp_path):
        report = _check(_write_tree(tmp_path, devcontainer=None), "v2.5.4")
        assert any("missing" in p for p in report.problems)

    def test_lock_version_picks_the_editable_root_not_a_same_named_registry_entry(self, tmp_path):
        paths = _write_tree(tmp_path, lock="2.5.4")
        assert arv.lock_version("cli-agent-orchestrator", paths["lock"]) == "2.5.4"
        assert arv.lock_version("not-in-lock", paths["lock"]) is None

    def test_no_tag_checks_the_tree_against_itself(self, tmp_path):
        """The workflow_dispatch path: nothing to compare the tag to, but the three
        sources must still agree, and the remedy must not send anyone to delete a tag."""
        assert _check(_write_tree(tmp_path / "ok"), None).ok
        report = _check(_write_tree(tmp_path / "lag", lock="2.5.3"), None)
        assert len(report.problems) == 1 and "uv.lock" in report.problems[0]
        assert report.remedy == arv.REMEDY_TREE_ONLY
        assert "delete this tag" not in report.remedy
        assert "GitHub Release" not in report.remedy

    def test_a_malformed_tag_is_the_only_problem_and_gets_its_own_remedy(self, tmp_path):
        report = _check(_write_tree(tmp_path), "main")
        assert len(report.problems) == 1 and "not a release tag" in report.problems[0]
        assert report.remedy == arv.REMEDY_BAD_TAG
        assert "vMAJOR.MINOR.PATCH" in report.remedy
        assert "bump" not in report.remedy.lower(), "a bad ref is not a version-bump problem"


class TestWhichIndexIsAsked:
    """Pins the URL(s) each path queries. A future index or path change must fail here."""

    def test_the_url_shape_is_the_json_api_of_the_given_index(self):
        assert (
            arv.version_url(arv.TESTPYPI, "cli-agent-orchestrator", "2.5.4")
            == "https://test.pypi.org/pypi/cli-agent-orchestrator/2.5.4/json"
        )
        assert (
            arv.version_url("https://pypi.org/", "cli-agent-orchestrator", "2.5.4")
            == "https://pypi.org/pypi/cli-agent-orchestrator/2.5.4/json"
        )

    def test_no_index_means_no_network_call(self, tmp_path):
        def must_not_run(url):
            raise AssertionError("an index was queried with no --index given")

        assert _check(_write_tree(tmp_path), "v2.5.4", fetch_status=must_not_run).ok

    def test_each_given_index_is_asked_once_in_order(self, tmp_path):
        fake = _FakeIndexes({arv.TESTPYPI: set(), arv.PYPI: set()})
        report = _check(
            _write_tree(tmp_path),
            "v2.5.4",
            indexes=[arv.TESTPYPI, arv.PYPI],
            fetch_status=fake,
        )
        assert report.ok
        assert fake.asked == [
            "https://test.pypi.org/pypi/cli-agent-orchestrator/2.5.4/json",
            "https://pypi.org/pypi/cli-agent-orchestrator/2.5.4/json",
        ]

    def test_scenario_a_testpypi_only_version_is_refused_on_the_release_path(self, tmp_path):
        """Review scenario A: a release reached TestPyPI, then failed; the maintainer
        re-created the tag. PyPI says 404, TestPyPI says 200. Asking only PyPI passed this
        and cost 80 minutes; asking both refuses it in seconds."""
        fake = _FakeIndexes(REAL_DIVERGENCE)
        report = _check(
            _write_tree(tmp_path, pyproject="2.2.0", lock="2.2.0", devcontainer="2.2.0"),
            "v2.2.0",
            indexes=[arv.TESTPYPI, arv.PYPI],
            fetch_status=fake,
        )
        assert not report.ok
        assert len(report.problems) == 1 and arv.TESTPYPI in report.problems[0]
        assert report.remedy == arv.REMEDY_PUBLISHED_TAGGED

    def test_scenario_b_pypi_only_version_is_allowed_for_a_testpypi_dispatch(self, tmp_path):
        """Review scenario B: 2.4.1 is on PyPI but not TestPyPI. A testpypi dispatch must
        ask TestPyPI only, and pass."""
        fake = _FakeIndexes(REAL_DIVERGENCE)
        report = _check(
            _write_tree(tmp_path, pyproject="2.4.1", lock="2.4.1", devcontainer="2.4.1"),
            None,
            indexes=[arv.TESTPYPI],
            fetch_status=fake,
        )
        assert report.ok
        assert fake.asked == ["https://test.pypi.org/pypi/cli-agent-orchestrator/2.4.1/json"]

    def test_a_pypi_dispatch_asks_pypi_and_refuses_what_pypi_has(self, tmp_path):
        fake = _FakeIndexes(REAL_DIVERGENCE)
        report = _check(
            _write_tree(tmp_path, pyproject="2.4.1", lock="2.4.1", devcontainer="2.4.1"),
            None,
            indexes=[arv.PYPI],
            fetch_status=fake,
        )
        assert not report.ok and arv.PYPI in report.problems[0]
        assert report.remedy == arv.REMEDY_PUBLISHED_UNTAGGED
        assert "delete this tag" not in report.remedy

    def test_a_version_on_both_indexes_is_reported_once_per_index(self, tmp_path):
        fake = _FakeIndexes(REAL_DIVERGENCE)
        report = _check(
            _write_tree(tmp_path, pyproject="2.5.3", lock="2.5.3", devcontainer="2.5.3"),
            "v2.5.3",
            indexes=[arv.TESTPYPI, arv.PYPI],
            fetch_status=fake,
        )
        assert len(report.problems) == 2

    def test_a_network_failure_warns_but_does_not_block_the_release(self, tmp_path, capsys):
        def boom(url):
            raise OSError("no route to host")

        report = _check(
            _write_tree(tmp_path), "v2.5.4", indexes=[arv.TESTPYPI, arv.PYPI], fetch_status=boom
        )
        assert report.ok
        err = capsys.readouterr().err
        # Exact list, not substring checks: which index was named, in which order.
        assert re.findall(r"WARNING: could not ask (\S+) about", err) == [arv.TESTPYPI, arv.PYPI]

    def test_an_unexpected_status_is_unknown_not_published(self, tmp_path, capsys):
        report = _check(
            _write_tree(tmp_path), "v2.5.4", indexes=[arv.PYPI], fetch_status=lambda url: 503
        )
        assert report.ok and "503" in capsys.readouterr().err

    def test_indexes_are_not_asked_when_the_tree_is_already_wrong(self, tmp_path):
        """One clear report, not a network call on top of a known-bad tree."""

        def must_not_run(url):
            raise AssertionError("an index was queried despite tree problems")

        report = _check(
            _write_tree(tmp_path, lock="2.5.3"),
            "v2.5.4",
            indexes=[arv.PYPI],
            fetch_status=must_not_run,
        )
        assert not report.ok


class TestCommandLine:
    def test_exit_1_with_the_mismatch_remedy_on_the_v2_5_4_shape(self, tmp_path):
        paths = _write_tree(tmp_path, pyproject="2.5.3", lock="2.5.3", devcontainer="2.5.3")
        result = _cli(paths, "--tag", "v2.5.4")
        assert result.returncode == 1
        assert "RELEASE VERSION CHECK FAILED" in result.stderr
        assert "re-create both on that merge commit" in result.stderr
        assert "PR #899" in result.stderr

    def test_exit_0_on_a_consistent_tree(self, tmp_path):
        result = _cli(_write_tree(tmp_path), "--tag", "v2.5.4")
        assert result.returncode == 0, result.stderr
        assert "matches version 2.5.4" in result.stdout

    def test_the_dispatch_shape_never_tells_the_operator_to_delete_a_tag(self, tmp_path):
        result = _cli(_write_tree(tmp_path, lock="2.5.3"))
        assert result.returncode == 1
        assert "delete this tag" not in result.stderr
        assert "dispatch again" in result.stderr

    def test_index_is_repeatable_and_echoed_on_success(self, tmp_path):
        """Real network is not used: an unroutable host makes the lookup a WARNING."""
        result = _cli(
            _write_tree(tmp_path),
            "--tag",
            "v2.5.4",
            "--index",
            "http://127.0.0.1:9",
            "--index",
            "http://127.0.0.1:9/",
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr.count("WARNING") == 2
        assert "not on http://127.0.0.1:9, http://127.0.0.1:9/" in result.stdout


class TestTheRealTree:
    def test_pyproject_lock_and_devcontainer_agree(self):
        """The bump discipline this guard enforces at release time, enforced on every PR too.

        A `chore: release` PR that touches one file but not the others fails here, before
        anyone tags it.
        """
        report = arv.check(None)
        assert report.ok, "pyproject.toml, uv.lock and the devcontainer feature disagree"


class TestPublishWorkflowRunsTheGuardFirst:
    def test_the_job_exists_and_both_build_jobs_wait_on_it(self):
        """The whole value is failing BEFORE ~80 minutes of wheel builds."""
        jobs = _publish_workflow()["jobs"]
        assert "verify-release-version" in jobs
        for job in ("build-wheels", "build-sdist"):
            needs = jobs[job]["needs"]
            needs = [needs] if isinstance(needs, str) else needs
            assert "verify-release-version" in needs, f"{job} does not wait on the guard"

    def test_there_is_one_step_per_event_and_neither_interpolates_into_run(self):
        """`GITHUB_REF_NAME` and the target index come from the environment, never
        `${{ }}` inside `run:`, so nothing from the event can be executed as shell."""
        assert len(_guard_steps()) == 2, "one step for release events, one for manual dispatch"
        for step in _guard_steps():
            assert "${{" not in step["run"]

    def test_the_release_step_compares_the_tag_and_asks_both_indexes(self):
        """A release uploads to TestPyPI first and PyPI second, so a version on EITHER is a
        guaranteed 400 later. Pinned as literal URLs: this is the review's P2."""
        run = _release_step()["run"]
        assert '--tag "$GITHUB_REF_NAME"' in run
        assert _index_args(run) == [arv.TESTPYPI, arv.PYPI]

    def test_the_dispatch_step_asks_only_the_index_it_targets(self):
        """`inputs.environment` selects the index in `env:`, and `run:` reads it from the
        environment. A testpypi dispatch must never be refused on pypi.org state, and a
        pypi dispatch must never be passed on test.pypi.org state."""
        step = _dispatch_step()
        # The whole expression, pinned exactly: the choice input picks the index.
        assert step["env"]["TARGET_INDEX"] == (
            "${{ inputs.environment == 'testpypi' && 'https://test.pypi.org' "
            "|| 'https://pypi.org' }}"
        )
        # Every --index in the run block is the environment variable; none is a literal.
        assert _index_args(step["run"]) == ['"$TARGET_INDEX"', '"$TARGET_INDEX"']

    def test_the_dispatch_step_compares_the_tag_when_dispatched_from_one(self):
        """The pypi environment is restricted to v* tags, so the PyPI escape hatch always
        runs on a tag ref; it must get the same tag/tree comparison a release does."""
        run = _dispatch_step()["run"]
        assert '"$GITHUB_REF_TYPE" = "tag"' in run
        assert '--tag "$GITHUB_REF_NAME"' in run

    def test_the_guard_job_is_bounded_and_pinned(self):
        job = _guard_job()
        assert job["timeout-minutes"] <= 10
        for step in job["steps"]:
            if "uses" in step:
                assert len(step["uses"].rsplit("@", 1)[1]) == 40, step["uses"]


class TestReleasingDocDescribesTheGate:
    def test_the_pipeline_overview_shows_the_guard(self):
        text = RELEASING_DOC.read_text(encoding="utf-8")
        assert "verify-release-version" in text

    def test_the_troubleshooting_entry_no_longer_says_to_re_run_the_same_tag(self):
        text = RELEASING_DOC.read_text(encoding="utf-8")
        assert "fix `pyproject.toml` manually and re-run" not in text
        assert "re-create" in text and "uv.lock" in text

    def test_the_testpypi_dispatch_section_names_its_precondition(self):
        text = RELEASING_DOC.read_text(encoding="utf-8")
        assert "not already on TestPyPI" in text
