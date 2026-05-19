"""Tests for CLI memory commands (U8.3).

CLI command tests mock MemoryService to isolate command logic.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import clear, delete, list_memories, show
from cli_agent_orchestrator.models.memory import Memory

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_memory(
    key: str = "test-key",
    content: str = "test content",
    scope: str = "project",
    memory_type: str = "project",
    tags: str = "",
    file_path: str = "/tmp/wiki/test.md",
) -> Memory:
    """Create a Memory instance for testing."""
    now = datetime.now(timezone.utc)
    return Memory(
        id="test-id",
        key=key,
        memory_type=memory_type,
        scope=scope,
        scope_id=None,
        file_path=file_path,
        tags=tags,
        source_provider=None,
        source_terminal_id=None,
        created_at=now,
        updated_at=now,
        content=content,
    )


# ===========================================================================
# U8.3 — CLI Command Tests
# ===========================================================================


class TestMemoryList:
    """U8.3: cao memory list — runs and formats output."""

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_list_shows_memories(self, mock_get_svc):
        """list command should display memories in table format."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(
            return_value=[
                _make_memory(
                    key="pref-pytest", scope="global", memory_type="feedback", tags="testing"
                ),
                _make_memory(key="deploy-cfg", scope="project", memory_type="reference", tags="ci"),
            ]
        )
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(list_memories)

        assert result.exit_code == 0
        assert "pref-pytest" in result.output
        assert "deploy-cfg" in result.output
        assert "KEY" in result.output  # header

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_list_empty(self, mock_get_svc):
        """list command should handle no memories gracefully."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(return_value=[])
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(list_memories)

        assert result.exit_code == 0
        assert "No memories found" in result.output

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_list_with_scope_filter(self, mock_get_svc):
        """list command should pass scope filter to recall."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(return_value=[])
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(list_memories, ["--scope", "global"])

        assert result.exit_code == 0
        mock_svc.recall.assert_called_once()
        call_kwargs = mock_svc.recall.call_args
        assert (
            call_kwargs[1].get("scope") == "global" or call_kwargs.kwargs.get("scope") == "global"
        )

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_list_all_flag(self, mock_get_svc):
        """list --all should pass scan_all=True to recall."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(return_value=[])
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(list_memories, ["--all"])

        assert result.exit_code == 0
        mock_svc.recall.assert_called_once()
        call_kwargs = mock_svc.recall.call_args
        assert call_kwargs.kwargs.get("scan_all") is True

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_list_default_passes_cwd(self, mock_get_svc):
        """list command should pass terminal_context with cwd by default."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(return_value=[])
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(list_memories)

        assert result.exit_code == 0
        call_kwargs = mock_svc.recall.call_args
        ctx = call_kwargs.kwargs.get("terminal_context")
        assert ctx is not None
        assert "cwd" in ctx


class TestMemoryShow:
    """U8.3: cao memory show — finds key and displays content."""

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_show_displays_content(self, mock_get_svc):
        """show command should display full memory content."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(
            return_value=[
                _make_memory(
                    key="my-key", content="Detailed memory content here", tags="tag1,tag2"
                ),
            ]
        )
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(show, ["my-key"])

        assert result.exit_code == 0
        assert "my-key" in result.output
        assert "Detailed memory content here" in result.output
        # Verify CWD context and scan_all are passed so project memories are found
        call_kwargs = mock_svc.recall.call_args.kwargs
        assert call_kwargs.get("scan_all") is True
        assert "cwd" in (call_kwargs.get("terminal_context") or {})

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_show_not_found(self, mock_get_svc):
        """show command should error when key not found."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(return_value=[])
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(show, ["nonexistent"])

        assert result.exit_code != 0
        assert "not found" in result.output


class TestMemoryDeleteWithConfirmation:
    """U8.3: cao memory delete — prompts and deletes."""

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_delete_with_confirmation(self, mock_get_svc):
        """delete command should prompt for confirmation and delete."""
        mock_svc = MagicMock()
        mock_svc.forget = AsyncMock(return_value=True)
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(delete, ["my-key", "--yes"])

        assert result.exit_code == 0
        assert "Deleted" in result.output
        mock_svc.forget.assert_called_once()
        # Verify CWD context is passed so project memories are resolved correctly
        call_kwargs = mock_svc.forget.call_args.kwargs
        assert "cwd" in (call_kwargs.get("terminal_context") or {})

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_delete_prompts_user(self, mock_get_svc):
        """delete command without --yes should prompt for confirmation."""
        mock_svc = MagicMock()
        mock_svc.forget = AsyncMock(return_value=True)
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(delete, ["my-key"], input="y\n")

        assert result.exit_code == 0
        assert "Deleted" in result.output

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_delete_not_found(self, mock_get_svc):
        """delete command should error when key not found."""
        mock_svc = MagicMock()
        mock_svc.forget = AsyncMock(return_value=False)
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(delete, ["nonexistent", "--yes"])

        assert result.exit_code != 0
        assert "not found" in result.output


class TestMemoryKeyValidation:
    """Defense-in-depth: reject keys with path traversal or invalid characters."""

    def test_show_rejects_path_traversal(self):
        """show command should reject keys containing path separators."""
        runner = CliRunner()
        result = runner.invoke(show, ["../../etc/passwd"])

        assert result.exit_code != 0
        assert "Invalid key" in result.output

    def test_delete_rejects_path_traversal(self):
        """delete command should reject keys containing path separators."""
        runner = CliRunner()
        result = runner.invoke(delete, ["../secrets", "--yes"])

        assert result.exit_code != 0
        assert "Invalid key" in result.output

    def test_show_rejects_uppercase(self):
        """show command should reject keys with uppercase characters."""
        runner = CliRunner()
        result = runner.invoke(show, ["My-Key"])

        assert result.exit_code != 0
        assert "Invalid key" in result.output

    def test_show_rejects_underscores(self):
        """show command should reject keys with underscores."""
        runner = CliRunner()
        result = runner.invoke(show, ["my_key"])

        assert result.exit_code != 0
        assert "Invalid key" in result.output

    def test_show_accepts_valid_key(self):
        """show command should accept lowercase alphanumeric keys with hyphens."""
        runner = CliRunner()
        result = runner.invoke(show, ["valid-key-123"])

        # Key validation passes; error is "not found", not "Invalid key"
        assert "Invalid key" not in result.output


class TestMemoryClearRequiresScope:
    """U8.3: cao memory clear errors without --scope."""

    def test_memory_clear_requires_scope(self):
        """clear command without --scope should fail."""
        runner = CliRunner()
        result = runner.invoke(clear, [])

        assert result.exit_code != 0

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_clear_with_scope(self, mock_get_svc):
        """clear command with --scope should clear all memories in that scope."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(
            return_value=[
                _make_memory(key="m1", scope="session"),
                _make_memory(key="m2", scope="session"),
            ]
        )
        mock_svc.forget = AsyncMock(return_value=True)
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(clear, ["--scope", "session", "--yes"])

        assert result.exit_code == 0
        assert "Cleared" in result.output
        assert mock_svc.forget.call_count == 2

    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_memory_clear_empty_scope(self, mock_get_svc):
        """clear command on empty scope should report no memories to clear."""
        mock_svc = MagicMock()
        mock_svc.recall = AsyncMock(return_value=[])
        mock_get_svc.return_value = mock_svc

        runner = CliRunner()
        result = runner.invoke(clear, ["--scope", "session", "--yes"])

        assert result.exit_code == 0
        assert "No session-scoped memories to clear" in result.output
