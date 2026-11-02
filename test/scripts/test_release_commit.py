"""Tests for ``scripts/release_commit.py``: the release commit and its tag land together.

Review on PR #652, N2: ``git push && git push --tags`` could land the bump on ``main`` and
lose the tag, and the next schedule then released the version after the one that never got
a tag. These tests push to a real bare repository whose ``update`` hook rejects one ref, the
way a network failure or a server-side rule would, and check what actually landed.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "release_commit.py"
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"


def _load():
    spec = importlib.util.spec_from_file_location("release_commit", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rc = _load()


def _env(home: Path) -> Dict[str, str]:
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


class World:
    """A bare 'origin' with a hook, and a clone that has just been bumped."""

    def __init__(self, tmp: Path, monkeypatch) -> None:
        self.env = _env(tmp)
        for key, value in self.env.items():
            monkeypatch.setenv(key, value)
        self.origin = tmp / "origin.git"
        self.clone = tmp / "clone"
        self.run("git", "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.run("git", "clone", "-q", str(self.origin), str(self.clone))
        self.git("checkout", "-q", "-b", "main")
        for rel in rc.RELEASE_FILES:
            path = self.clone / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("1.0.0\n", encoding="utf-8")
        (self.clone / "README.md").write_text("readme\n", encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: init")
        self.git("push", "-q", "origin", "HEAD:refs/heads/main")
        self.base = self.git("rev-parse", "HEAD")

    def run(self, *argv: str) -> str:
        return subprocess.run(
            argv, env=self.env, capture_output=True, text=True, check=True
        ).stdout.strip()

    def git(self, *args: str) -> str:
        return self.run("git", "-C", str(self.clone), *args)

    def remote(self, ref: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(self.origin), "rev-parse", "-q", "--verify", ref],
            env=self.env,
            capture_output=True,
            text=True,
        )
        return out.stdout.strip()

    def bump(self, to: str = "1.0.1") -> None:
        for rel in (
            "pyproject.toml",
            "uv.lock",
            ".devcontainer/features/cao/devcontainer-feature.json",
        ):
            (self.clone / rel).write_text(f"{to}\n", encoding="utf-8")

    def reject_on_server(self, ref_prefix: str) -> None:
        """A per-ref `update` hook: without --atomic, the OTHER ref would still land."""
        hook = self.origin / "hooks" / "update"
        hook.write_text(
            f'#!/bin/sh\ncase "$1" in {ref_prefix}*) echo "rejected $1" >&2; exit 1;; esac\n',
            encoding="utf-8",
        )
        hook.chmod(0o755)


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> World:
    return World(tmp_path, monkeypatch)


class TestTheAtomicPush:
    def test_the_commit_and_the_tag_land_together(self, world):
        world.bump()
        assert rc.main(["--version", "1.0.1", "--repo-root", str(world.clone)]) == 0
        head = world.git("rev-parse", "HEAD")
        assert world.remote("refs/heads/main") == head
        assert world.remote("refs/tags/v1.0.1^{commit}") == head
        assert (
            world.git("log", "-1", "--format=%s %an") == "chore: release v1.0.1 github-actions[bot]"
        )

    def test_a_rejected_tag_leaves_main_untouched(self, world, capsys):
        world.reject_on_server("refs/tags/")
        world.bump()
        assert rc.main(["--version", "1.0.1", "--repo-root", str(world.clone)]) == 1
        assert world.remote("refs/heads/main") == world.base, "main moved without its tag"
        assert world.remote("refs/tags/v1.0.1") == ""
        assert "::error::" in capsys.readouterr().out

    def test_a_rejected_branch_leaves_no_tag(self, world):
        world.reject_on_server("refs/heads/")
        world.bump()
        assert rc.main(["--version", "1.0.1", "--repo-root", str(world.clone)]) == 1
        assert world.remote("refs/tags/v1.0.1") == ""
        assert world.remote("refs/heads/main") == world.base

    def test_the_old_two_step_push_is_what_half_lands(self, world):
        """Control: the shape this replaces really does leave main bumped and untagged."""
        world.reject_on_server("refs/tags/")
        world.bump()
        world.git("commit", "-q", "-am", "chore: release v1.0.1")
        world.git("tag", "v1.0.1")
        world.git("push", "-q", "origin", "HEAD:refs/heads/main")
        with pytest.raises(subprocess.CalledProcessError):
            world.git("push", "-q", "origin", "--tags")
        assert world.remote("refs/heads/main") == world.git("rev-parse", "HEAD")
        assert world.remote("refs/tags/v1.0.1") == ""

    def test_main_moving_underneath_rejects_both(self, world, tmp_path):
        other = tmp_path / "other"
        world.run("git", "clone", "-q", str(world.origin), str(other))
        (other / "README.md").write_text("someone else\n", encoding="utf-8")
        world.run("git", "-C", str(other), "commit", "-q", "-am", "fix: landed meanwhile")
        world.run("git", "-C", str(other), "push", "-q", "origin", "HEAD:refs/heads/main")
        moved = world.remote("refs/heads/main")
        world.bump()
        assert rc.main(["--version", "1.0.1", "--repo-root", str(world.clone)]) == 1
        assert world.remote("refs/heads/main") == moved, "never forced"
        assert world.remote("refs/tags/v1.0.1") == ""

    def test_the_push_names_both_refs_and_never_forces(self):
        argv = rc.push_command("origin", "2.5.5")
        assert argv == [
            "push",
            "--atomic",
            "origin",
            "HEAD:refs/heads/main",
            "refs/tags/v2.5.5:refs/tags/v2.5.5",
        ]
        assert not any(a.startswith("+") or a in ("-f", "--force") for a in argv)


class TestWhatTheReleaseCommitMayCarry:
    def test_a_stray_change_is_refused_before_anything_is_committed(self, world, capsys):
        world.bump()
        (world.clone / "README.md").write_text("tampered\n", encoding="utf-8")
        assert rc.main(["--version", "1.0.1", "--repo-root", str(world.clone)]) == 1
        assert world.git("rev-parse", "HEAD") == world.base
        assert world.git("tag", "--list") == ""
        assert "also changed: README.md" in capsys.readouterr().out

    def test_nothing_staged_is_refused(self, world, capsys):
        assert rc.main(["--version", "1.0.1", "--repo-root", str(world.clone)]) == 1
        assert "nothing to commit" in capsys.readouterr().out
        assert world.remote("refs/tags/v1.0.1") == ""

    @pytest.mark.parametrize("version", ["v1.0.1", "1.0", "1.0.1rc1", "1.0.1; rm -rf ~"])
    def test_a_malformed_version_is_refused(self, world, version):
        world.bump()
        assert rc.main(["--version", version, "--repo-root", str(world.clone)]) == 1
        assert world.git("rev-parse", "HEAD") == world.base


def test_release_yml_commits_through_the_script():
    import yaml

    steps = yaml.safe_load(RELEASE_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["release"]["steps"]
    runs = "\n".join(str(s.get("run", "")) for s in steps)
    assert 'release_commit.py --version "$VERSION"' in runs
    assert "git push" not in runs, "every push goes through the atomic script"
    (step,) = [s for s in steps if "release_commit.py" in s.get("run", "")]
    assert step["env"]["VERSION"] == "${{ steps.bump.outputs.version }}"
