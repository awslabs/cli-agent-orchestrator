"""Tests for the install CLI command wrapper."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import (
    _copy_local_profile_to_store,
    install,
)
from cli_agent_orchestrator.services.install_service import InstallResult


class TestInstallCommand:
    """Tests for the thin CLI wrapper around install_service.install_agent."""

    @pytest.fixture
    def runner(self) -> CliRunner:
        """Create a CLI test runner."""
        return CliRunner()

    def test_install_help_describes_env_workflow(self, runner: CliRunner) -> None:
        """Help text should describe env file storage, ${VAR} syntax, and an example."""
        result = runner.invoke(install, ["--help"])

        assert result.exit_code == 0
        assert "~/.aws/cli-agent-orchestrator/.env" in result.output
        assert "${VAR}" in result.output
        assert "API_TOKEN=my-secret-token" in result.output

    def test_install_success_outputs_result_details(self, runner: CliRunner) -> None:
        """Successful installs should print the same user-facing summary lines."""
        service_result = InstallResult(
            success=True,
            message="Agent 'developer' installed successfully",
            agent_name="developer",
            context_file="/tmp/agent-context/developer.md",
            agent_file="/tmp/kiro/developer.json",
            unresolved_vars=["BASE_URL"],
            source_kind="name",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=service_result,
        ) as mock_install:
            result = runner.invoke(
                install,
                [
                    "developer",
                    "--provider",
                    "kiro_cli",
                    "--env",
                    "API_TOKEN=secret-token",
                ],
            )

        assert result.exit_code == 0
        assert "Agent 'developer' installed successfully" in result.output
        assert "Set 1 env var(s)" in result.output
        assert "Unresolved env var(s) in profile: BASE_URL" in result.output
        assert "cao env set" in result.output
        assert "Context file: /tmp/agent-context/developer.md" in result.output
        assert "kiro_cli agent: /tmp/kiro/developer.json" in result.output
        mock_install.assert_called_once_with(
            "developer",
            "kiro_cli",
            {"API_TOKEN": "secret-token"},
        )

    def test_install_url_source_prints_download_confirmation(self, runner: CliRunner) -> None:
        """URL installs should print a download confirmation line."""
        service_result = InstallResult(
            success=True,
            message="Agent 'remote' installed successfully",
            agent_name="remote",
            source_kind="url",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=service_result,
        ):
            result = runner.invoke(
                install,
                ["https://example.com/remote.md", "--provider", "kiro_cli"],
            )

        assert result.exit_code == 0
        assert "Downloaded agent from URL to local store" in result.output
        assert "Agent 'remote' installed successfully" in result.output

    def test_install_file_source_prints_copy_confirmation(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """File path installs should copy to the store and print a copy confirmation.

        File-handling lives entirely in the CLI: the service layer only sees
        the validated bare stem. We verify the copy happened and the service
        was called with the stem, not the original path.
        """
        local_store = tmp_path / "agent-store"
        local_store.mkdir()
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR",
            local_store,
        )
        source_profile = tmp_path / "local.md"
        source_profile.write_text("---\nname: local\ndescription: Test\n---\nBody\n")

        service_result = InstallResult(
            success=True,
            message="Agent 'local' installed successfully",
            agent_name="local",
            source_kind="name",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=service_result,
        ) as mock_install:
            result = runner.invoke(
                install,
                [str(source_profile), "--provider", "kiro_cli"],
            )

        assert result.exit_code == 0, result.output
        assert "Copied agent from file to local store" in result.output
        assert "Agent 'local' installed successfully" in result.output
        # Service sees the validated stem, never the full user path.
        mock_install.assert_called_once_with("local", "kiro_cli", None)
        assert (local_store / "local.md").read_text() == source_profile.read_text()

    def test_install_file_source_missing_file_fails_fast(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A `.md`-suffixed path that doesn't exist should fail before the service call."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR",
            tmp_path / "agent-store",
        )

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
        ) as mock_install:
            result = runner.invoke(install, [str(tmp_path / "missing.md")])

        assert result.exit_code != 0
        assert "File not found" in result.output
        mock_install.assert_not_called()

    def test_install_file_source_rejects_unsafe_stem(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A local .md file whose stem contains unsafe characters must be refused."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR",
            tmp_path / "agent-store",
        )
        bad = tmp_path / "evil space.md"
        bad.write_text("---\nname: evil\ndescription: x\n---\nBody\n")

        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
        ) as mock_install:
            result = runner.invoke(install, [str(bad)])

        assert result.exit_code != 0
        assert "must match" in result.output
        mock_install.assert_not_called()


    def test_install_failure_prints_error(self, runner: CliRunner) -> None:
        """Service failures should be surfaced as CLI errors without raising."""
        with patch(
            "cli_agent_orchestrator.cli.commands.install.install_agent",
            return_value=InstallResult(success=False, message="Source not found: missing"),
        ):
            result = runner.invoke(install, ["missing"])

        assert result.exit_code == 0
        assert "Error: Source not found: missing" in result.output

    def test_install_invalid_env_format_returns_click_error(self, runner: CliRunner) -> None:
        """Assignments without '=' should fail validation with a user-friendly error."""
        result = runner.invoke(install, ["developer", "--env", "INVALID_FORMAT"])

        assert result.exit_code == 2
        assert "Invalid value for --env" in result.output
        assert "Expected format KEY=VALUE" in result.output

    def test_install_empty_env_key_returns_click_error(self, runner: CliRunner) -> None:
        """Assignments with an empty key should fail validation."""
        result = runner.invoke(install, ["developer", "--env", "=value"])

        assert result.exit_code == 2
        assert "Invalid value for --env" in result.output
        assert "Key must not be empty" in result.output


class TestCopyLocalProfileToStore:
    """Tests for the file-handling helper that lives only in the CLI layer.

    This helper is the reason ``install_service.install_agent`` can keep a
    narrow, bare-name-or-URL contract: the CLI copies user files into the
    local store itself and then forwards just the validated stem.
    """

    def test_returns_none_for_url_source(self) -> None:
        assert _copy_local_profile_to_store("https://example.com/a.md") is None
        assert _copy_local_profile_to_store("http://example.com/a.md") is None

    def test_returns_none_for_bare_name(self) -> None:
        assert _copy_local_profile_to_store("developer") is None

    def test_copies_file_and_returns_stem(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = tmp_path / "store"
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.install.LOCAL_AGENT_STORE_DIR", store
        )
        src = tmp_path / "my-agent.md"
        src.write_text("body", encoding="utf-8")

        stem = _copy_local_profile_to_store(str(src))

        assert stem == "my-agent"
        assert (store / "my-agent.md").read_text(encoding="utf-8") == "body"
