"""Claude Code memory-injection plugin (built-in).

Writes the CAO memory context block into ``.claude/CLAUDE.md`` of the
terminal's working directory on ``post_create_terminal``. Replaces the
provider-side Stop/PreCompact hooks that used to shoulder this job.

Observer-only: the plugin runs *after* the terminal is created, so any
failure is logged and the terminal continues without memory context
rather than crashing ``cao-server``.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.plugins import PostCreateTerminalEvent, hook
from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.services.memory_service import MemoryService

logger = logging.getLogger(__name__)

# Delimited section so repeated runs overwrite the same block rather than
# appending forever. Readers of CLAUDE.md can also treat the delimiters as
# a well-known injection boundary.
BEGIN_MARKER = "<!-- cao-memory:begin -->"
END_MARKER = "<!-- cao-memory:end -->"
CLAUDE_FILENAME = "CLAUDE.md"
CLAUDE_DIR = ".claude"


class ClaudeCodeMemoryPlugin(CaoPlugin):
    """Inject CAO memory into the per-project CLAUDE.md on terminal creation."""

    async def setup(self) -> None:
        """Nothing to configure; plugin is stateless."""

    async def teardown(self) -> None:
        """Nothing to close; plugin holds no resources."""

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Write the <cao-memory> block into <cwd>/.claude/CLAUDE.md."""

        if event.provider != "claude_code":
            return

        try:
            working_directory = self._resolve_working_directory(event)
        except Exception as exc:
            logger.warning(
                "claude_code_memory: could not resolve working dir for %s: %s",
                event.terminal_id,
                exc,
            )
            return

        if not working_directory:
            logger.debug(
                "claude_code_memory: no working directory for %s; skipping",
                event.terminal_id,
            )
            return

        try:
            context_block = MemoryService().get_memory_context_for_terminal(event.terminal_id)
        except Exception as exc:
            logger.warning(
                "claude_code_memory: memory fetch failed for %s: %s",
                event.terminal_id,
                exc,
            )
            return

        if not context_block:
            logger.debug(
                "claude_code_memory: no memory context for %s; skipping write",
                event.terminal_id,
            )
            return

        try:
            target = self._validated_target_path(working_directory)
        except ValueError as exc:
            logger.warning(
                "claude_code_memory: path validation rejected %s: %s",
                working_directory,
                exc,
            )
            return

        try:
            self._write_block(target, context_block)
        except Exception as exc:
            logger.warning(
                "claude_code_memory: write failed for %s: %s",
                target,
                exc,
            )

    # ------------------------------------------------------------------
    # helpers

    def _resolve_working_directory(self, event: PostCreateTerminalEvent) -> str | None:
        """Look up the tmux pane's working directory for the terminal."""

        metadata = get_terminal_metadata(event.terminal_id)
        if metadata is None:
            return None

        session_name = metadata.get("tmux_session") or event.session_id
        window_name = metadata.get("tmux_window")
        if not session_name or not window_name:
            return None

        return tmux_client.get_pane_working_directory(session_name, window_name)

    def _validated_target_path(self, working_directory: str) -> Path:
        """Return <cwd>/.claude/CLAUDE.md, rejecting paths that escape the cwd.

        Uses realpath for both the base and the final target so symlink
        trickery cannot redirect the write outside the working directory.
        """

        base = Path(working_directory).resolve(strict=True)
        target = (base / CLAUDE_DIR / CLAUDE_FILENAME).resolve()
        base_str = str(base)
        target_str = str(target)
        if target_str != base_str and not target_str.startswith(base_str + os.sep):
            raise ValueError(f"target {target_str} escapes working directory {base_str}")
        return target

    def _write_block(self, target: Path, context_block: str) -> None:
        """Write or replace the delimited memory section in CLAUDE.md."""

        target.parent.mkdir(parents=True, exist_ok=True)

        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        stripped = self._strip_existing_block(existing)

        separator = "" if not stripped or stripped.endswith("\n") else "\n"
        new_content = f"{stripped}{separator}{BEGIN_MARKER}\n{context_block}\n{END_MARKER}\n"
        target.write_text(new_content, encoding="utf-8")

    @staticmethod
    def _strip_existing_block(content: str) -> str:
        """Remove any prior cao-memory block so we replace rather than append."""

        if BEGIN_MARKER not in content or END_MARKER not in content:
            return content
        before, _, rest = content.partition(BEGIN_MARKER)
        _, _, after = rest.partition(END_MARKER)
        before = before.rstrip("\n")
        after = after.lstrip("\n")
        if before and after:
            return f"{before}\n{after}"
        return before or after
