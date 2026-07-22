"""issue #100 Phase 1 -- worktree_service tests.

Covers:
- ``find_repo_root``: resolves from a subdirectory, raises outside a repo.
- ``create_worktree`` / ``remove_worktree``: real ``git worktree add``/
  ``remove``/branch-delete against a real local repo -- no subprocess mocking,
  same posture as ``test_project_identity.py``'s own real-git tests.
- ``remove_worktree`` tolerates uncommitted/untracked content left behind
  (agents commonly leave modified files -- ``--force`` is required for this).
- ``list_worktrees`` reflects real ``git worktree`` state, including entries
  ``create_worktree`` did not itself create.
- ``parse_worktree_path`` round-trips against paths ``worktree_path_for``
  produces, and rejects unrelated paths.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.worktree_service import (
    WorktreeError,
    branch_for,
    create_worktree,
    find_repo_root,
    list_worktrees,
    parse_worktree_path,
    remove_worktree,
    worktree_path_for,
)


def _git_available() -> bool:
    try:
        subprocess.run(["git", "--version"], capture_output=True, check=True, timeout=2)
        return True
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(not _git_available(), reason="git executable required")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True
    )
    (path / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=path, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


class TestFindRepoRoot:
    def test_resolves_from_repo_root_itself(self, repo: Path) -> None:
        assert find_repo_root(str(repo)) == str(repo.resolve())

    def test_resolves_from_a_subdirectory(self, repo: Path) -> None:
        subdir = repo / "src" / "pkg"
        subdir.mkdir(parents=True)
        assert find_repo_root(str(subdir)) == str(repo.resolve())

    def test_raises_outside_any_git_repository(self, tmp_path: Path) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError, match="is not inside a git repository"):
            find_repo_root(str(non_repo))

    def test_raises_worktree_error_not_a_raw_os_error_for_a_nonexistent_path(
        self, tmp_path: Path
    ) -> None:
        """Regression: a nonexistent start_path (e.g. a typo'd
        working_directory) must surface as the same clean WorktreeError as
        'exists but isn't a repo' -- not an uncaught FileNotFoundError from
        subprocess.run's own cwd resolution, which would reach the API
        boundary as an unhandled 500 instead of the intended 400."""
        nonexistent = tmp_path / "does" / "not" / "exist"
        with pytest.raises(WorktreeError):
            find_repo_root(str(nonexistent))


class TestCreateAndRemoveWorktree:
    def test_create_worktree_produces_a_real_checkout_on_its_own_branch(self, repo: Path) -> None:
        terminal_id = "term_abc123"
        path = create_worktree(str(repo), terminal_id)

        assert Path(path) == Path(worktree_path_for(str(repo), terminal_id))
        assert (Path(path) / "README.md").is_file()  # real checkout of HEAD's tree

        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path,
            capture_output=True,
            text=True,
            check=True,
        )
        assert branch_result.stdout.strip() == branch_for(terminal_id)

        # git itself agrees this is a real worktree of `repo`.
        list_result = subprocess.run(
            ["git", "worktree", "list"], cwd=repo, capture_output=True, text=True, check=True
        )
        assert path in list_result.stdout

    def test_create_worktree_raises_a_clear_error_outside_a_git_repository(
        self, tmp_path: Path
    ) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError):
            create_worktree(str(non_repo), "term_xyz")

    def test_remove_worktree_deletes_the_directory_and_the_branch(self, repo: Path) -> None:
        terminal_id = "term_clean01"
        path = create_worktree(str(repo), terminal_id)

        remove_worktree(str(repo), terminal_id)

        assert not Path(path).exists()
        branch_result = subprocess.run(
            ["git", "branch", "--list", branch_for(terminal_id)],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        assert branch_result.stdout.strip() == ""

    def test_remove_worktree_force_removes_uncommitted_and_untracked_content(
        self, repo: Path
    ) -> None:
        """Agents commonly leave modified/untracked files behind -- a plain
        (non-force) ``git worktree remove`` refuses in that case; this must
        not surface as a failure to the caller (teardown paths call this
        best-effort and must never raise)."""
        terminal_id = "term_dirty01"
        path = create_worktree(str(repo), terminal_id)
        (Path(path) / "scratch.txt").write_text("uncommitted work\n")
        (Path(path) / "README.md").write_text("modified\n")

        remove_worktree(str(repo), terminal_id)  # must not raise

        assert not Path(path).exists()

    def test_remove_worktree_on_an_already_removed_worktree_does_not_raise(
        self, repo: Path
    ) -> None:
        terminal_id = "term_gone01"
        create_worktree(str(repo), terminal_id)
        remove_worktree(str(repo), terminal_id)

        remove_worktree(str(repo), terminal_id)  # second call: must not raise

    def test_remove_worktree_on_a_nonexistent_repo_root_does_not_raise(self) -> None:
        """Regression: this function's own docstring promises 'never raises'
        (both terminal_service.delete_terminal's teardown path and
        create_terminal's own failure-cleanup path call it with no
        try/except, relying on that contract). A repo_root that no longer
        exists on disk (e.g. the parent clone was itself deleted between
        worktree creation and teardown) previously raised an uncaught
        FileNotFoundError from subprocess.run's own cwd resolution."""
        remove_worktree("/definitely/not/a/real/repo/root/anywhere", "term_x")  # must not raise


class TestListWorktrees:
    def test_lists_the_main_checkout_and_every_created_worktree(self, repo: Path) -> None:
        create_worktree(str(repo), "term_one")
        create_worktree(str(repo), "term_two")

        entries = list_worktrees(str(repo))

        paths = {e["worktree"] for e in entries if "worktree" in e}
        assert str(repo.resolve()) in paths
        assert worktree_path_for(str(repo), "term_one") in paths
        assert worktree_path_for(str(repo), "term_two") in paths

    def test_raises_outside_a_git_repository(self, tmp_path: Path) -> None:
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        with pytest.raises(WorktreeError):
            list_worktrees(str(non_repo))


class TestParseWorktreePath:
    def test_round_trips_against_worktree_path_for(self) -> None:
        repo_root = "/home/user/myrepo"
        terminal_id = "term_9f8e7d"
        path = worktree_path_for(repo_root, terminal_id)

        parsed = parse_worktree_path(path)

        assert parsed == (repo_root, terminal_id)

    def test_returns_none_for_a_path_outside_any_worktree_subdir(self) -> None:
        assert parse_worktree_path("/home/user/myrepo/src/pkg") is None

    def test_returns_none_for_none(self) -> None:
        assert parse_worktree_path(None) is None

    def test_returns_none_for_a_worktree_subdir_path_with_no_terminal_segment(self) -> None:
        # `.cao/worktrees` itself, or a nested extra path segment past the
        # terminal_id -- neither is a valid single terminal_id leaf.
        assert parse_worktree_path("/home/user/myrepo/.cao/worktrees/") is None
        assert parse_worktree_path("/home/user/myrepo/.cao/worktrees/term_x/extra") is None
