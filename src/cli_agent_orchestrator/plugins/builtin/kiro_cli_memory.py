"""Kiro CLI memory-injection plugin (built-in).

On ``post_create_terminal`` for a ``kiro_cli`` provider, writes the CAO
memory context to ``<cwd>/.kiro/steering/cao-memory.md``. Kiro CLI natively
loads every ``*.md`` file under ``.kiro/steering/``, so this file is picked
up automatically. CAO owns this file and replaces it as a whole on each run.

The target path is fixed *per working directory*
(``<cwd>/.kiro/steering/cao-memory.md``), so its whole-file update uses
``locked_atomic_rewrite``. This serializes concurrent writes and publishes
through a unique temp file.

Observer-only: runs after terminal creation, logs-and-skips on every
error path rather than crashing ``cao-server``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from cli_agent_orchestrator.plugins import PostCreateTerminalEvent, hook
from cli_agent_orchestrator.plugins.base import CaoPlugin
from cli_agent_orchestrator.services.memory_injection import (
    empty_memory_block,
    memory_blocks_have_same_body,
    stamp_memory_block,
)
from cli_agent_orchestrator.utils.atomic_file import (
    locked_atomic_rewrite,
    locked_atomic_rewrite_or_delete,
)

if TYPE_CHECKING:
    from cli_agent_orchestrator.backends.base import TerminalBackend
    from cli_agent_orchestrator.services.memory_service import MemoryService as _MemoryService

logger = logging.getLogger(__name__)

STEERING_SUBDIR = ".kiro/steering"
MEMORY_FILENAME = "cao-memory.md"
_CAO_GENERATED_MEMORY_FILE = re.compile(
    r'^\s*<cao-memory generated="\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z">'
)


def get_backend() -> "TerminalBackend":
    """Resolve the backend only on the server-side handler path."""
    from cli_agent_orchestrator.backends.registry import get_backend as _get_backend

    return _get_backend()


def remote_memory_url() -> str | None:
    """Resolve the memory gateway only on the server-side handler path."""
    from cli_agent_orchestrator.services.memory_gateway import (
        remote_memory_url as _remote_memory_url,
    )

    return _remote_memory_url()


def memory_context_for_terminal(terminal_id: str, task_description: str = "") -> str:
    """Delegate to the memory gateway without importing it during discovery."""
    from cli_agent_orchestrator.services.memory_gateway import (
        memory_context_for_terminal as _memory_context_for_terminal,
    )

    return _memory_context_for_terminal(terminal_id, task_description)


def MemoryService() -> "_MemoryService":
    """Construct the local service without importing it during discovery."""
    from cli_agent_orchestrator.services.memory_service import MemoryService as _MemoryService

    return _MemoryService()


def memory_is_enabled() -> bool:
    """Resolve the operator's memory switch only on the handler path."""
    from cli_agent_orchestrator.services.settings_service import is_memory_enabled

    return is_memory_enabled()


def memory_injection_clear_on_empty_enabled() -> bool:
    """Resolve the default-on stale-block fix only on the handler path."""
    from cli_agent_orchestrator.services.settings_service import (
        is_memory_injection_clear_on_empty_enabled,
    )

    return is_memory_injection_clear_on_empty_enabled()


def memory_injection_write_project_files_enabled() -> bool:
    """Resolve the opt-in project-file writer only on the handler path."""
    from cli_agent_orchestrator.services.settings_service import (
        is_memory_injection_write_project_files_enabled,
    )

    return is_memory_injection_write_project_files_enabled()


