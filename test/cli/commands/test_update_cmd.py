"""Tests for the ``cao update`` command (issue #26)."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.update import (
    _build_command,
    _git_source_from_receipt,
    _local_source_from_receipt,
    update,
)

_MOD = "cli_agent_orchestrator.cli.commands.update"

# Real uv format (verified against uv 0.8.x): the git ref is embedded in the
# URL as a ``?rev=<ref>`` query param, NOT a separate rev/branch/tag key.
_GIT_RECEIPT_REAL = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", git = "https://github.com/awslabs/cli-agent-orchestrator.git?rev=main" }]
"""

# Bare git URL with no ref (uv omits ?rev= when installed without @<ref>).
_GIT_RECEIPT_NO_REF = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", git = "https://github.com/awslabs/cli-agent-orchestrator.git" }]
"""

# Defensive: a hypothetical separate-key rev shape (older/other uv versions).
_GIT_RECEIPT_SEPARATE_REV = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", git = "https://github.com/awslabs/cli-agent-orchestrator.git", branch = "main" }]
"""

# Real uv format for a PyPI/registry install: a bare name, no specifier key.
_REGISTRY_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator" }]
"""

_DIRECTORY_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", directory = "/home/me/cli-agent-orchestrator" }]
"""

# Real uv format for a wheel/path install (verified: mcp-obsidian uses this).
_PATH_RECEIPT = """
[tool]
requirements = [{ name = "cli-agent-orchestrator", path = "/home/me/dist/cli_agent_orchestrator-2.3.0-py3-none-any.whl" }]
"""


def _completed(returncode):
    result = MagicMock()
    result.returncode = returncode
    return result


class TestGitSourceFromReceipt:
    """_git_source_from_receipt classifies the install source from the receipt."""

    def test_real_git_receipt_with_rev_query(self, tmp_path):
        # The exact shape uv writes for `uv tool install git+...@main`.
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_GIT_RECEIPT_REAL)
        assert (
            _git_source_from_receipt(r)
            == "git+https://github.com/awslabs/cli-agent-orchestrator.git?rev=main"
        )

    def test_git_receipt_no_ref(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_GIT_RECEIPT_NO_REF)
        assert (
            _git_source_from_receipt(r)
            == "git+https://github.com/awslabs/cli-agent-orchestrator.git"
        )

    def test_git_receipt_separate_rev_key_pins_rev(self, tmp_path):
        # Defensive against a receipt that carries the ref as a separate key.
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_GIT_RECEIPT_SEPARATE_REV)
        assert (
            _git_source_from_receipt(r)
            == "git+https://github.com/awslabs/cli-agent-orchestrator.git@main"
        )

    def test_registry_receipt_returns_none(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_REGISTRY_RECEIPT)
        assert _git_source_from_receipt(r) is None

    def test_directory_receipt_returns_none(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_DIRECTORY_RECEIPT)
        assert _git_source_from_receipt(r) is None


class TestLocalSourceFromReceipt:
    """_local_source_from_receipt surfaces a local directory/path install."""

    def test_directory_receipt_returns_kind_and_path(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_DIRECTORY_RECEIPT)
        assert _local_source_from_receipt(r) == (
            "directory",
            "/home/me/cli-agent-orchestrator",
        )

    def test_path_receipt_returns_kind_and_path(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_PATH_RECEIPT)
        assert _local_source_from_receipt(r) == (
            "path",
            "/home/me/dist/cli_agent_orchestrator-2.3.0-py3-none-any.whl",
        )

    def test_registry_receipt_returns_none(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_REGISTRY_RECEIPT)
        assert _local_source_from_receipt(r) is None

    def test_git_receipt_returns_none(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text(_GIT_RECEIPT_REAL)
        assert _local_source_from_receipt(r) is None

    def test_unparseable_receipt_returns_none(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text("this is : not : valid toml [[[")
        assert _local_source_from_receipt(r) is None


class TestBuildCommand:
    def test_git_source_reinstalls(self):
        cmd = _build_command("git+https://example.com/x.git")
        assert cmd == [
            "uv",
            "tool",
            "install",
            "git+https://example.com/x.git",
            "--upgrade",
            "--reinstall",
        ]

    def test_no_git_source_upgrades(self):
        assert _build_command(None) == [
            "uv",
            "tool",
            "upgrade",
            "cli-agent-orchestrator",
        ]


class TestUpdateCommand:
    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._git_source_from_receipt", return_value=None)
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_registry_install_runs_upgrade(self, _which, _receipt, _git, mock_run):
        mock_run.return_value = _completed(0)
        result = CliRunner().invoke(update, [])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(["uv", "tool", "upgrade", "cli-agent-orchestrator"])
        assert "up to date" in result.output

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._local_source_from_receipt", return_value=None)
    @patch(
        f"{_MOD}._git_source_from_receipt",
        return_value="git+https://github.com/awslabs/cli-agent-orchestrator.git",
    )
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_git_install_forces_reinstall(self, _which, _receipt, _git, _local, mock_run):
        mock_run.return_value = _completed(0)
        result = CliRunner().invoke(update, [])

        assert result.exit_code == 0
        mock_run.assert_called_once_with(
            [
                "uv",
                "tool",
                "install",
                "git+https://github.com/awslabs/cli-agent-orchestrator.git",
                "--upgrade",
                "--reinstall",
            ]
        )

    @patch(f"{_MOD}.subprocess.run")
    @patch(
        f"{_MOD}._local_source_from_receipt",
        return_value=("directory", "/home/me/cli-agent-orchestrator"),
    )
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_local_dir_install_informs_without_running_uv(self, _which, _receipt, _local, mock_run):
        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "local directory" in result.output
        assert "/home/me/cli-agent-orchestrator" in result.output
        assert "git -C" in result.output  # directory-specific guidance
        # Must NOT run a no-op uv upgrade for a local install.
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(
        f"{_MOD}._local_source_from_receipt",
        return_value=("path", "/home/me/dist/cao.whl"),
    )
    @patch(f"{_MOD}._receipt_path")
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_path_install_informs_without_running_uv(self, _which, _receipt, _local, mock_run):
        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "local path" in result.output
        assert "/home/me/dist/cao.whl" in result.output
        assert "git -C" not in result.output  # path guidance omits git pull
        mock_run.assert_not_called()

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}.shutil.which", return_value=None)
    def test_missing_uv_is_a_clickexception(self, _which, mock_run):
        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "uv is not on PATH" in result.output
        mock_run.assert_not_called()  # fails BEFORE any subprocess

    @patch(f"{_MOD}.subprocess.run")
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_nonzero_exit_surfaces_error(self, _which, _receipt, mock_run):
        mock_run.return_value = _completed(2)
        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "exited with code 2" in result.output

    @patch(f"{_MOD}.subprocess.run", side_effect=OSError("cannot exec"))
    @patch(f"{_MOD}._receipt_path", return_value=None)
    @patch(f"{_MOD}.shutil.which", return_value="/usr/bin/uv")
    def test_oserror_is_wrapped(self, _which, _receipt, _run):
        result = CliRunner().invoke(update, [])

        assert result.exit_code != 0
        assert "Failed to run uv" in result.output
