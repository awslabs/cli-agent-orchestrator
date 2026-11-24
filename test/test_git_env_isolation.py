"""A push from a linked worktree exports GIT_DIR into the pre-push hook.

Run with that environment, the suite's tests that create throwaway repositories
(`git init`, `git config`, `git commit` in a temp directory) used to write into the
repository being pushed: a "Test" commit, ``core.bare=true`` and a repo-level
``user.name``. Two layers stop it: the hook unsets the repository-binding variables,
and ``test/conftest.py`` drops them for the session.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "cao_mcp_apps" / ".husky" / "pre-push"
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(GIT is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        [GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
    ).stdout


# Variables the CI runner (or xdist, coverage, the must-run gate) puts in the test process's
# environment and that must not steer a nested pytest session: `CAO_MUST_RUN=unit` made the
# nested run fail with "no must_run test was collected" although its 4 tests passed.
_NESTED_RUN_ENV_PREFIXES = ("GIT_", "PYTEST_", "COV_CORE", "COVERAGE_", "CAO_MUST_RUN")


def _child_env(**extra: str) -> dict:
    """``os.environ`` without anything that steers a nested pytest run, plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(_NESTED_RUN_ENV_PREFIXES)}
    env.update(extra)
    return env


def _hook_style_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A repository with one commit and a linked worktree, as a hook run sees them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Owner")
    _git(repo, "config", "user.email", "owner@example.invalid")
    (repo / "f").write_text("x\n")
    _git(repo, "add", "f")
    _git(repo, "commit", "-q", "-m", "owner commit")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(worktree), "-b", "feature")
    gitdir = Path(_git(worktree, "rev-parse", "--absolute-git-dir").strip())
    return repo, worktree, gitdir


def _snapshot(repo: Path) -> dict:
    return {
        "config": (repo / ".git" / "config").read_text(),
        "refs": _git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
        "log": _git(repo, "log", "--all", "--format=%an %s"),
        "worktrees": _git(repo, "worktree", "list", "--porcelain"),
    }


def test_the_suite_does_not_write_into_the_repository_a_hook_points_at(tmp_path):
    repo, worktree, gitdir = _hook_style_repo(tmp_path)
    before = _snapshot(repo)

    env = _child_env(GIT_DIR=str(gitdir))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(REPO_ROOT / "test" / "services" / "test_worktree_service.py"),
            "-k",
            "TestFindRepoRoot",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:xdist",
        ],
        cwd=worktree,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-2000:]

    assert _snapshot(repo) == before


@pytest.mark.parametrize(
    "git_on_path", [True, False], ids=["git-list-and-fixed-names", "fixed-names-only"]
)
def test_the_pre_push_hook_unsets_the_repository_binding_variables(tmp_path, git_on_path):
    seen = tmp_path / "seen"
    npm_calls = tmp_path / "npm-calls"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    # Stand-ins for every tool the hook runs, so the test never starts a real runner or
    # npm step in the checkout: `uv` records what it inherits; `npm` and `node` only record.
    (stubs / "uv").write_text(f'#!/bin/sh\nenv > "{seen}"\n')
    (stubs / "npm").write_text(f'#!/bin/sh\necho "$@" >> "{npm_calls}"\n')
    (stubs / "node").write_text("#!/bin/sh\nexit 0\n")
    for name in ("uv", "npm", "node"):
        (stubs / name).chmod(0o755)
    fixed = {
        "GIT_DIR": "/nonexistent/gitdir",
        "GIT_WORK_TREE": "/nonexistent/tree",
        "GIT_INDEX_FILE": "/nonexistent/index",
        "GIT_COMMON_DIR": "/nonexistent/common",
        "GIT_OBJECT_DIRECTORY": "/nonexistent/objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/nonexistent/alt",
        "GIT_PREFIX": "sub/",
        "GIT_CONFIG": "/nonexistent/config",
        "GIT_CONFIG_PARAMETERS": "'x.y=z'",
        "GIT_CONFIG_COUNT": "1",
    }
    # Names only git's own `--local-env-vars` list has: the hook's fixed list does not.
    git_only = {"GIT_SHALLOW_FILE": "/nonexistent/shallow", "GIT_GRAFT_FILE": "/nonexistent/graft"}
    if git_on_path:
        path = f"{stubs}{os.pathsep}/usr/bin{os.pathsep}/bin"
        expected_gone = {**fixed, **git_only}
    else:
        # No git on PATH: only the tools the hook and the stubs need, so the hook's fixed
        # list is all that is left (it also covers a git without `--local-env-vars`).
        tools = tmp_path / "tools"
        tools.mkdir()
        for tool in ("dirname", "env"):
            (tools / tool).symlink_to(shutil.which(tool))
        path = f"{stubs}{os.pathsep}{tools}"
        expected_gone = dict(fixed)
    env = {"PATH": path, "HOME": str(tmp_path), **expected_gone, **git_only}
    result = subprocess.run(
        ["/bin/sh", str(HOOK)], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stdout[-1500:] + result.stderr[-1500:]
    assert "[pre-push] Python unit tests" in result.stdout
    # The two npm steps ran through the stub, not the host's npm.
    assert npm_calls.read_text().split("\n")[:2] == ["run scan:jit", "run check:size"]
    inherited = seen.read_text()
    for name in expected_gone:
        assert f"{name}=" not in inherited, f"{name} reached the test runner"


_ALL_NAMES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_COMMON_DIR",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_CONFIG",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
)


def _names_left_after_importing_the_conftest(extra_names=(), path=None) -> str:
    names = _ALL_NAMES + tuple(extra_names)
    code = (
        "import importlib.util, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('cao_conftest_probe', {str(REPO_ROOT / 'test' / 'conftest.py')!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        f"left = [n for n in {names!r} if n in os.environ]\n"
        "print('LEFT:' + ','.join(left))\n"
    )
    env = _child_env(**{name: "/nonexistent" for name in names})
    if path is not None:
        env["PATH"] = path
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert "LEFT:" in result.stdout, result.stdout[-1500:] + result.stderr[-1500:]
    return result.stdout.rsplit("LEFT:", 1)[1].strip()


def test_conftest_drops_every_repository_binding_variable():
    """Import ``test/conftest.py`` with every name set: none may survive.

    Guards against a conftest that strips only ``GIT_DIR`` or forgets ``GIT_CONFIG*``.
    """
    left = _names_left_after_importing_the_conftest()
    assert left == "", f"the conftest left these set: {left}"


def test_conftest_drops_what_git_itself_lists_as_repository_local(tmp_path):
    """The conftest asks ``git rev-parse --local-env-vars`` as the hook does, so a name
    that only git's own list has (here ``GIT_SHALLOW_FILE``) is dropped too."""
    listed = subprocess.run(
        [GIT, "rev-parse", "--local-env-vars"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        env=_child_env(),
    ).stdout.split()
    only_git = [name for name in listed if name.startswith("GIT_") and name not in _ALL_NAMES]
    assert only_git, "git lists no name beyond the conftest's fixed tuple"
    left = _names_left_after_importing_the_conftest(extra_names=only_git[:1])
    assert left == "", f"the conftest left these set: {left}"


def test_conftest_falls_back_to_its_fixed_names_when_git_is_missing(tmp_path):
    """With no ``git`` on PATH the fixed tuple still drops all ten names."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    left = _names_left_after_importing_the_conftest(path=str(empty))
    assert left == "", f"the conftest left these set: {left}"
