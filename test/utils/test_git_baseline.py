"""Tests for the run's repository baseline derivation (issue #583 Bolt 2, unit ``manifest-freeze``).

The contract under test is TOTALITY with a determinate distinction: a non-repository is an approvable
recorded state, while ``git`` absence, unreadable directories, and timeouts remain unavailable rather
than being mistaken for that state.
"""

import os
import subprocess
import threading
from types import SimpleNamespace

import pytest

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
            "reason": "changed_during_snapshot",
            "commit": result[0]["commit"],
            "worktree_state": {"status": "unavailable", "reason": "changed_during_snapshot"},
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
        "reason": "git_timeout",
        "commit": baseline["commit"],
        "worktree_state": {"status": "unavailable", "reason": "git_timeout"},
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
    assert baseline["worktree_state"] == {"status": "unavailable", "reason": "untracked_too_large"}
    assert baseline["reason"] == "untracked_too_large"


def test_records_absence_outside_a_repository(tmp_path):
    """A workspace outside git is entirely ordinary, not a fault."""
    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": True,
        "repository": False,
        "root": os.path.realpath(tmp_path),
        "root_identity": git_baseline.root_identity(str(tmp_path)),
    }


def test_plain_directory_proof_does_not_depend_on_git_stderr(monkeypatch, tmp_path):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 128, stdout="", stderr="localized or empty diagnostic"
        ),
    )

    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": True,
        "repository": False,
        "root": os.path.realpath(tmp_path),
        "root_identity": git_baseline.root_identity(str(tmp_path)),
    }


def test_failed_probe_with_git_marker_is_not_treated_as_plain_directory(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    actual_run = subprocess.run

    def _failed_probe(args, *rest, **kwargs):
        if "rev-parse" in args and "--is-inside-work-tree" in args:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="arbitrary")
        return actual_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", _failed_probe)
    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": False,
        "reason": "repository_unreadable",
    }


def test_broken_git_marker_is_not_treated_as_plain_directory(monkeypatch, tmp_path):
    (tmp_path / ".git").symlink_to(tmp_path / "missing")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **kwargs: subprocess.CompletedProcess(
            args, 128, stdout="", stderr="not classified by text"
        ),
    )

    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": False,
        "reason": "repository_unreadable",
    }


def test_inherited_git_dir_cannot_redirect_the_selected_root(monkeypatch, tmp_path):
    declared = tmp_path / "declared"
    redirected = tmp_path / "redirected"
    declared.mkdir()
    redirected.mkdir()
    _initialise_repository(declared)
    _initialise_repository(redirected)
    (redirected / "f.txt").write_text("redirected", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=redirected, check=True)
    subprocess.run(["git", "commit", "-qm", "redirected"], cwd=redirected, check=True)

    expected = git_baseline.derive_baseline(str(declared))
    monkeypatch.setenv("GIT_DIR", str(redirected / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))

    assert git_baseline.derive_baseline(str(declared)) == expected


def test_git_environment_is_snapshotted_once_and_all_invocations_are_neutral(monkeypatch, tmp_path):
    _initialise_repository(tmp_path)
    monkeypatch.setenv("GIT_DIR", "/redirect")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.fsmonitor")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "evil")
    actual_run = subprocess.run
    observed = []

    def _record(args, *rest, **kwargs):
        if args and args[0] == "git":
            observed.append((tuple(args), kwargs["env"]))
        return actual_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", _record)
    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert baseline["available"] is True
    assert observed
    assert len({id(environment) for _args, environment in observed}) == 1
    for args, environment in observed:
        assert args[1:4] == ("--no-optional-locks", "-c", "core.fsmonitor=false")
        assert "GIT_DIR" not in environment
        assert "GIT_CONFIG_COUNT" not in environment
        assert "GIT_CONFIG_GLOBAL" not in environment
        assert "GIT_CONFIG_NOSYSTEM" not in environment
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert environment["LC_ALL"] == "C"
        assert environment["LANG"] == "C"


def test_baseline_does_not_write_the_index(tmp_path):
    _initialise_repository(tmp_path)
    index = tmp_path / ".git" / "index"
    before = (index.read_bytes(), index.stat().st_mtime_ns)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert baseline["available"] is True
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before


def test_records_absence_when_git_is_missing(monkeypatch, tmp_path):
    """``git`` absent from PATH raises ``FileNotFoundError`` (an ``OSError``); it must not escape."""

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": False,
        "reason": "git_unavailable",
    }


def test_records_absence_on_timeout(monkeypatch, tmp_path):
    """A hung git must not block run start."""

    def _hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(subprocess, "run", _hang)
    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": False,
        "reason": "git_timeout",
    }