def get_terminal_metadata(terminal_id: str):
    """Lazy indirection onto ``clients.database.get_terminal_metadata``.

    Deliberately a wrapper rather than a module-level import. These plugins are
    discovered through the ``cao.plugins`` entry points, and that discovery also
    runs in the AGENT's MCP process (``register_mcp_server_surfaces`` is called at
    MCP server import). A module-level import therefore ran ``clients.database``'s
    import-time ``_ensure_db_dir()`` -> ``DB_DIR.mkdir()`` inside every agent
    process: a filesystem side effect on agent startup that also fails outright
    wherever the data dir is unreadable. This handler only ever fires
    server-side, so resolving the symbol on call costs nothing.
    """
    from cli_agent_orchestrator.clients.database import (
        get_terminal_metadata as _get_terminal_metadata,
    )

    return _get_terminal_metadata(terminal_id)


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
                "kiro_cli_memory: could not resolve working dir for %s: %s",
                event.terminal_id,
                exc,
            )
            return

        if not working_directory:
            logger.debug(
                "kiro_cli_memory: no working directory for %s; skipping",
                event.terminal_id,
            )
            return

        if not memory_injection_write_project_files_enabled():
            await self._remove_project_file_memory(working_directory)
            return

        if not memory_is_enabled():
            return

        try:
            if remote_memory_url():
                context_block = await asyncio.to_thread(
                    memory_context_for_terminal,
                    event.terminal_id,
                )
            else:
                context_block = MemoryService().get_memory_context_for_terminal(event.terminal_id)
        except Exception as exc:
            logger.warning(
                "kiro_cli_memory: memory fetch failed for %s: %s",
                event.terminal_id,
                exc,
            )
            return

        if context_block:
            context_block = stamp_memory_block(context_block)
        elif memory_injection_clear_on_empty_enabled():
            context_block = empty_memory_block()
        else:
            logger.debug(
                "kiro_cli_memory: no memory context for %s; skipping write",
                event.terminal_id,
            )
            return

        try:
            target = self._validated_target_path(working_directory)
        except ValueError as exc:
            logger.warning(
                "kiro_cli_memory: path validation rejected %s: %s",
                working_directory,
                exc,
            )
            return

        try:
            try:
                existing = await asyncio.to_thread(target.read_text, encoding="utf-8")
            except FileNotFoundError:
                existing = ""
            if memory_blocks_have_same_body(existing, context_block):
                return
            await asyncio.to_thread(
                locked_atomic_rewrite,
                target,
                lambda _existing: context_block + "\n",
            )
        except Exception as exc:
            logger.warning(
                "kiro_cli_memory: write failed for %s: %s",
                target,
                exc,
            )

    async def _remove_project_file_memory(self, working_directory: str) -> None:
        """Delete a recognizable CAO-owned steering file without fetching memory."""
        try:
            target = self._validated_target_path(working_directory)
            await asyncio.to_thread(self._remove_owned_file, target)
        except ValueError as exc:
            logger.warning(
                "kiro_cli_memory: path validation rejected %s: %s",
                working_directory,
                exc,
            )
        except Exception as exc:
            logger.warning("kiro_cli_memory: cleanup failed for %s: %s", working_directory, exc)

    # ------------------------------------------------------------------
    # helpers

    def _resolve_working_directory(self, event: PostCreateTerminalEvent) -> str | None:
        """Look up the pane's working directory for the terminal via backend."""

        metadata = get_terminal_metadata(event.terminal_id)
        if metadata is None:
            return None

        session_name = metadata.get("tmux_session") or event.session_id
        window_name = metadata.get("tmux_window")
        if not session_name or not window_name:
            return None

        return get_backend().get_pane_working_directory(session_name, window_name)

    def _validated_target_path(self, working_directory: str) -> Path:
        """Return <cwd>/.kiro/steering/cao-memory.md, rejecting escape attempts.

        Uses realpath for both the base and the target so symlink trickery
        cannot redirect the write outside the working directory.
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
        target = (base / STEERING_SUBDIR / MEMORY_FILENAME).resolve()
        # relative_to() correctly handles the root-path case (base == "/"),
        # which a string startswith(base + separator) check mishandles ("//").
        try:
            target.relative_to(base)
        except ValueError:
            raise ValueError(f"target {target} escapes working directory {base}")
        return target

    @staticmethod
    def _remove_owned_file(target: Path) -> None:
        def remove_if_owned(content: str) -> str:
            return "" if _CAO_GENERATED_MEMORY_FILE.match(content) else content

        locked_atomic_rewrite_or_delete(
            target,
            remove_if_owned,
            lambda content: not content.strip(),
        )
