"""Tests for operator-supplied working-directory normalization."""

import pytest

from cli_agent_orchestrator.utils.paths import normalize_working_directory


@pytest.fixture
def mnt(tmp_path):
    """A fake WSL interop mount with a c: drive."""
    (tmp_path / "mnt" / "c" / "Users" / "rkram" / "Desktop").mkdir(parents=True)
    return tmp_path / "mnt"


class TestNormalizeWorkingDirectory:
    def test_none_and_blank_pass_through(self, mnt):
        assert normalize_working_directory(None, mnt_root=mnt) is None
        assert normalize_working_directory("   ", mnt_root=mnt) is None

    def test_linux_path_unchanged(self, tmp_path, mnt):
        target = tmp_path / "proj"
        target.mkdir()
        assert normalize_working_directory(str(target), mnt_root=mnt) == str(target)

    def test_windows_backslash_path_translates_to_wsl_mount(self, mnt):
        result = normalize_working_directory(
            r"C:\Users\rkram\Desktop\task_management", mnt_root=mnt
        )
        assert result == str(mnt / "c" / "Users" / "rkram" / "Desktop" / "task_management")

    def test_windows_forward_slash_path_translates(self, mnt):
        result = normalize_working_directory("C:/Users/rkram/Desktop", mnt_root=mnt)
        assert result == str(mnt / "c" / "Users" / "rkram" / "Desktop")

    def test_explorer_copy_as_path_quotes_stripped(self, mnt):
        result = normalize_working_directory('"C:\\Users\\rkram\\Desktop"', mnt_root=mnt)
        assert result == str(mnt / "c" / "Users" / "rkram" / "Desktop")

    def test_missing_directory_is_created(self, mnt):
        result = normalize_working_directory(
            r"C:\Users\rkram\Desktop\brand_new\nested", mnt_root=mnt
        )
        expected = mnt / "c" / "Users" / "rkram" / "Desktop" / "brand_new" / "nested"
        assert result == str(expected)
        assert expected.is_dir()

    def test_missing_directory_rejected_when_create_disabled(self, mnt):
        with pytest.raises(ValueError, match="does not exist"):
            normalize_working_directory(
                r"C:\Users\rkram\Desktop\nope", mnt_root=mnt, create_missing=False
            )

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


class TestFsDirsEndpoint:
    """GH #282: server-side folder listing for the in-app browser."""

    def _client(self):
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry

        app.state.plugin_registry = PluginRegistry()
        return TestClient(app)

    def test_lists_only_directories_visible_first(self, tmp_path):
        (tmp_path / "beta").mkdir()
        (tmp_path / "alpha").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "a_file.txt").write_text("x")

        resp = self._client().get(
            "/fs/dirs", params={"path": str(tmp_path)}, headers={"Host": "localhost"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dirs"] == ["alpha", "beta", ".hidden"]
        assert body["path"] == str(tmp_path)
        assert body["parent"] == str(tmp_path.parent)

    def test_missing_folder_is_a_clear_400(self):
        resp = self._client().get(
            "/fs/dirs", params={"path": "/definitely/not/here"}, headers={"Host": "localhost"}
        )
        assert resp.status_code == 400

    def test_defaults_to_home(self):
        resp = self._client().get("/fs/dirs", headers={"Host": "localhost"})
        assert resp.status_code == 200
        from pathlib import Path

        assert resp.json()["path"] == str(Path.home())
