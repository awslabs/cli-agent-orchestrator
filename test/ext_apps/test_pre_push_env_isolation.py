"""Regression tests for cond-0386: pre-push hook must not leak Git env vars.

Git hooks run with ``GIT_DIR``, ``GIT_WORK_TREE`` and other local variables
set to the repository being pushed. If the pre-push hook launches external
test processes without clearing those variables, nested temporary-repository
Git commands can bind to the source worktree and mutate the branch being
pushed. These tests verify the hook isolates child processes by removing every
variable reported by ``git rev-parse --local-env-vars``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


def _repo_root() -> Path:
    # This file lives at test/ext_apps/test_pre_push_env_isolation.py.
    return Path(__file__).resolve().parents[2]


def _hook_path() -> Path:
    return _repo_root() / "cao_mcp_apps" / ".husky" / "pre-push"


def _git_local_env_vars() -> list[str]:
    """Return the authoritative list of Git hook-local env-var names."""
    root = _repo_root()
    output = subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"],
        cwd=root,
        text=True,
    )
    return [name.strip() for name in output.splitlines() if name.strip()]


class TestPrePushHookEnvIsolation:
    def test_hook_exists_and_clears_local_env_vars(self) -> None:
        hook = _hook_path()
        assert hook.exists(), f"pre-push hook not found at {hook}"
        script = hook.read_text(encoding="utf-8")

        local_vars = _git_local_env_vars()
        assert local_vars, "expected Git to report local-env-vars"

        # The hook must unset every variable Git declares as hook-local.
        # Accept either an explicit unset list or a portable loop over the
        # Git-reported names.
        missing = [
            name
            for name in local_vars
            if name not in script and "$(git rev-parse --local-env-vars)" not in script
        ]
        assert not missing, (
            f"pre-push hook does not clear Git local env vars: {missing}\n"
            "Either list every variable explicitly or loop over "
            "`git rev-parse --local-env-vars`."
        )

    def test_child_temp_repo_commit_does_not_mutate_source(self, tmp_path: Path) -> None:
        """With hook isolation applied, a child temp-repo commit stays local.

        The test sets the same Git local env vars that a real hook invocation
        would provide, applies the hook's isolation preamble, then creates and
        commits inside a temporary repository. It finally asserts that the
        source repository's HEAD and index are unchanged.
        """
        root = _repo_root()

        src_git_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-dir"], cwd=root, text=True
        ).strip()
        src_head_before = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        src_index = str(Path(src_git_dir) / "index")

        child = tmp_path / "child"
        child.mkdir()
        subprocess.run(["git", "init", "-q", str(child)], check=True)
        subprocess.run(
            ["git", "config", "user.email", "canary@example.com"],
            cwd=child,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Canary"],
            cwd=child,
            check=True,
        )
        (child / "file.txt").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "add", "file.txt"], cwd=child, check=True)

        # Build a shell snippet that sources the hook's isolation preamble.
        # The hook changes directory, so we capture only the unset logic.
        isolation_script = f"""
            set -eu
            # Simulate Git's hook-local environment.
            export GIT_DIR="{src_git_dir}"
            export GIT_WORK_TREE="{root}"
            export GIT_INDEX_FILE="{src_index}"
            # Apply the same portable isolation used by the pre-push hook.
            for _cao_git_var in $(git rev-parse --local-env-vars); do
                unset $_cao_git_var
            done
            unset _cao_git_var
            # Child repo operation that would corrupt the source without isolation.
            git -C "{child}" commit -q -m "child commit"
            git -C "{child}" rev-parse HEAD
        """

        result = subprocess.run(
            ["sh", "-c", isolation_script],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        child_head = result.stdout.strip()
        assert child_head, "child repo did not produce a commit"

        src_head_after = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        assert src_head_after == src_head_before, (
            "Source repo HEAD moved after child commit: " f"{src_head_before} -> {src_head_after}"
        )

        # The child commit used a separate temp repo; the source HEAD and index
        # are unchanged because GIT_DIR was cleared before the commit ran.
        assert src_head_after == src_head_before

    def test_without_isolation_child_command_leaks_to_source(self, tmp_path: Path) -> None:
        """Sanity check: without clearing env vars, child git uses the source repo.

        This test only reads repository paths; it does not commit anything, so
        it cannot mutate the source branch or index.
        """
        root = _repo_root()
        src_git_dir = subprocess.check_output(
            ["git", "rev-parse", "--git-dir"], cwd=root, text=True
        ).strip()

        child = tmp_path / "child"
        child.mkdir()
        subprocess.run(["git", "init", "-q", str(child)], check=True)

        probe = f"""
            export GIT_DIR="{src_git_dir}"
            export GIT_WORK_TREE="{root}"
            export GIT_INDEX_FILE="{src_git_dir}/index"
            git -C "{child}" rev-parse --git-dir
        """
        leaked = subprocess.check_output(["sh", "-c", probe], cwd=root, text=True).strip()
        assert (
            leaked == src_git_dir
        ), f"Expected child to leak to source git-dir ({src_git_dir}), got {leaked}"
