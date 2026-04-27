"""Kiro CLI memory-injection plugin.

Writes the CAO memory context to ``.kiro/steering/cao-memory.md`` in the
terminal's working directory on ``post_create_terminal``. Replaces the
``agentSpawn`` + ``userPromptSubmit`` Kiro hooks that used to shoulder
memory injection.

Coexists with the Phase 2 U7 ``agent-identity.md`` steering file
(written by ``terminal_service._write_kiro_steering_file``). Kiro CLI
reads every file in ``.kiro/steering/``, so the static identity and the
memory context stay in separate files that each have a single owner.

Observer-only: runs after terminal creation, logs-and-skips on every
error path rather than crashing ``cao-server``.
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

STEERING_SUBDIR = ".kiro/steering"
MEMORY_FILENAME = "cao-memory.md"


class KiroCliMemoryPlugin(CaoPlugin):
    """Inject CAO memory into the per-project Kiro steering directory."""

    async def setup(self) -> None:
        """Stateless; nothing to configure."""

    async def teardown(self) -> None:
        """Stateless; nothing to close."""

    @hook("post_create_terminal")
    async def on_post_create_terminal(self, event: PostCreateTerminalEvent) -> None:
        """Write <cwd>/.kiro/steering/cao-memory.md with the memory context."""

        if event.provider != "kiro_cli":
            return

        try:
            working_directory = self._resolve_working_directory(event)
        except Exception as exc:
            logger.warning(
                "cao-memory-kiro-cli: could not resolve working dir for %s: %s",
                event.terminal_id,
                exc,
            )
            return

        if not working_directory:
            logger.debug(
                "cao-memory-kiro-cli: no working directory for %s; skipping",
                event.terminal_id,
            )
            return

        try:
            context_block = MemoryService().get_memory_context_for_terminal(
                event.terminal_id
            )
        except Exception as exc:
            logger.warning(
                "cao-memory-kiro-cli: memory fetch failed for %s: %s",
                event.terminal_id,
                exc,
            )
            return

        if not context_block:
            logger.debug(
                "cao-memory-kiro-cli: no memory context for %s; skipping write",
                event.terminal_id,
            )
            return

        try:
            target = self._validated_target_path(working_directory)
        except ValueError as exc:
            logger.warning(
                "cao-memory-kiro-cli: path validation rejected %s: %s",
                working_directory,
                exc,
            )
            return

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(context_block + "\n", encoding="utf-8")
        except Exception as exc:
            logger.warning(
                "cao-memory-kiro-cli: write failed for %s: %s",
                target,
                exc,
            )

    # ------------------------------------------------------------------
    # helpers

    def _resolve_working_directory(
        self, event: PostCreateTerminalEvent
    ) -> str | None:
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
        """Return <cwd>/.kiro/steering/cao-memory.md, rejecting escape attempts.

        Uses realpath for both the base and the target so symlink trickery
        cannot redirect the write outside the working directory.
        """

        if "\x00" in working_directory:
            raise ValueError("working directory contains null bytes")

        base = Path(working_directory).resolve(strict=True)
        target = (base / STEERING_SUBDIR / MEMORY_FILENAME).resolve()
        base_str = str(base)
        target_str = str(target)
        if target_str != base_str and not target_str.startswith(base_str + os.sep):
            raise ValueError(
                f"target {target_str} escapes working directory {base_str}"
            )
        return target