def test_records_absence_on_unreadable_directory():
    """A nonexistent cwd surfaces as ``OSError`` from ``subprocess.run``; also an absence."""
    assert git_baseline.derive_baseline("/nonexistent/path/for/test") == {
        "available": False,
        "reason": "git_unavailable",
    }


def test_captures_no_branch_and_no_path(tmp_path):
    """Only commit and worktree state. A path is environment-specific and would make plan_id machine-dependent.

    Including a branch or path would mean two machines running an identical plan derived different
    ``plan_id`` values, forcing a spurious re-approval on every machine change.
    """
    _initialise_repository(tmp_path)

    baseline = git_baseline.derive_baseline(str(tmp_path))

    assert set(baseline) == {"available", "commit", "worktree_state"}
    assert str(tmp_path) not in str(baseline)


def test_non_git_roots_and_aliases_have_directory_identity(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    sub = a / "sub"
    sub.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(a, target_is_directory=True)
    results = [git_baseline.derive_baseline(str(path)) for path in [a, b, sub]]
    assert [value["root"] for value in results] == [os.path.realpath(path) for path in [a, b, sub]]
    assert len({value["root"] for value in results}) == 3
    assert git_baseline.derive_baseline(str(alias)) == results[0]


# ---------------------------------------------------------------------------
# A non-Git root is identified by device and inode, not only by its path
# (issue #827 criterion 2)
# ---------------------------------------------------------------------------


def test_a_non_git_root_replaced_at_the_same_path_is_a_different_baseline(tmp_path):
    """THE DEFECT. A path is not an identity, and the baseline used to record only the path.

    Move the approved directory away, put a different one at the same path, and every value the
    baseline carried before this fix is identical -- so a plan frozen against the first directory
    describes the second one just as well.
    """
    root = tmp_path / "work"
    root.mkdir()
    before = git_baseline.derive_baseline(str(root))

    root.rename(tmp_path / "moved-away")
    (tmp_path / "work").mkdir()
    after = git_baseline.derive_baseline(str(root))

    assert before["root"] == after["root"], "fixture assumption broken: the path must be unchanged"
    assert before != after, (
        "a directory moved away and replaced at the same path produced the same baseline, so its "
        "plan_id and therefore its approval are reused for a directory nobody reviewed (#827)"
    )
    assert before["root_identity"]["bound"] and after["root_identity"]["bound"]
    assert before["root_identity"] != after["root_identity"]


def test_root_identity_is_stable_across_reads_and_follows_the_resolved_root(tmp_path):
    """The binding must not cost a re-approval on every run of an unchanged directory."""
    root = tmp_path / "work"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    identity = git_baseline.root_identity(str(root))
    assert identity["bound"] is True
    assert identity == git_baseline.root_identity(str(root))
    # ``os.stat``, not ``os.lstat``: the run executes in the target, so a symlinked root is the same
    # directory and must be the same plan (see the alias case above).
    assert identity == git_baseline.root_identity(str(alias))
    assert identity["device"].isdigit() and identity["inode"].isdigit()


def test_a_git_root_records_no_device_or_inode(tmp_path):
    """The asymmetry is deliberate, and this is the test that would fail if it were "tidied up".

    A Git root's identity is its commit and worktree digest -- content, not location -- so two
    checkouts in the same state are ONE plan on one machine or two. Device and inode are per-machine
    and per-copy, so recording them for a Git root would force a re-approval for every fresh clone or
    worktree while adding nothing: a different directory at the same path already has a different
    commit or a different worktree digest.
    """
    _initialise_repository(tmp_path)
    baseline = git_baseline.derive_baseline(str(tmp_path))
    assert "root_identity" not in baseline
    identity = git_baseline.root_identity(str(tmp_path))
    assert identity["inode"] not in str(baseline)


def test_a_filesystem_with_no_usable_inode_falls_back_with_a_recorded_reason(monkeypatch, tmp_path):
    """Network mounts, some container overlays and Windows volumes without a file index report 0.

    Zero is shared by every directory, so comparing it would prove nothing while reading as a
    successful binding. The fallback is today's path-only identity, and it is RECORDED: the unbound
    marker sits in the baseline, so it reaches ``plan_id`` and the manifest rather than vanishing.
    """
    real_stat = os.stat

    def zero_inode(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        return SimpleNamespace(st_dev=result.st_dev, st_ino=0, st_mode=result.st_mode)

    monkeypatch.setattr(os, "stat", zero_inode)
    assert git_baseline.root_identity(str(tmp_path)) == {
        "bound": False,
        "reason": "root_identity_unsupported",
    }


def test_an_unstattable_root_falls_back_with_its_own_recorded_reason():
    assert git_baseline.root_identity("/nonexistent/path/for/test") == {
        "bound": False,
        "reason": "root_identity_unreadable",
    }


def test_the_unbound_reasons_are_closed_and_are_not_refusal_reasons():
    """An unbindable root must not make a run unapprovable.

    ``UNAVAILABLE_REASONS`` is the vocabulary ``approval_gate`` turns into a 503 that refuses the run.
    Putting these two reasons in it would make every script run on a filesystem with no usable inode
    permanently unrunnable, which is strictly worse than the path-only identity those runs have today.
    """
    assert git_baseline.ROOT_IDENTITY_UNBOUND_REASONS == frozenset(
        {"root_identity_unsupported", "root_identity_unreadable"}
    )
    assert not (git_baseline.ROOT_IDENTITY_UNBOUND_REASONS & git_baseline.UNAVAILABLE_REASONS)


def test_an_unbound_root_identity_still_yields_an_available_baseline(monkeypatch, tmp_path):
    monkeypatch.setattr(git_baseline, "root_identity", lambda path: {"bound": False, "reason": "x"})
    baseline = git_baseline.derive_baseline(str(tmp_path))
    assert baseline["available"] is True
    assert baseline["root_identity"] == {"bound": False, "reason": "x"}


def test_derive_baseline_binds_the_identity_it_is_handed_rather_than_taking_a_second_stat(tmp_path):
    """The P3-3 seam, for the root. One stat behind the identifier and the guard, not two.

    ``manifest_freeze`` takes the stat once and threads it through both consumers, so a root replaced
    between the freeze and the launch-guard capture cannot be recorded as the approved one.
    """
    root = tmp_path / "work"
    root.mkdir()
    handed = {"bound": True, "device": "7", "inode": "11"}

    baseline = git_baseline.derive_baseline(str(root), identity=handed)

    assert baseline["root_identity"] == handed
    assert baseline["root_identity"] != git_baseline.root_identity(str(root))


@pytest.mark.parametrize(
    "seam,reason",
    [
        ("marker", "git_unavailable"),
        ("missing", "git_unavailable"),
        ("timeout", "git_timeout"),
        ("probe", "repository_unreadable"),
        ("head", "repository_unreadable"),
        ("diff", "repository_unreadable"),
        ("untracked", "repository_unreadable"),
        ("fifo", "untracked_unsupported"),
        ("flags", "untracked_unsupported"),
        ("inode", "changed_during_snapshot"),
        ("hash-error", "changed_during_snapshot"),
    ],
)
def test_unavailable_reason_covers_failure_seams(tmp_path, monkeypatch, seam, reason):
    _initialise_repository(tmp_path)
    if seam == "marker":
        monkeypatch.setattr(git_baseline, "_git_marker_state", lambda cwd: None)
    elif seam in {"missing", "timeout", "probe", "head", "diff", "untracked"}:
        real_run = subprocess.run

        def fail(args, **kwargs):
            matches = (
                seam in {"missing", "timeout"}
                or (seam == "probe" and "--is-inside-work-tree" in args)
                or (seam == "head" and "HEAD" in args and "rev-parse" in args)
                or (seam == "diff" and "diff" in args)
                or (seam == "untracked" and "ls-files" in args)
            )
            if matches:
                if seam == "missing":
                    raise FileNotFoundError("private git executable")
                if seam == "timeout":
                    raise subprocess.TimeoutExpired(args, 10)
                return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(tmp_path))
            return real_run(args, **kwargs)

        monkeypatch.setattr(subprocess, "run", fail)
    else:
        entry = tmp_path / "entry"
        if seam == "fifo":
            os.mkfifo(entry)
            real_run = subprocess.run

            def listed_entry(args, **kwargs):
                if "ls-files" in args:
                    return subprocess.CompletedProcess(args, 0, stdout=b"entry\0", stderr=b"")
                return real_run(args, **kwargs)

            monkeypatch.setattr(subprocess, "run", listed_entry)
        else:
            entry.write_bytes(b"data")
        if seam == "flags":
            monkeypatch.setattr(os, "O_NOFOLLOW", None)
        if seam == "inode":
            original = os.fstat

            def raced(fd):
                result = original(fd)
                return SimpleNamespace(
                    st_mode=result.st_mode,
                    st_dev=result.st_dev,
                    st_ino=result.st_ino + 1,
                    st_size=result.st_size,
                )

            monkeypatch.setattr(os, "fstat", raced)
        if seam == "hash-error":

            def unreadable(fd):
                raise OSError(str(tmp_path))

            monkeypatch.setattr(os, "fstat", unreadable)
    result = git_baseline.derive_baseline(str(tmp_path))
    assert result["available"] is False
    assert result["reason"] == reason
    assert result["reason"] in git_baseline.UNAVAILABLE_REASONS


def test_empty_git_repository_is_repository_unreadable(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert git_baseline.derive_baseline(str(tmp_path)) == {
        "available": False,
        "reason": "repository_unreadable",
    }
