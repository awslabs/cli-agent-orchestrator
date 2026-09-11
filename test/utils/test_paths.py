"""Tests for the operator-supplied working-directory normalization."""

import os

import pytest

from cli_agent_orchestrator.utils.paths import (
    WORKING_DIRECTORY_MAX_DEPTH,
    WORKING_DIRECTORY_MAX_LEN,
    normalize_working_directory,
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
        (tmp_path / "work").mkdir()
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

    def test_missing_directory_is_created(self, mnt):
        result = normalize_working_directory(
            r"C:\Users\operator\Desktop\brand_new\nested", mnt_root=mnt
        )
        expected = mnt / "c" / "Users" / "operator" / "Desktop" / "brand_new" / "nested"
        assert result == str(expected)
        assert expected.is_dir()

    def test_missing_directory_rejected_when_create_disabled(self, mnt):
        target = mnt / "c" / "Users" / "operator" / "Desktop" / "nope"
        with pytest.raises(ValueError, match="does not exist"):
            normalize_working_directory(
                r"C:\Users\operator\Desktop\nope", mnt_root=mnt, create_missing=False
            )
        assert not target.exists(), "read-only callers must not touch the filesystem"

    def test_unmounted_drive_gives_clear_error(self, mnt):
        with pytest.raises(ValueError, match="drive Z: is not mounted"):
            normalize_working_directory(r"Z:\projects\app", mnt_root=mnt)

    def test_relative_path_rejected(self, mnt):
        with pytest.raises(ValueError, match="absolute"):
            normalize_working_directory("projects/app", mnt_root=mnt)

    def test_file_rejected(self, tmp_path, mnt):
        f = tmp_path / "afile.txt"
        f.write_text("x")
        with pytest.raises(ValueError, match="is not a folder"):
            normalize_working_directory(str(f), mnt_root=mnt)

    def test_existing_non_directory_rejected(self, tmp_path, mnt):
        """Not only regular files: a FIFO, socket or device node would
        otherwise pass and fail later inside tmux with exactly the opaque
        error this function exists to prevent."""
        fifo = tmp_path / "afifo"
        os.mkfifo(fifo)
        with pytest.raises(ValueError, match="is not a folder"):
            normalize_working_directory(str(fifo), mnt_root=mnt)

    def test_creation_is_bounded_by_depth(self, tmp_path, mnt):
        deep = tmp_path.joinpath(*[f"d{i}" for i in range(WORKING_DIRECTORY_MAX_DEPTH + 5)])
        with pytest.raises(ValueError, match="nested too deeply"):
            normalize_working_directory(str(deep), mnt_root=mnt)
        assert not deep.exists()

    def test_length_is_bounded_before_any_filesystem_call(self, tmp_path, mnt):
        """Built from many legal-length components, so the limit under test is
        ours and not the OS's per-component one. The check has to run before
        exists(), which would itself raise ENAMETOOLONG and escape as a 500."""
        long = tmp_path.joinpath(*["x" * 200 for _ in range(30)])
        assert len(str(long)) > WORKING_DIRECTORY_MAX_LEN
        with pytest.raises(ValueError, match="too long"):
            normalize_working_directory(str(long), mnt_root=mnt)

    def test_bounds_do_not_apply_to_an_existing_directory(self, tmp_path, mnt):
        """The caps bound what we CREATE. A deep tree that already exists is
        the operator's own layout and is none of our business."""
        deep = tmp_path.joinpath(*[f"d{i}" for i in range(WORKING_DIRECTORY_MAX_DEPTH + 5)])
        deep.mkdir(parents=True)
        assert normalize_working_directory(str(deep), mnt_root=mnt) == str(deep)

    def test_uncreatable_directory_gives_clear_error(self, tmp_path, mnt):
        # A path whose parent is a FILE cannot be created; the OSError must
        # surface as the same ValueError family the endpoints turn into a 400.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(ValueError, match="could not be created"):
            normalize_working_directory(str(blocker / "child"), mnt_root=mnt)
