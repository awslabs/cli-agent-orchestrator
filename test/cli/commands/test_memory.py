"""Tests for CLI memory commands (U8.3).

CLI command tests mock MemoryService to isolate command logic.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import (
    clear,
    delete,
    heal_cmd,
    lint_cmd,
    list_memories,
    show,
)
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


class TestMemoryLint:
    """cao memory lint — JSON output must be a clean, parseable stream."""

    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_lint_json_stdout_is_pure_json(self, mock_get_svc, mock_run_lint):
        """The completion line must not pollute stdout under --format json."""
        import json

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = []  # no issues

        runner = CliRunner()
        result = runner.invoke(lint_cmd, ["--format", "json"])

        assert result.exit_code == 0
        # stdout parses cleanly; the "lint_run_completed" line went to stderr.
        assert json.loads(result.stdout) == []
        assert "lint_run_completed" not in result.stdout
        assert "lint_run_completed" in result.stderr

    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_completion_summary_only_run_reports_no_issues(self, mock_get_svc, mock_run_lint):
        """A run whose only row is the completion summary must hit the
        'No lint issues found.' branch, not render the summary as a finding.
        """
        from cli_agent_orchestrator.services.wiki_lint import LintIssue

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = [
            LintIssue(
                issue_type="lint_error",
                key="run_lint",
                description="lint_run_completed: 5/5",
                severity="warning",
            )
        ]

        runner = CliRunner()
        result = runner.invoke(lint_cmd, [])

        assert result.exit_code == 0
        assert "No lint issues found." in result.stdout
        # The summary is echoed once (top line) but not duplicated as a row.
        assert result.stdout.count("lint_run_completed") <= 1

    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_completion_summary_excluded_from_json_payload(self, mock_get_svc, mock_run_lint):
        """Under --format json the completion summary is not a payload row."""
        import json

        from cli_agent_orchestrator.services.wiki_lint import LintIssue

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = [
            LintIssue(
                issue_type="orphan_page",
                key="lonely",
                description="orphan",
                severity="warning",
            ),
            LintIssue(
                issue_type="lint_error",
                key="run_lint",
                description="lint_run_completed: 5/5",
                severity="warning",
            ),
        ]

        runner = CliRunner()
        result = runner.invoke(lint_cmd, ["--format", "json"])

        payload = json.loads(result.stdout)
        assert len(payload) == 1
        assert payload[0]["key"] == "lonely"
        assert not any("lint_run_completed" in p["description"] for p in payload)


class TestMemoryHeal:
    """cao memory heal — dry-run default, --apply gate, poison dual-gate."""

    @patch("cli_agent_orchestrator.services.wiki_healer.heal", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_dry_run_default(self, mock_get_svc, mock_run_lint, mock_heal):
        from cli_agent_orchestrator.services.wiki_healer import HealAction, HealReport

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = []
        mock_heal.return_value = HealReport(
            scope="project",
            scope_id="proj-abc",
            apply=False,
            aggressive=False,
            actions=[
                HealAction(
                    "orphan_pruned", "lonely", description="Would delete orphan", status="planned"
                )
            ],
            dry_run_summary="Would apply 1 of 1 actionable findings (0 suppressed by caps).",
        )

        runner = CliRunner()
        result = runner.invoke(heal_cmd, ["--scope", "project"])

        assert result.exit_code == 0
        # heal() was called with apply=False by default.
        _, kwargs = mock_heal.call_args
        assert kwargs["apply"] is False
        assert kwargs["aggressive"] is False
        assert "Would apply 1" in result.stdout
        assert "planned" in result.stdout

    @patch("cli_agent_orchestrator.services.wiki_healer.heal", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_apply_flag_passed_through(self, mock_get_svc, mock_run_lint, mock_heal):
        from cli_agent_orchestrator.services.wiki_healer import HealAction, HealReport

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = []
        mock_heal.return_value = HealReport(
            scope="project",
            scope_id="proj-abc",
            apply=True,
            aggressive=False,
            actions=[
                HealAction("orphan_pruned", "lonely", description="deleted", status="applied")
            ],
        )

        runner = CliRunner()
        result = runner.invoke(heal_cmd, ["--scope", "project", "--apply"])

        assert result.exit_code == 0
        _, kwargs = mock_heal.call_args
        assert kwargs["apply"] is True
        assert "applied" in result.stdout

    @patch("cli_agent_orchestrator.services.wiki_healer.heal", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_aggressive_flag_passed_through(self, mock_get_svc, mock_run_lint, mock_heal):
        from cli_agent_orchestrator.services.wiki_healer import HealReport

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = []
        mock_heal.return_value = HealReport(
            scope="project", scope_id="proj-abc", apply=True, aggressive=True, actions=[]
        )

        runner = CliRunner()
        result = runner.invoke(heal_cmd, ["--scope", "project", "--apply", "--aggressive"])

        assert result.exit_code == 0
        _, kwargs = mock_heal.call_args
        assert kwargs["apply"] is True
        assert kwargs["aggressive"] is True
        assert "Nothing to heal." in result.stdout

    @patch("cli_agent_orchestrator.services.wiki_healer.heal", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_issue_type_filter(self, mock_get_svc, mock_run_lint, mock_heal):
        from cli_agent_orchestrator.services.wiki_healer import HealReport
        from cli_agent_orchestrator.services.wiki_lint import LintIssue

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = [
            LintIssue(issue_type="orphan_page", key="a"),
            LintIssue(issue_type="stale_claim", key="b", description="file not found: x.py"),
        ]
        mock_heal.return_value = HealReport(
            scope="project", scope_id="proj-abc", apply=False, aggressive=False, actions=[]
        )

        runner = CliRunner()
        result = runner.invoke(heal_cmd, ["--scope", "project", "--issue-type", "orphan_page"])

        assert result.exit_code == 0
        passed_issues = mock_heal.call_args.args[0]
        assert all(i.issue_type == "orphan_page" for i in passed_issues)
        assert len(passed_issues) == 1

    @patch("cli_agent_orchestrator.services.wiki_healer.heal", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_json_output(self, mock_get_svc, mock_run_lint, mock_heal):
        import json

        from cli_agent_orchestrator.services.wiki_healer import HealAction, HealReport

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = []
        mock_heal.return_value = HealReport(
            scope="project",
            scope_id="proj-abc",
            apply=True,
            aggressive=False,
            actions=[
                HealAction("orphan_pruned", "lonely", description="deleted", status="applied")
            ],
            total_suppressed=0,
        )

        runner = CliRunner()
        result = runner.invoke(heal_cmd, ["--scope", "project", "--apply", "--format", "json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["apply"] is True
        assert payload["actions"][0]["key"] == "lonely"

    @patch("cli_agent_orchestrator.services.wiki_healer.heal", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.services.wiki_lint.run_lint", new_callable=AsyncMock)
    @patch("cli_agent_orchestrator.cli.commands.memory._get_memory_service")
    def test_lock_conflict_surfaces_clean_error(self, mock_get_svc, mock_run_lint, mock_heal):
        from cli_agent_orchestrator.services.wiki_healer import HealConflictError

        mock_svc = MagicMock()
        mock_svc.resolve_scope_id = MagicMock(return_value="proj-abc")
        mock_get_svc.return_value = mock_svc
        mock_run_lint.return_value = []
        mock_heal.side_effect = HealConflictError("another heal is running")

        runner = CliRunner()
        result = runner.invoke(heal_cmd, ["--scope", "project", "--apply"])

        assert result.exit_code != 0
        assert "another heal is running" in result.output
