"""Claude Code memory-injection plugin (built-in).

On ``post_create_terminal`` for a ``claude_code`` provider, writes the CAO
memory context block into ``<cwd>/.claude/CLAUDE.md``, replacing any prior
block delimited by the cao-memory markers.

Observer-only: the plugin runs *after* the terminal is created, so any
failure is logged and the terminal continues without memory context
rather than crashing ``cao-server``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.plugins import PostCreateTerminalEvent, hook
from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.utils.atomic_file import locked_atomic_rewrite

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
            # locked_atomic_rewrite polls with time.sleep up to the lock
            # timeout; run it off the event loop so a contended lock cannot
            # stall cao-server for other terminals.
            await asyncio.to_thread(self._write_block, target, context_block)
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

        if "\x00" in working_directory:
            raise ValueError("working directory contains null bytes")

        # resolve(strict=True) raises OSError (e.g. FileNotFoundError) for an
        # ephemeral/missing cwd. Surface it as ValueError so the caller's
        # single ``except ValueError`` reliably catches every validation
        # failure and honours the plugin's log-and-skip contract.
        try:
            base = Path(working_directory).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"working directory {working_directory!r} is not resolvable: {exc}")
        target = (base / CLAUDE_DIR / CLAUDE_FILENAME).resolve()
        # relative_to() correctly handles the root-path case (base == "/"),
        # which a string startswith(base + separator) check mishandles ("//").
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(f"target {target} escapes working directory {base}")
        return target

    def _write_block(self, target: Path, context_block: str) -> None:
        """Write or replace the delimited memory section in CLAUDE.md.

        Concurrent writers (multiple terminals, or the CLI and cao-server
        touching the same working directory) must never lose each other's
        memory block. ``locked_atomic_rewrite`` holds an inter-process lock
        for the whole read-strip-append-replace cycle so
        ``compute_new_content`` always sees the latest published content.
        """

        def compute_new_content(existing: str) -> str:
            stripped = self._strip_existing_block(existing)
            separator = "" if not stripped or stripped.endswith("\n") else "\n"
            return f"{stripped}{separator}{BEGIN_MARKER}\n{context_block}\n{END_MARKER}\n"

        locked_atomic_rewrite(target, compute_new_content)

    @staticmethod
    def _strip_existing_block(content: str) -> str:
        """Remove any prior cao-memory block so we replace rather than append.

        Each BEGIN is paired with the END that follows it. A stray BEGIN with
        no following END (or with another BEGIN before its END) is treated as
        corruption: only the marker token is removed, never the user content
        around it. This stops a stale unclosed BEGIN from later pairing with an
        unrelated block's END and deleting everything in between.
        """

        while True:
            begin = content.find(BEGIN_MARKER)
            if begin == -1:
                break
            end = content.find(END_MARKER, begin + len(BEGIN_MARKER))
            next_begin = content.find(BEGIN_MARKER, begin + len(BEGIN_MARKER))
            if end == -1 or (next_begin != -1 and next_begin < end):
                # Stray/unclosed BEGIN: drop only the marker, keep all content.
                content = content[:begin] + content[begin + len(BEGIN_MARKER) :]
                continue
            before = content[:begin].rstrip("\n")
            after = content[end + len(END_MARKER) :].lstrip("\n")
            if before and after:
                content = f"{before}\n{after}"
            else:
                content = before or after

        return content
