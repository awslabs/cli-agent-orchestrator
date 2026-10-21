"""Tests for the operator-supplied working-directory helpers.

Three functions, three responsibilities (plauzy, #724 review):

* ``normalize_working_directory`` is PURE -- spelling only, never touches the
  target;
* ``validate_working_directory`` applies the repository's path policy without
  side effects;
* ``ensure_working_directory`` creates, and is the only one allowed to.
"""

import os
import uuid
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils.paths import (
    WORKING_DIRECTORY_COMPONENT_MAX_LEN,
    WORKING_DIRECTORY_MAX_DEPTH,
    WORKING_DIRECTORY_MAX_LEN,
    WorkingDirectoryError,
    ensure_working_directory,
    normalize_working_directory,
    validate_working_directory,
)


@pytest.fixture
def mnt(tmp_path):
    """A fake WSL interop mount with a ``c:`` drive."""
    (tmp_path / "mnt" / "c" / "Users" / "operator" / "Desktop").mkdir(parents=True)
    return tmp_path / "mnt"


class TestNormalizeWorkingDirectory:
    def test_none_and_blank_pass_through(self, mnt):
        assert normalize_working_directory(None, mnt_root=mnt) is None
        assert normalize_working_directory("   ", mnt_root=mnt) is None
        assert normalize_working_directory('""', mnt_root=mnt) is None

    def test_linux_path_unchanged(self, tmp_path, mnt):
        target = tmp_path / "proj"
        target.mkdir()
        assert normalize_working_directory(str(target), mnt_root=mnt) == str(target)

    def test_tilde_expanded(self, mnt, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert normalize_working_directory("~/work", mnt_root=mnt) == str(tmp_path / "work")

    def test_windows_backslash_path_translates_to_wsl_mount(self, mnt):
        result = normalize_working_directory(r"C:\Users\operator\Desktop\task", mnt_root=mnt)
        assert result == str(mnt / "c" / "Users" / "operator" / "Desktop" / "task")

    def test_windows_forward_slash_path_translates(self, mnt):
        result = normalize_working_directory("C:/Users/operator/Desktop", mnt_root=mnt)
        assert result == str(mnt / "c" / "Users" / "operator" / "Desktop")

    def test_explorer_copy_as_path_quotes_stripped(self, mnt):
        result = normalize_working_directory('"C:\\Users\\operator\\Desktop"', mnt_root=mnt)
        assert result == str(mnt / "c" / "Users" / "operator" / "Desktop")

    def test_escaped_backslashes_keep_the_drive(self, mnt):
        """``C:\\\\Users\\me`` -- a paste out of JSON or a Python literal -- made
        ``rest`` start with a separator, and ``drive_mount / "/Users/me"`` is
        ``/Users/me``: the drive was dropped entirely (plauzy, #724 review)."""
        result = normalize_working_directory("C:\\\\Users\\operator\\Desktop", mnt_root=mnt)
        assert result == str(mnt / "c" / "Users" / "operator" / "Desktop")

    def test_dot_dot_cannot_walk_out_of_the_drive_mount(self, mnt):
        """The mount-exists check used to run on the drive root BEFORE
        ``normpath``, so ``C:\\..\\..\\Windows`` -> ``/mnt/c/../../Windows`` ->
        ``/Windows`` escaped the mount just validated."""
        with pytest.raises(WorkingDirectoryError, match="leaves drive C:"):
            normalize_working_directory(r"C:\..\..\Windows", mnt_root=mnt)

    def test_missing_directory_is_not_created(self, mnt):
        """Pure: the spelling is cleaned, nothing is stat'ed or made. Creation
        is ``ensure_working_directory``'s job, at the point of use."""
        target = mnt / "c" / "Users" / "operator" / "Desktop" / "brand_new" / "nested"
        result = normalize_working_directory(
            r"C:\Users\operator\Desktop\brand_new\nested", mnt_root=mnt
        )
        assert result == str(target)
        assert not target.exists()

    def test_unmounted_drive_gives_clear_error(self, mnt):
        with pytest.raises(WorkingDirectoryError, match="drive Z: is not mounted"):
            normalize_working_directory(r"Z:\projects\app", mnt_root=mnt)

    def test_relative_path_rejected(self, mnt):
        with pytest.raises(WorkingDirectoryError, match="absolute"):
            normalize_working_directory("projects/app", mnt_root=mnt)

    def test_length_is_bounded(self, tmp_path, mnt):
        long = tmp_path.joinpath(*["x" * 200 for _ in range(30)])
        assert len(str(long)) > WORKING_DIRECTORY_MAX_LEN
        with pytest.raises(WorkingDirectoryError, match="too long"):
            normalize_working_directory(str(long), mnt_root=mnt)

    def test_component_over_name_max_is_rejected_before_any_stat(self, tmp_path, mnt):
        """The total-length cap is 4096 but the limit that trips is the OS's
        per-component 255: ``Path.exists()`` on such a path raises
        ``OSError(ENAMETOOLONG)``, which used to escape both create endpoints
        as a 500 (plauzy, #724 review). Refused lexically, so nothing is
        stat'ed at all."""
        target = tmp_path / ("x" * (WORKING_DIRECTORY_COMPONENT_MAX_LEN + 45))
        assert len(str(target)) < WORKING_DIRECTORY_MAX_LEN
        with pytest.raises(WorkingDirectoryError, match="folder name longer than"):
            normalize_working_directory(str(target), mnt_root=mnt)

    def test_unknown_user_tilde_is_a_clear_error(self, mnt):
        """``expanduser`` raises RuntimeError for ``~nobody``; that has to
        become the same error family every other unusable spelling produces,
        or the endpoints answer 500."""
        with pytest.raises(WorkingDirectoryError, match="Cannot expand"):
            normalize_working_directory("~definitely-not-a-user-here/proj", mnt_root=mnt)

    def test_dot_dot_is_cancelled_lexically(self, tmp_path, mnt):
        """``a/../b`` cancels ``a`` on paper; the later ``mkdir(parents=True)``
        must never see ``a`` at all."""
        result = normalize_working_directory(
            str(tmp_path / "cancelled" / ".." / "kept"), mnt_root=mnt
        )
        assert result == str(tmp_path / "kept")
        assert not (tmp_path / "cancelled").exists()
        assert not (tmp_path / "kept").exists()

    def test_error_is_a_value_error(self, mnt):
        """Existing ``except ValueError`` arms keep working."""
        assert issubclass(WorkingDirectoryError, ValueError)


class TestValidateWorkingDirectory:
    """The repository's own path policy (#345, design D5), side-effect free."""

    def test_existing_allowed_directory_passes_canonicalized(self, tmp_path):
        link = tmp_path / "link"
        real = tmp_path / "real"
        real.mkdir()
        link.symlink_to(real)
        assert validate_working_directory(str(link)) == os.path.realpath(real)

    def test_missing_directory_under_an_allowed_parent_passes_and_is_not_created(self, tmp_path):
        target = tmp_path / "new" / "deeper"
        assert validate_working_directory(str(target)) == os.path.realpath(target)
        assert not target.exists()

    def test_missing_directory_under_a_blocked_root_is_refused(self):
        """``/tmp`` is in BLOCKED_SYSTEM_DIRECTORIES. The old helper mkdir'ed
        ``/tmp/<new>`` at the HTTP boundary, after which the backend's
        ``allow_create=False`` check saw an existing, non-exact-match path and
        accepted it: the one control meant to bound the mkdir was stepped
        around (plauzy, #724 review, blocking item 1)."""
        target = Path("/tmp") / f"cao-724-{uuid.uuid4().hex}"
        try:
            with pytest.raises(WorkingDirectoryError, match="nearest existing ancestor"):
                validate_working_directory(str(target))
            assert not target.exists()
        finally:
            if target.exists():  # pragma: no cover - only on a failed assertion
                target.rmdir()

    def test_blocked_subtree_is_refused(self):
        with pytest.raises(WorkingDirectoryError, match="not allowed"):
            validate_working_directory(f"/etc/cao-724-{uuid.uuid4().hex}")

    def test_existing_non_directory_is_refused(self, tmp_path):
        """Not only regular files: a FIFO, socket or device node would
        otherwise pass and fail later inside tmux with exactly the opaque
        error the normalization exists to prevent."""
        fifo = tmp_path / "afifo"
        os.mkfifo(fifo)
        with pytest.raises(WorkingDirectoryError, match="is not a folder"):
            validate_working_directory(str(fifo))
        afile = tmp_path / "afile"
        afile.write_text("x")
        with pytest.raises(WorkingDirectoryError, match="is not a folder"):
            validate_working_directory(str(afile))


class TestEnsureWorkingDirectory:
    """The only helper allowed to create, and everything it can fail on is a 400."""

    def test_missing_directory_is_created(self, tmp_path):
        target = tmp_path / "brand_new" / "nested"
        assert ensure_working_directory(str(target)) == os.path.realpath(target)
        assert target.is_dir()

    def test_existing_directory_is_left_alone(self, tmp_path):
        target = tmp_path / "proj"
        target.mkdir()
        marker = target / "keep"
        marker.write_text("x")
        assert ensure_working_directory(str(target)) == os.path.realpath(target)
        assert marker.read_text() == "x"

    def test_creation_is_bounded_by_depth(self, tmp_path):
        deep = tmp_path.joinpath(*[f"d{i}" for i in range(WORKING_DIRECTORY_MAX_DEPTH + 5)])
        with pytest.raises(WorkingDirectoryError, match="nested too deeply"):
            ensure_working_directory(str(deep))
        assert not deep.exists()
        # ...and nothing on the way down either.
        assert not (tmp_path / "d0").exists()

    def test_bounds_do_not_apply_to_an_existing_directory(self, tmp_path):
        """The caps bound what we CREATE. A deep tree that already exists is
        the operator's own layout and is none of our business."""
        deep = tmp_path.joinpath(*[f"d{i}" for i in range(WORKING_DIRECTORY_MAX_DEPTH + 5)])
        deep.mkdir(parents=True)
        assert ensure_working_directory(str(deep)) == os.path.realpath(deep)

    def test_uncreatable_directory_gives_clear_error(self, tmp_path):
        """A path whose parent is a FILE cannot be created; the OSError must
        surface as the same error family the endpoints turn into a 400."""
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(
            WorkingDirectoryError, match="no existing ancestor|could not be created"
        ):
            ensure_working_directory(str(blocker / "child"))

    def test_component_over_name_max_is_a_clear_error_not_an_oserror(self, tmp_path):
        """Reached directly (bypassing the lexical check in the normalizer):
        the stat and the mkdir both raise ``OSError(ENAMETOOLONG)``, and both
        must come out as WorkingDirectoryError, never a bare OSError."""
        target = tmp_path / ("x" * 300)
        with pytest.raises(WorkingDirectoryError):
            ensure_working_directory(str(target))
        # os.path.exists, not Path.exists: the latter raises ENAMETOOLONG itself.
        assert not os.path.exists(target)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
    def test_non_traversable_parent_is_a_clear_error(self, tmp_path):
        """``PermissionError`` from a parent the server cannot traverse is an
        OSError too; same contract."""
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            with pytest.raises(WorkingDirectoryError):
                ensure_working_directory(str(locked / "child"))
        finally:
            locked.chmod(0o755)

    def test_blocked_path_is_never_created(self):
        """The policy check runs inside ensure too, so a caller that skipped
        ``validate_working_directory`` still cannot mkdir under ``/tmp``."""
        target = Path("/tmp") / f"cao-724-{uuid.uuid4().hex}"
        try:
            with pytest.raises(WorkingDirectoryError, match="not allowed"):
                ensure_working_directory(str(target))
            assert not target.exists()
        finally:
            if target.exists():  # pragma: no cover - only on a failed assertion
                target.rmdir()
