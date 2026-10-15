"""Tests for ``scripts/assert_release_version.py`` and the publish-workflow job that runs it.

The defect these pin was verified on three real release runs (v2.5.1, v2.5.2, v2.5.4): a
GitHub Release tag placed on a commit whose ``pyproject.toml`` still carried the previous
version. The wheel matrix built already-published filenames for ~80 minutes and TestPyPI
answered ``400 File already exists``, naming the artifact rather than the cause.

Every test here either exercises the comparison on a synthetic tree, or asserts the guard
FAILS on the exact shape that reached CI. A guard that cannot fail reports success.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
SCRIPT = SCRIPTS_DIR / "assert_release_version.py"
PUBLISH_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"


def _load_module():
    spec = importlib.util.spec_from_file_location("assert_release_version", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


arv = _load_module()


def _publish_workflow() -> Dict[str, Any]:
    import yaml

    return yaml.safe_load(PUBLISH_WORKFLOW.read_text(encoding="utf-8"))


def _write_tree(
    root: Path,
    *,
    pyproject: str = "2.5.4",
    lock: str = "2.5.4",
    devcontainer: str | None = "2.5.4",
    name: str = "cli-agent-orchestrator",
) -> Dict[str, Path]:
    """A minimal tree carrying the three version sources the guard reads."""
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


def _check(paths: Dict[str, Path], tag: str | None, **kwargs) -> list:
    return arv.check(
        tag,
        pyproject=paths["pyproject"],
        lock=paths["lock"],
        devcontainer=paths["devcontainer"],
        **kwargs,
    )


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
        assert _check(_write_tree(tmp_path), "v2.5.4") == []

    def test_the_v2_5_4_shape_fails_and_names_both_versions(self, tmp_path):
        """The real failure: tag v2.5.4 on a tree still at 2.5.3 everywhere."""
        paths = _write_tree(tmp_path, pyproject="2.5.3", lock="2.5.3", devcontainer="2.5.3")
        problems = _check(paths, "v2.5.4")
        assert problems, "the guard passed the exact shape that failed three releases"
        joined = "\n".join(problems)
        assert "v2.5.4" in joined and "2.5.3" in joined
        assert "pyproject.toml" in joined

    def test_a_lock_that_lags_the_bump_fails(self, tmp_path):
        """UV_LOCKED=1 would fail this later and less clearly; it must fail here."""
        paths = _write_tree(tmp_path, lock="2.5.3")
        problems = _check(paths, "v2.5.4")
        assert len(problems) == 1 and "uv.lock" in problems[0] and "2.5.3" in problems[0]

    def test_a_devcontainer_that_lags_the_bump_fails(self, tmp_path):
        paths = _write_tree(tmp_path, devcontainer="2.5.3")
        problems = _check(paths, "v2.5.4")
        assert len(problems) == 1 and "devcontainer-feature.json" in problems[0]

    def test_a_missing_devcontainer_feature_fails_rather_than_passing_silently(self, tmp_path):
        paths = _write_tree(tmp_path, devcontainer=None)
        assert any("missing" in p for p in _check(paths, "v2.5.4"))

    def test_lock_version_picks_the_editable_root_not_a_same_named_registry_entry(self, tmp_path):
        paths = _write_tree(tmp_path, lock="2.5.4")
        assert arv.lock_version("cli-agent-orchestrator", paths["lock"]) == "2.5.4"
        assert arv.lock_version("not-in-lock", paths["lock"]) is None

    def test_no_tag_checks_the_tree_against_itself(self, tmp_path):
        """The workflow_dispatch path: nothing to compare the tag to, but the three
        sources must still agree."""
        assert _check(_write_tree(tmp_path), None) == []
        lag = tmp_path / "lag"
        lag.mkdir()
        problems = _check(_write_tree(lag, lock="2.5.3"), None)
        assert len(problems) == 1 and "uv.lock" in problems[0]

    def test_a_malformed_tag_is_the_only_problem_reported(self, tmp_path):
        problems = _check(_write_tree(tmp_path), "main")
        assert len(problems) == 1 and "not a release tag" in problems[0]


class TestAlreadyPublished:
    def test_a_version_pypi_already_serves_is_refused(self, tmp_path):
        problems = _check(
            _write_tree(tmp_path), "v2.5.4", refuse_published=True, fetch_status=lambda url: 200
        )
        assert len(problems) == 1 and "already on PyPI" in problems[0]

    def test_a_404_means_unpublished_and_passes(self, tmp_path):
        assert (
            _check(
                _write_tree(tmp_path),
                "v2.5.4",
                refuse_published=True,
                fetch_status=lambda url: 404,
            )
            == []
        )

    def test_a_network_failure_warns_but_does_not_block_the_release(self, tmp_path, capsys):
        def boom(url):
            raise OSError("no route to host")

        assert (
            _check(_write_tree(tmp_path), "v2.5.4", refuse_published=True, fetch_status=boom) == []
        )
        assert "WARNING" in capsys.readouterr().err

    def test_the_query_names_the_project_and_version(self):
        seen = {}

        def record(url):
            seen["url"] = url
            return 404

        assert arv.pypi_has_version("cli-agent-orchestrator", "2.5.4", record) is False
        assert seen["url"] == "https://pypi.org/pypi/cli-agent-orchestrator/2.5.4/json"

    def test_pypi_is_not_asked_when_the_tree_is_already_wrong(self, tmp_path):
        """One clear report, not a network call on top of a known-bad tree."""

        def must_not_run(url):
            raise AssertionError("PyPI was queried despite tree problems")

        paths = _write_tree(tmp_path, lock="2.5.3")
        assert _check(paths, "v2.5.4", refuse_published=True, fetch_status=must_not_run)


class TestCommandLine:
    def test_exit_1_with_the_fix_on_the_v2_5_4_shape(self, tmp_path):
        paths = _write_tree(tmp_path, pyproject="2.5.3", lock="2.5.3", devcontainer="2.5.3")
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--tag",
                "v2.5.4",
                "--pyproject",
                str(paths["pyproject"]),
                "--lock",
                str(paths["lock"]),
                "--devcontainer",
                str(paths["devcontainer"]),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "RELEASE VERSION CHECK FAILED" in result.stderr
        assert "re-create both on that merge commit" in result.stderr
        assert "PR #899" in result.stderr

    def test_exit_0_on_a_consistent_tree(self, tmp_path):
        paths = _write_tree(tmp_path)
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--tag",
                "v2.5.4",
                "--pyproject",
                str(paths["pyproject"]),
                "--lock",
                str(paths["lock"]),
                "--devcontainer",
                str(paths["devcontainer"]),
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "matches version 2.5.4" in result.stdout


class TestTheRealTree:
    def test_pyproject_lock_and_devcontainer_agree(self):
        """The bump discipline this guard enforces at release time, enforced on every PR too.

        A `chore: release` PR that touches one file but not the others fails here, before
        anyone tags it.
        """
        assert (
            arv.check(None) == []
        ), "pyproject.toml, uv.lock and the devcontainer feature disagree"


class TestPublishWorkflowRunsTheGuardFirst:
    def test_the_job_exists_and_both_build_jobs_wait_on_it(self):
        """The whole value is failing BEFORE ~80 minutes of wheel builds."""
        jobs = _publish_workflow()["jobs"]
        assert "verify-release-version" in jobs
        for job in ("build-wheels", "build-sdist"):
            needs = jobs[job]["needs"]
            needs = [needs] if isinstance(needs, str) else needs
            assert "verify-release-version" in needs, f"{job} does not wait on the guard"

    def test_the_release_step_passes_the_ref_through_the_environment(self):
        """`GITHUB_REF_NAME` from the environment, never `${{ github.ref_name }}` in a
        `run:` block, so the ref can never be executed as shell."""
        steps = _publish_workflow()["jobs"]["verify-release-version"]["steps"]
        runs = [s for s in steps if "assert_release_version.py" in s.get("run", "")]
        assert len(runs) == 2, "one step for release events, one for manual dispatch"
        release = [s for s in runs if s.get("if") == "github.event_name == 'release'"]
        assert len(release) == 1
        assert '--tag "$GITHUB_REF_NAME"' in release[0]["run"]
        for step in runs:
            assert "${{" not in step["run"]
            assert "--refuse-published" in step["run"]

    def test_the_guard_job_is_bounded_and_pinned(self):
        job = _publish_workflow()["jobs"]["verify-release-version"]
        assert job["timeout-minutes"] <= 10
        for step in job["steps"]:
            if "uses" in step:
                assert len(step["uses"].rsplit("@", 1)[1]) == 40, step["uses"]
