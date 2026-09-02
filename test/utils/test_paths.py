"""Tests for the operator-supplied working-directory normalization."""

import pytest

from cli_agent_orchestrator.utils.paths import normalize_working_directory


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
        with pytest.raises(ValueError, match="is a file"):
            normalize_working_directory(str(f), mnt_root=mnt)

    def test_uncreatable_directory_gives_clear_error(self, tmp_path, mnt):
        # A path whose parent is a FILE cannot be created; the OSError must
        # surface as the same ValueError family the endpoints turn into a 400.
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        with pytest.raises(ValueError, match="could not be created"):
            normalize_working_directory(str(blocker / "child"), mnt_root=mnt)
