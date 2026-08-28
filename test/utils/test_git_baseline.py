"""Tests for the run's repository baseline derivation (issue #583 Bolt 2, unit ``manifest-freeze``).

The contract under test is TOTALITY. Deriving a baseline must never be the reason a run cannot start, so
every failure mode — not a repository, ``git`` absent, unreadable directory, hung process — has to answer a
recorded absence rather than raise or block. Four of the six tests here exist for that alone.
"""

import os
import subprocess
import threading

from cli_agent_orchestrator.utils import git_baseline


def _initialise_repository(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("one", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def test_returns_commit_and_worktree_state_inside_a_repository(tmp_path):
    _initialise_repository(tmp_path)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert baseline["available"] is True
    assert len(baseline["commit"]) == 40
    assert baseline["worktree_state"] == {"status": "clean"}


def test_dirty_worktree_state_depends_on_the_uncommitted_contents(tmp_path):
    _initialise_repository(tmp_path)

    (tmp_path / "f.txt").write_text("two", encoding="utf-8")
    baseline_a = git_baseline.derive_baseline(str(tmp_path))

    (tmp_path / "f.txt").write_text("three", encoding="utf-8")
    baseline_b = git_baseline.derive_baseline(str(tmp_path))

    assert baseline_a["commit"] == baseline_b["commit"]
    assert baseline_a["worktree_state"] != baseline_b["worktree_state"]


def test_staged_changes_and_deletions_are_dirty_worktree_states(tmp_path):
    _initialise_repository(tmp_path)
    clean = git_baseline.derive_baseline(str(tmp_path))

    (tmp_path / "f.txt").write_text("staged", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True)
    staged = git_baseline.derive_baseline(str(tmp_path))

    subprocess.run(["git", "reset", "--hard", "HEAD"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").unlink()
    deleted = git_baseline.derive_baseline(str(tmp_path))

    assert staged["worktree_state"] != clean["worktree_state"]
    assert deleted["worktree_state"] != clean["worktree_state"]


def test_untracked_file_contents_affect_worktree_state(tmp_path):
    _initialise_repository(tmp_path)
    (tmp_path / "untracked.txt").write_text("one", encoding="utf-8")
    baseline_a = git_baseline.derive_baseline(str(tmp_path))

    (tmp_path / "untracked.txt").write_text("two", encoding="utf-8")
    baseline_b = git_baseline.derive_baseline(str(tmp_path))

    assert baseline_a["worktree_state"] != baseline_b["worktree_state"]


def test_untracked_file_is_hashed_in_bounded_chunks(monkeypatch, tmp_path):
    _initialise_repository(tmp_path)
    hash_chunk_bytes = 64 * 1024
    contents = b"x" * (hash_chunk_bytes * 2 + 1)
    untracked_path = tmp_path / "untracked.bin"
    untracked_path.write_bytes(contents)
    read_sizes = []
    actual_fdopen = os.fdopen

    class _TrackingFile:
        def __init__(self, file):
            self._file = file

        def read(self, size=-1):
            read_sizes.append(size)
            return self._file.read(size)

        def __getattr__(self, name):
            return getattr(self._file, name)

        def __enter__(self):
            self._file.__enter__()
            return self

        def __exit__(self, *args):
            return self._file.__exit__(*args)

    def _track_untracked_fdopen(descriptor, *args, **kwargs):
        return _TrackingFile(actual_fdopen(descriptor, *args, **kwargs))

    monkeypatch.setattr(os, "fdopen", _track_untracked_fdopen)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert baseline["worktree_state"]["status"] == "dirty"
    assert read_sizes
    assert -1 not in read_sizes
    assert all(0 < read_size <= hash_chunk_bytes for read_size in read_sizes)


def test_untracked_file_replaced_by_fifo_before_open_returns_unavailable_without_blocking(
    monkeypatch, tmp_path
):
    _initialise_repository(tmp_path)
    untracked_path = tmp_path / "untracked"
    untracked_path.write_bytes(b"contents")
    actual_lstat = os.lstat
    target_lstat_calls = 0

    def _replace_after_entry_validation(path, *args, **kwargs):
        nonlocal target_lstat_calls
        result = actual_lstat(path, *args, **kwargs)
        if os.fsencode(path) == os.fsencode(untracked_path):
            target_lstat_calls += 1
            if target_lstat_calls == 2:
                untracked_path.unlink()
                os.mkfifo(untracked_path)
        return result

    monkeypatch.setattr(os, "lstat", _replace_after_entry_validation)
    result = []
    completed = threading.Event()

    def _derive():
        result.append(git_baseline.derive_baseline(str(tmp_path)))
        completed.set()

    worker = threading.Thread(target=_derive, daemon=True)
    worker.start()
    try:
        assert completed.wait(1), "opening the replacement FIFO must not block baseline derivation"
    finally:
        if worker.is_alive():
            writer = os.open(untracked_path, os.O_WRONLY | os.O_NONBLOCK)
            os.close(writer)
        worker.join(timeout=1)

    assert result == [
        {
            "available": False,
            "commit": result[0]["commit"],
            "worktree_state": {"status": "unavailable"},
        }
    ]


def test_untracked_file_symlink_hashes_link_payload_without_dereferencing(tmp_path):
    _initialise_repository(tmp_path)
    external = tmp_path.parent / "external.txt"
    external.write_text("one", encoding="utf-8")
    link = tmp_path / "untracked-link"
    link.symlink_to(external)

    before = git_baseline.derive_baseline(str(tmp_path))
    external.write_text("two", encoding="utf-8")
    after_external_target_change = git_baseline.derive_baseline(str(tmp_path))

    replacement = tmp_path.parent / "replacement.txt"
    replacement.write_text("two", encoding="utf-8")
    link.unlink()
    link.symlink_to(replacement)
    after_link_retarget = git_baseline.derive_baseline(str(tmp_path))

    assert before["worktree_state"] == after_external_target_change["worktree_state"]
    assert before["worktree_state"] != after_link_retarget["worktree_state"]


def test_untracked_directory_symlink_does_not_collapse_dirty_state(tmp_path):
    _initialise_repository(tmp_path)
    external_directory = tmp_path.parent / "external-directory"
    external_directory.mkdir()
    (tmp_path / "untracked-directory").symlink_to(external_directory, target_is_directory=True)
    (tmp_path / "dirty.txt").write_text("one", encoding="utf-8")

    first = git_baseline.derive_baseline(str(tmp_path))
    (tmp_path / "dirty.txt").write_text("two", encoding="utf-8")
    second = git_baseline.derive_baseline(str(tmp_path))

    assert first["worktree_state"]["status"] == "dirty"
    assert first["worktree_state"] != second["worktree_state"]


def test_worktree_state_ignores_local_diff_presentation_settings(tmp_path):
    _initialise_repository(tmp_path)
    (tmp_path / "f.txt").write_text("two\nthree\nfour\n", encoding="utf-8")

    baseline_without_settings = git_baseline.derive_baseline(str(tmp_path))
    for key, value in (
        ("diff.context", "0"),
        ("diff.interHunkContext", "99"),
        ("diff.algorithm", "histogram"),
        ("diff.indentHeuristic", "true"),
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.renames", "true"),
        ("diff.submodule", "log"),
        ("core.quotePath", "false"),
        ("core.autocrlf", "true"),
        ("color.diff", "always"),
    ):
        subprocess.run(["git", "config", key, value], cwd=tmp_path, check=True)
    baseline_with_settings = git_baseline.derive_baseline(str(tmp_path))

    assert baseline_with_settings["worktree_state"] == baseline_without_settings["worktree_state"]


def test_records_an_unavailable_worktree_state_explicitly(monkeypatch, tmp_path):
    _initialise_repository(tmp_path)
    actual_run = subprocess.run

    def _timeout_worktree_snapshot(args, *args_rest, **kwargs):
        if "diff" in args:
            raise subprocess.TimeoutExpired(cmd=args, timeout=1)
        return actual_run(args, *args_rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", _timeout_worktree_snapshot)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert baseline == {
        "available": False,
        "commit": baseline["commit"],
        "worktree_state": {"status": "unavailable"},
    }


def test_untracked_hash_budget_exhaustion_records_an_unavailable_baseline(tmp_path):
    _initialise_repository(tmp_path)
    untracked_path = tmp_path / "untracked.bin"
    untracked_path.touch()
    # Without this fallback, pre-fix code fails with AttributeError instead of exercising behaviour.
    budget = getattr(git_baseline, "_UNTRACKED_HASH_BUDGET_BYTES", 64 * 1024 * 1024)
    os.truncate(untracked_path, budget + 1)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert baseline["available"] is False
    assert baseline["worktree_state"] == {"status": "unavailable"}


def test_records_absence_outside_a_repository(tmp_path):
    """A workspace outside git is entirely ordinary, not a fault."""
    assert git_baseline.derive_baseline(str(tmp_path)) == {"available": False}


def test_records_absence_when_git_is_missing(monkeypatch, tmp_path):
    """``git`` absent from PATH raises ``FileNotFoundError`` (an ``OSError``); it must not escape."""

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert git_baseline.derive_baseline(str(tmp_path)) == {"available": False}


def test_records_absence_on_timeout(monkeypatch, tmp_path):
    """A hung git must not block run start."""

    def _hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", _hang)
    assert git_baseline.derive_baseline(str(tmp_path)) == {"available": False}


def test_records_absence_on_unreadable_directory():
    """A nonexistent cwd surfaces as ``OSError`` from ``subprocess.run``; also an absence."""
    assert git_baseline.derive_baseline("/nonexistent/path/for/test") == {"available": False}


def test_captures_no_branch_and_no_path(tmp_path):
    """Only commit and worktree state. A path is environment-specific and would make plan_id machine-dependent.

    Including a branch or path would mean two machines running an identical plan derived different
    ``plan_id`` values, forcing a spurious re-approval on every machine change.
    """
    _initialise_repository(tmp_path)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert set(baseline) == {"available", "commit", "worktree_state"}
    assert str(tmp_path) not in str(baseline)
