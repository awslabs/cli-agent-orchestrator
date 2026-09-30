"""Terminal service with workflow functions.

This module provides high-level terminal management operations that orchestrate
multiple components (database, tmux, providers) to create a unified terminal
abstraction for CLI agents.

Key Responsibilities:
- Terminal lifecycle management (create, get, delete)
- Provider initialization and cleanup
- Tmux session/window management
- Terminal output capture and message extraction

Terminal Workflow:
1. create_terminal() → Creates tmux window, initializes provider, starts logging
2. send_input() → Sends user message to the agent via tmux
3. get_output() → Retrieves agent response from terminal history
4. delete_terminal() → Cleans up provider, database record, and logging
"""

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests
from pydantic import ValidationError

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (  # Compatibility/test seam: creation no longer bulk-deletes with this helper,; but older integrations/tests patch the imported symbol while exercising; create_terminal. Keep the name exported from this module until that seam; can be retired separately.
    count_runtime_allocated_terminals,
    create_inbox_message,
)
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import (  # Compatibility/test seam: creation no longer bulk-deletes with this helper,; but older integrations/tests patch the imported symbol while exercising; create_terminal. Keep the name exported from this module until that seam; can be retired separately.
    delete_idempotency_key,
)
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (  # Compatibility/test seam: creation no longer bulk-deletes with this helper,; but older integrations/tests patch the imported symbol while exercising; create_terminal. Keep the name exported from this module until that seam; can be retired separately.
    delete_terminals_by_session,
    get_idempotency_record,
    get_session_incarnation,
    get_terminal_metadata,
    list_all_terminals,
    list_pending_deferred_init_external_owner_terminal_ids,
    list_siblings_by_group_prefix,
    list_terminals_by_session,
    update_last_active,
    update_terminal_deferred_init_external_owner,
    update_terminal_deferred_init_failure,
    update_terminal_deferred_init_runtime_reclaimed,
    update_terminal_group,
    update_terminal_metadata,
    update_terminal_provider_variant,
    update_terminal_shell_command,
    update_terminals_session_incarnation,
)
from cli_agent_orchestrator.constants import (
    CALLBACK_TERMINAL_ID_ENV,
    CALLBACK_URL_ENV,
    FIFO_DIR,
    PIPE_LIVENESS_TAIL_LINES,
    SESSION_PREFIX,
    TERMINAL_LOG_DIR,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import (
    Terminal,
    TerminalInputBlockedError,
    TerminalLimitError,
    TerminalStatus,
)
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateTerminalEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.providers.base import (
    BaseProvider,
    OutputExtractionError,
    OutputExtractionRejected,
)
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    KiroPhase0KASError,
    probe_kiro_capabilities,
    requested_kiro_capabilities,
)
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import worktree_service
from cli_agent_orchestrator.services.elastic_worker_gateway import (
    elastic_worker_gateway_headers,
)
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.herdr_inbox_registry import get_herdr_inbox_service
from cli_agent_orchestrator.services.install_service import (
    kiro_install_predates_native_enforcement,
)
from cli_agent_orchestrator.services.memory_gateway import (
    memory_context_for_terminal,
    remote_memory_url,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock
from cli_agent_orchestrator.services.settings_service import get_max_terminals
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.step_output_store import _validate_key_part
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.enforcement import NATIVE as NATIVE_ENFORCEMENT
from cli_agent_orchestrator.utils.enforcement import (
    PROVIDER_ENFORCEMENT,
    enforcement_for,
    native_providers,
)
from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
    wait_until_status,
)

logger = logging.getLogger(__name__)

_DEFERRED_INIT_FAILURE_MESSAGE_MAX = 4096
_DEFERRED_INIT_FAILURE_SCAN_MAX = 16384
_DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS = 3
_DEFERRED_INIT_FAILURE_FALLBACK_SUFFIX = ".deferred-init-failure.json"
_DEFERRED_INIT_COMPLETE_FALLBACK_SUFFIX = ".deferred-init-complete"
_DEFERRED_INIT_SIDECAR_LOCK = threading.RLock()


class IdempotencyKeyConflict(Exception):
    """An idempotency key was reused for a DIFFERENT request.

    Review on PR #634, issue #616. Surfaced as HTTP 409 by both create
    endpoints -- the same shape Stripe and AWS use
    (``IdempotentParameterMismatch``) rather than silently serving the first
    call's result to a caller who asked for something else.

    Subclasses ``Exception`` and NOT ``ValueError``, deliberately. Every
    ``ValueError`` out of ``create_terminal`` is already spoken for by the
    endpoints: ``create_session`` maps it to 400 and
    ``create_terminal_in_session`` maps it to 404, and in ``create_session``
    that arm sits FIRST -- so a ``ValueError`` subclass would be swallowed
    into a 400 before any 409 arm could see it, and a later reorder could
    silently re-break it. As a plain ``Exception`` the only ordering
    requirement is that its arm precede the catch-all 500, which no reorder
    of the ``ValueError``-family arms can violate. This also matches the
    dominant convention in this repo (``WorktreeError(Exception)``,
    ``ProviderError(Exception)``).
    """


class TerminalRecordCorruptError(Exception):
    """A stored terminal row exists but does not satisfy the ``Terminal`` model.

    Review on PR #634. Raised in place of the bare pydantic
    ``ValidationError`` so the failure cannot be mistaken for a client error:
    ``ValidationError`` subclasses ``ValueError``, so letting it escape would
    surface as 400 from ``create_session`` and 404 from
    ``create_terminal_in_session`` -- both of which blame the caller for a
    corrupt server-side row. As a plain ``Exception`` it reaches each
    endpoint's catch-all and is reported as a 500, which is what a
    server-data fault is, and needs no new ``except`` arm to do it.
    """


# Upper bound (bytes) on a single offset-ranged read of a terminal log
# (U5 / #504, BR-2). ``read_output_range`` clamps its ``length`` to this so a
# caller (playback fetching output around a selected event) can never trigger
# an unbounded read of a large log file. 1 MiB is a defensible ceiling: it is
# far larger than any realistic per-event output window (the rolling
# STATE_BUFFER_MAX is only 8 KiB) yet bounds the worst-case allocation and
# response size to a fixed, predictable amount regardless of on-disk log size.
TERMINAL_RANGE_MAX_LENGTH = 1024 * 1024

# Timeout (seconds) for the cross-node deferred-failure notification POST to a
# remote supervisor's inbox. Runs on a worker thread, so a slow peer only
# delays this one notification — but still bounded so a black-holed node can't
# pin the thread.
CROSS_NODE_NOTIFY_TIMEOUT = 10.0

# Track terminals that have already received memory injection (first message only).
_memory_injected_terminals: set = set()
_memory_injected_lock = threading.Lock()

_CURRENT_COMPOSER_PROBE_MAX_CHARS = 64

# Strong references to in-flight deferred-init background tasks. asyncio keeps
# only a WEAK reference to tasks from loop.create_task, so without this a
# deferred provider.initialize() + input-send task could be GC'd mid-run,
# silently leaving a worker uninitialized. Tasks drop themselves on completion.
_deferred_init_tasks: set = set()

# External-owner deferred-init rows survive provider startup failures so Bridge
# and other external observers can read a durable verdict. Restart recovery
# scans those rows and converts ones stranded by a PREVIOUS cao-server process
# into interrupted_init failures. A retry of that scan after startup must not
# mistake a worker being initialized by THIS process for a crash survivor.
#
# The fence is set before an external-owner row is inserted (inside the same
# creation critical section) and is cleared only after its deferred-init task
# settles. A threading lock is used because row creation itself runs in
# asyncio.to_thread while recovery/task callbacks run on the event loop.
_active_deferred_init_external_owner_ids: set[str] = set()
_active_deferred_init_external_owner_lock = threading.RLock()


def _mark_deferred_init_external_owner_active(terminal_id: str) -> None:
    with _active_deferred_init_external_owner_lock:
        _active_deferred_init_external_owner_ids.add(terminal_id)


def _clear_deferred_init_external_owner_active(terminal_id: str) -> None:
    with _active_deferred_init_external_owner_lock:
        _active_deferred_init_external_owner_ids.discard(terminal_id)


def _is_deferred_init_external_owner_active(terminal_id: str) -> bool:
    with _active_deferred_init_external_owner_lock:
        return terminal_id in _active_deferred_init_external_owner_ids


def inject_memory_context(
    first_message: str, terminal_id: str, frozen_memory: str | None = None
) -> str:
    """Prepend <cao-memory> context block to the first user message.

    Tracks which terminals have already been injected so that only the very
    first user message after init receives the memory block.

    Calls the configured memory backend, which returns a formatted
    <cao-memory>...</cao-memory> block (or empty string if no memories exist).
    Stateless — no file mutation, no backup/restore.

    ``frozen_memory`` is an OPTIONAL PRE-RESOLVED BLOCK (issue #583 FR-9). When it
    is not None the block is injected verbatim and MemoryService is never
    consulted, so a replayed workflow run sees the memory the ORIGINAL run
    recorded rather than whatever the store holds today. When it is None this
    function behaves exactly as it always has, which is what keeps every
    non-workflow terminal in CAO unaffected — and this module deliberately knows
    nothing about workflows: the parameter is named for its content, not its
    origin.
    """
    with _memory_injected_lock:
        if terminal_id in _memory_injected_terminals:
            return first_message
        _memory_injected_terminals.add(terminal_id)

    if frozen_memory is not None:
        # "" is a SUPPLIED block, not an absent one: it means the original run
        # resolved no memory, so prepend nothing — and, crucially, do NOT fall
        # through to the live path. Deciding the arm on `is not None` while
        # deciding the prepend on truthiness is deliberate; collapsing the two
        # into one truthiness test is exactly how a run that legitimately froze
        # an empty block would pick up memories written after it, which is the
        # drift FR-9 exists to prevent.
        if not frozen_memory:
            return first_message
        # The operator's kill switch binds this path too. Skipping MemoryService
        # would otherwise skip its is_memory_enabled() check, and a workflow run
        # would paste memory into the context of someone who turned memory off.
        # The cost is that a replay under a disabled switch differs from the
        # original run — acceptable because the manifest records that memory was
        # frozen, so the difference is explainable, whereas a bypassed control
        # would leave no trace at all. Imported lazily for the same
        # settings -> memory circular-import reason memory_service documents.
        from cli_agent_orchestrator.services.settings_service import is_memory_enabled

        if not is_memory_enabled():
            return first_message
        # No try/except here on purpose: string concatenation cannot fail on I/O,
        # so the only thing a guard could swallow is a programming error — and
        # swallowing it would silently downgrade a replay to live memory, which
        # is a wrong answer wearing a right answer's clothes.
        return frozen_memory + "\n\n" + first_message

    try:
        if remote_memory_url():
            context = memory_context_for_terminal(
                terminal_id,
                task_description=first_message[:200],
            )
        else:
            context = MemoryService().get_curated_memory_context(
                terminal_id,
                task_description=first_message[:200],
            )
        if context:
            return context + "\n\n" + first_message
    except Exception as e:
        logger.warning(f"Failed to inject memory context for terminal {terminal_id}: {e}")
    return first_message


class OutputMode(str, Enum):
    """Output mode for terminal history retrieval.

    FULL: Returns complete terminal output (scrollback buffer)
    LAST: Returns only the last agent response (extracted by provider)
    """

    FULL = "full"
    LAST = "last"


# Providers that accept a runtime skill_prompt kwarg and append it to the
# system prompt at launch time.  Other providers deliver skills differently:
# Kiro (skill:// resources) and OpenCode (OPENCODE_CONFIG_DIR/skills symlink)
# discover skills natively; Copilot receives a baked catalog at install
# time.
RUNTIME_SKILL_PROMPT_PROVIDERS = {
    ProviderType.CLAUDE_CODE.value,
    ProviderType.CODEX.value,
    ProviderType.KIMI_CLI.value,
    ProviderType.ANTIGRAVITY_CLI.value,
    ProviderType.OMP.value,
    ProviderType.GROK_CLI.value,
    ProviderType.MINIMAX_CODE.value,
}

# Providers that cannot enforce a restricted tool policy natively: either the
# restriction is prompt-level text only, or (hermes, cursor_cli) nothing is
# passed at all and the provider auto-approves. Derived from the single table in
# utils/enforcement.py so this set, the launch gate and the docs agree.
SOFT_ENFORCEMENT_PROVIDERS = {
    provider for provider, level in PROVIDER_ENFORCEMENT.items() if level != NATIVE_ENFORCEMENT
}


def _resolve_working_directory(working_directory: Optional[str]) -> str:
    """Resolve launch cwd exactly as the tmux backend does before creation."""
    return resolve_and_validate_path(
        working_directory if working_directory is not None else os.getcwd(),
        allow_create=False,
        allow_file=False,
        description="Working directory",
    )


def _roll_back_backend_create_locked(
    session_name: str,
    window_name: str,
    *,
    created_session: bool,
) -> None:
    """Undo the backend resource a create just made. CALLER MUST HOLD the
    lifecycle lock for ``session_name``.

    Used by ``create_terminal``'s locked critical section so a failure between
    the backend create and the registry write cannot leave a live tmux
    session/window with no row. Both branches matter and they are NOT the same
    teardown:

    * ``created_session=True`` -- this call created the whole session, so kill the
      session and drop any forwarded env stashed for the name, so secrets don't
      linger in memory or bleed into a future reuse of the name.
    * ``created_session=False`` -- this call only added a WINDOW to a session that
      already existed (``new_session=False``: every MCP spawn/assign-into-an-
      existing-session call). Kill ONLY that window, so the pre-existing session
      and its other terminals are left alone. Note this is not a guarantee that
      the session survives: tmux drops a session when its last window dies, and
      the peer window that made the session non-empty at the `session_exists`
      check can be reaped by its own process exiting before this rollback runs --
      the lifecycle lock serializes CAO's transitions, not a pane's exit. In that
      race the session collapses and the peer's registry row is left pointing at
      a dead session. Killing the whole session instead would be strictly worse
      (it would destroy peers that ARE alive, which is the common case), so this
      stays window-scoped; the residual race is the same one the outer `except`
      path already carries and is tracked separately.

    Best-effort and never raises: it runs while an exception is already in
    flight, and that original failure is the one the caller must see.
    """
    if created_session:
        # `finally`, not a following statement: the env mapping must be dropped
        # however the kill turns out -- including when it raises a BaseException
        # (KeyboardInterrupt/SystemExit), which `except Exception` does not catch.
        # Sequencing these as two independent try blocks skipped the clear on
        # exactly that path, leaving a forwarded secret in the process-global map
        # keyed to a session name that is gone and may later be reused.
        # `finally` still lets a BaseException propagate, which is what we want:
        # a Ctrl-C must not be swallowed here.
        try:
            if not get_backend().kill_session(session_name):
                # Falsy means the backend could not confirm the kill (or found
                # nothing to kill). Either way the name may still be live, so say
                # so -- a silent branch here is how an orphan goes unnoticed.
                logger.warning(
                    f"Rollback: kill_session({session_name}) did not confirm the kill; "
                    "the session may still be live"
                )
        except Exception:
            logger.exception(f"Rollback: failed to kill session {session_name}")
        finally:
            try:
                clear_session_env(session_name)
            except Exception:
                logger.exception(f"Rollback: failed to clear session env for {session_name}")
    else:
        try:
            if not get_backend().kill_window(session_name, window_name):
                logger.warning(
                    f"Rollback: kill_window({session_name}:{window_name}) did not confirm "
                    "the kill; the window may still be live"
                )
        except Exception:
            logger.exception(f"Rollback: failed to kill window {session_name}:{window_name}")


def _still_owns_incarnation(session_name: str, terminal_id: Optional[str]) -> bool:
    """Under the lifecycle lock: does this row still own the current incarnation?

    Failed-init tombstones and deferred provider-cleanup rows can outlive the
    session they belong to. A surviving row and matching reusable name alone
    therefore do not prove ownership: its durable incarnation must still match
    the current pointer. Both are published atomically by the create transaction.
    Only legacy rows with no incarnation and no current pointer use the older
    row-existence witness. Callers must hold the lifecycle lock through rollback.
    """
    row = get_terminal_metadata(terminal_id) if terminal_id else None
    if row is None or row.get("tmux_session") != session_name:
        logger.warning(
            f"Rollback: terminal {terminal_id} is no longer registered under "
            f"{session_name}; another lifecycle operation owns that name now, so "
            f"the backend session is left alone"
        )
        return False
    row_incarnation = row.get("session_incarnation_id")
    current_incarnation = get_session_incarnation(session_name)
    if row_incarnation != current_incarnation:
        logger.warning(
            "Rollback: terminal %s no longer owns the current incarnation of %s; "
            "leaving the backend session and forwarded env alone",
            terminal_id,
            session_name,
        )
        return False
    return True


def _roll_back_cancelled_create(
    session_name: str,
    terminal_id: str,
    window_name: str,
    *,
    created_session: bool,
) -> None:
    """Undo a create whose awaiter was cancelled AFTER the worker succeeded.

    Runs on a worker thread. Unlike the in-closure rollback this must
    REACQUIRE the lifecycle lock: the worker released it when it returned, and
    an unlocked late kill could destroy a NEW incarnation of the name that
    another caller legitimately built in between — the same
    never-observable-half-built argument the closure's docstring makes. The
    lock serializes lifecycle operations; it does not say whose incarnation is
    under the name once acquired, so the row is checked as well: a
    ``delete_session`` plus a fresh create of the same name can both complete
    between the worker's commit and this compensation, and killing by name
    then would destroy the replacement and clear its forwarded env. Under the
    lock, kill the session/window only if this call's row still belongs to the
    current incarnation. Always drop this terminal's own row: no provider or
    deferred-init task has been started yet, so retaining the pending row would
    leave an UNKNOWN terminal that nobody can finish initializing.

    Best-effort like its sibling: the cancellation is already propagating and
    is what the caller must see.
    """
    with session_lifecycle_lock(session_name):
        if _still_owns_incarnation(session_name, terminal_id):
            _roll_back_backend_create_locked(
                session_name, window_name, created_session=created_session
            )
        try:
            db_delete_terminal(terminal_id)
        except Exception:
            logger.exception(
                f"Rollback: failed to delete registry row {terminal_id} " "after a cancelled create"
            )


def _roll_back_backend_create_if_still_ours(
    session_name: str,
    window_name: Optional[str],
    terminal_id: Optional[str],
    *,
    created_session: bool,
) -> None:
    """Undo a create that failed AFTER the locked transaction committed.

    Provider initialisation, FIFO setup and the rest of ``create_terminal`` run
    after the lifecycle lock was released, so by the time their failure reaches
    the outer handler another caller may have torn the name down and rebuilt it
    (``delete_session`` then a fresh ``new_session=True`` create, or the reverse
    order of the same pair). An unlocked ``kill_session(session_name)`` there
    destroyed that newer incarnation and left its registry row pointing at
    nothing.

    Reacquire the lock, then use this call's own registry row as the ownership
    witness (``_still_owns_incarnation``). If the row is gone or belongs to an
    older incarnation, the backend session and forwarded env are left alone.
    Otherwise the existing locked rollback runs exactly as before. The caller
    separately releases this terminal's provider and row, retaining its retry
    handle if provider cleanup is deferred.

    Synchronous, and it blocks on a ``threading.Lock``: the async caller runs it
    through ``asyncio.to_thread`` so a same-name teardown holding the lock
    parks this thread, not the API event loop.
    """
    with session_lifecycle_lock(session_name):
        if not _still_owns_incarnation(session_name, terminal_id):
            return
        _roll_back_backend_create_locked(
            session_name, window_name or "", created_session=created_session
        )


async def _finish_and_roll_back_cancelled_create(
    create_worker: "asyncio.Task[Tuple[str, bool, bool]]",
    session_name: str,
    terminal_id: str,
) -> None:
    """Await the un-cancellable create worker, then compensate its outcome.

    If the worker RAISED, its locked closure already rolled the backend
    resource back and never wrote the row — nothing to do. If it RETURNED, it
    built a session/window and committed a row that no caller will ever hear
    about; roll both back under the lifecycle lock.
    """
    try:
        window_name, session_created, _ = await create_worker
    except BaseException:
        return
    await asyncio.to_thread(
        _roll_back_cancelled_create,
        session_name,
        terminal_id,
        window_name,
        created_session=session_created,
    )


def _roll_back_failed_create(
    terminal_id: Optional[str],
    session_name: Optional[str],
    window_name: Optional[str],
    *,
    session_created: bool,
    window_created: bool,
    worktree_repo_root: Optional[str],
) -> None:
    """Undo everything a failed ``create_terminal`` built, in dependency order.

    One synchronous, best-effort function: every step is guarded so a failure
    in one never skips the ones after it, and the caller runs the whole thing
    on a single worker thread through ``_await_uncancellable``, so neither the
    lifecycle lock (a ``threading.Lock`` a same-name teardown may be holding)
    nor a cancellation of the create request can leave it half done. The order
    is the one the steps depend on:

    1. FIFO reader and status monitor -- stop consuming the pane's output.
    2. Backend session or window -- locked and ownership-checked
       (``_roll_back_backend_create_if_still_ours``); the helper also drops the
       forwarded env for a session it kills. The window arm exists for
       harness-control#186: a window added to an ALREADY-EXISTING session
       (``new_session=False``, every MCP spawn/assign-into-existing-session
       call) has no session-level teardown to fall back on, and without it a
       provider init timeout rolled back the row and stopped the reader but
       left the pane running, invisible to list/tree, forever.
    3. Provider -- AFTER the process-owning session/window is stopped, because
       a provider releasing private on-disk state must not race a process
       still writing it (Grok's updater can still be writing ``$GROK_HOME``
       while its initialization fails; its cleanup verifies no such process
       remains).
    4. Registry row -- unless the provider deferred its cleanup (an explicit
       ``False`` from ``cleanup_provider``), in which case the row is the only
       retry handle and is retained so the failed terminal stays discoverable
       and its deletion retryable rather than leaking credentials/config.
       Idempotent (``DELETE ... WHERE id = ?``), a no-op when the failure
       happened before the row was written.
    5. Worktree -- a worktree created before a later step failed would
       otherwise survive as an orphan worktree + branch with no CAO-side
       record pointing at it.
    """
    if terminal_id is not None:
        _clear_deferred_init_external_owner_active(terminal_id)
        try:
            fifo_manager.stop_reader(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        try:
            status_monitor.clear_terminal(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
    if session_created and session_name:
        try:
            _roll_back_backend_create_if_still_ours(
                session_name, window_name, terminal_id, created_session=True
            )
        except Exception:
            logger.exception(f"Rollback: locked session rollback failed for {session_name}")
    elif window_created and session_name and window_name:
        try:
            _roll_back_backend_create_if_still_ours(
                session_name, window_name, terminal_id, created_session=False
            )
        except Exception:
            logger.exception(
                f"Rollback: locked window rollback failed for {session_name}:{window_name}"
            )
    cleanup_complete = True
    try:
        if terminal_id is not None:
            cleanup_complete = provider_manager.cleanup_provider(terminal_id) is not False
    except Exception:
        # Preserve the existing rollback contract for an unexpected
        # provider-manager failure. Only an explicit False is a Grok
        # cleanup deferral with enough information to retry safely.
        cleanup_complete = True
    if cleanup_complete:
        try:
            if terminal_id is not None:
                db_delete_terminal(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
    elif terminal_id is not None:
        logger.warning(
            "Create rollback deferred Grok cleanup for %s; retaining terminal metadata for retry",
            terminal_id,
        )
    if worktree_repo_root is not None and terminal_id is not None:
        try:
            worktree_service.remove_worktree(worktree_repo_root, terminal_id)
        except Exception:
            # Best-effort like every step above; the create's own error is
            # what the caller must see, not a failed worktree removal.
            logger.exception(
                f"Rollback: worktree removal failed for {terminal_id} under {worktree_repo_root}"
            )


async def _await_uncancellable(fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> None:
    """Run blocking cleanup ``fn`` on a worker thread and see it through to the end.

    A cancellation of the awaiting task while ``fn`` runs (or waits on a lock)
    does not interrupt it: the thread is shielded, the wait resumes, and the
    cancellation is re-raised only once ``fn`` has returned, so the caller
    still observes it but never before the cleanup is durable. Repeat
    cancellations are absorbed the same way. An exception from ``fn`` is
    logged and swallowed: cleanup is best-effort and must not replace the
    error the caller is about to raise.

    The thread is driven through ``loop.run_in_executor`` directly, not
    ``asyncio.to_thread`` wrapped in a Task, on purpose. Whole-runner shutdown
    (``asyncio.run``'s ``_cancel_all_tasks``, uvicorn's equivalent) cancels
    every *Task* on the loop; a Task standing in for the thread would then be
    cancelled itself, ``await shield(job)`` would raise on every iteration
    without ever yielding, and shutdown would spin here forever after the
    thread had long finished. An executor Future is not a Task, so shutdown
    cancels only this awaiting coroutine, which keeps waiting for the thread
    and then re-raises. And should the Future itself ever be cancelled or
    finish behind our back, ``job.done()`` ends the loop instead of retrying
    a settled awaitable.
    """
    loop = asyncio.get_running_loop()
    job: "asyncio.Future[Any]" = loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))
    cancelled: Optional[asyncio.CancelledError] = None
    while True:
        try:
            await asyncio.shield(job)
            break
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
            if job.done():
                # The job itself was cancelled or completed between iterations:
                # nothing left to wait for. Retrying a settled awaitable would
                # raise immediately every time and never yield to the loop.
                break
            # Otherwise the caller was cancelled while the thread still runs:
            # keep waiting for it.
        except Exception:
            logger.exception("Rollback: failed-create cleanup raised")
            break
    if cancelled is not None:
        raise cancelled


# ``allowed_tools=None`` and ``allowed_tools=[]`` are DIFFERENT requests --
# ``None`` resolves the tool set from the agent profile while ``[]`` is an
# explicit empty set that is not resolved (see the ``allowed_tools is None``
# branch in ``create_terminal``) -- so they must not hash alike. These two
# markers keep them apart by construction: an explicit list always starts with
# ``_FP_TOOLS_SET``, so no list, not even one whose sole member is the unset
# marker's own text, can produce the unset encoding.
_FP_TOOLS_UNSET = "-"
_FP_TOOLS_SET = "+"


def _fingerprint_component(value: str) -> str:
    """Length-prefix one fingerprint component so it cannot forge a boundary.

    Review on PR #634. A separator alone is not enough when the
    values are caller-controlled, and these are: ``allowed_tools`` arrives as a
    query-param string split on ``","`` (``api/main.py``), and the scalar
    fields are query params too, so a caller can embed the separator byte
    itself via percent-encoding. Length-prefixing makes every component
    self-delimiting, which kills the whole class in one place rather than
    per-field: ``["a\x1fb"]`` can no longer serialise like ``["a", "b"]``, and
    a ``NUL`` inside ``model`` can no longer forge the field boundary.
    """
    return f"{len(value)}:{value}"


def _request_fingerprint(
    provider: Optional[str],
    agent_profile: Optional[str],
    session_name: Optional[str],
    working_directory: Optional[str],
    caller_id: Optional[str],
    model: Optional[str],
    use_worktree: bool,
    engine: Optional[KiroEngine | str],
    allowed_tools: Optional[List[str]],
    env_vars: Optional[Dict[str, str]],
    resume_session_id: Optional[str],
    initial_message: Optional[str],
    initial_message_orchestration_type: Optional[OrchestrationType],
) -> str:
    """Fingerprint the create-terminal request an idempotency key stands for.

    Review on PR #634, issue #616. Stored alongside the key so a later call
    presenting the same key can be told apart: same fingerprint is a RETRY
    (return the existing terminal), different fingerprint is a COLLISION
    (raise ``IdempotencyKeyConflict``).

    REQUESTED values, not resolved ones, and that is load-bearing. The key
    check runs before ``create_terminal`` resolves the working directory,
    provider fallback or Kiro engine, so resolved values do not exist yet;
    computing them in order to compare would perform the very work the key
    exists to skip. A genuine retry re-sends the same REQUEST, so
    requested-vs-requested is the comparison that matches, and fingerprinting
    resolved values would spuriously conflict a legitimate retry issued from
    a different process cwd (whose ``working_directory=None`` resolves
    differently) -- a false 409 on the exact case this feature serves.

    ``caller_id`` is one of the fields, which is what makes a CROSS-CALLER
    collision a mismatch rather than a silent hand-off of someone else's
    worker: two supervisors reusing ``job-1`` fingerprint differently and the
    second gets a loud 409. It is deliberately NOT additionally scoped in the
    primary key -- see the accepted residuals in ``create_terminal``'s
    docstring for the one case this cannot separate.

    ``env_vars`` and ``resume_session_id`` are hashed for the same reason, and
    ``env_vars`` has the sharpest precedent of any field here: ``RunStepRequest``
    refuses ``env_vars`` together with ``reuse_terminal_id`` outright, because
    "a silently dropped RUN_ID/GENERATION fence token is the quiet identity
    failure NFR-SEC-4 exists to prevent". Unhashed, this function reproduced
    exactly that -- a caller asking for one workflow run received a terminal
    launched with ANOTHER run's fence tokens, silently.

    ``allowed_tools`` AND ``engine`` ARE BOTH HASHED, and leaving either out
    was a live privilege-escalation hole rather than a matter of taste
    (review on PR #634). Both are persisted COLUMNS on
    the ``terminals`` table (``database.py:43,46``), i.e. launch-time terminal
    identity, and ``POST /sessions/{name}/terminals`` accepts either one in the
    SAME call as ``idempotency_key``. Unhashed, a second call reusing a key
    with a WIDER ``allowed_tools`` was handed the first call's narrow terminal
    -- and in the other order, a caller asking for a narrow tool set received a
    terminal holding ``execute_bash`` it never requested. ``allowed_tools`` is
    baked in at launch, not re-evaluated per message, so that wrong policy is
    PERMANENT for the terminal's life. Unhashed ``engine`` additionally let a
    key hit skip the Kiro engine validation the same request would otherwise
    have been rejected by (see ``create_terminal``'s docstring).

    This is also the contract two other reuse paths in this repo already hold
    us to: ``agent_step._validate_reused_terminal`` RAISES on an engine
    mismatch, and ``api/main.py`` rejects ``env_vars`` with
    ``reuse_terminal_id`` outright. ``step_fingerprint`` -- the other
    fingerprint in this codebase -- hashes ``allowed_tools`` sorted (BR-4) and
    says of ``engine`` that "it stays unconditionally hashed ... it is the
    single easiest thing to get wrong here."

    SERIALIZATION, following ``step_fingerprint``'s discipline:

    - ``allowed_tools`` members are SORTED but NOT de-duplicated, because
      ``["a","b"]`` and ``["b","a"]`` grant identical capability -- order
      sensitivity would manufacture a false conflict for a caller who merely
      reordered a comma list -- while ``["a","a"]`` differs from ``["a"]`` and
      collapsing it would hide a real difference.
    - EVERY component is LENGTH-PREFIXED by ``_fingerprint_component``, not
      merely separated. The values are caller-controlled, so a separator alone
      is forgeable; see that helper for the concrete collisions it prevents.
    - ``None`` and ``[]`` hash DIFFERENTLY (``_FP_TOOLS_UNSET`` versus
      ``_FP_TOOLS_SET``). They are genuinely different requests: ``None`` means
      "resolve the tool policy from the agent profile" while ``[]`` is an
      explicit empty set that is NOT resolved (see the ``allowed_tools is
      None`` branch below), so collapsing them would let one caller be served
      the other's privilege set. Note ``[]`` is NOT reachable over HTTP -- both
      endpoints parse ``allowed_tools.split(",") if allowed_tools else None``,
      so an empty query value arrives as ``None`` -- but it IS reachable from
      the in-process callers (session, flow and step services), which is why
      the distinction is kept rather than simplified away.
    - ``env_vars`` is a MAPPING, so it sorts by key (declaration order is not
      a difference) and length-prefixes key AND value, which no
      single-axis scheme would do. Unlike ``allowed_tools``, ``None`` and
      ``{}`` deliberately SHARE an encoding: every use in ``create_terminal``
      collapses them already (``env_vars or {}``, ``if env_vars:``), so they
      cannot yield materially different terminals. Same-shaped question as
      ``allowed_tools``, opposite verified answer -- which is why the resolver
      has to be read rather than the pattern matched.
    - ``engine`` normalises through ``KiroEngine.value`` because the parameter
      accepts the enum OR a plain string -- ``api/main.py`` forwards a query
      string while ``parse_kiro_engine`` returns the member, and the same
      logical request arriving by those two routes must produce ONE digest or
      a legitimate retry 409s.

    sha256 hexdigest rather than the joined text: the column stays a bounded
    64 chars regardless of path length, and filesystem paths and profile names
    are not left sitting in the database in cleartext. ``None`` normalises to
    ``""`` for the scalar fields, and the field ORDER is fixed by this function
    -- it is the only writer and the only reader, so the digest never has to be
    stable across versions, only within one.
    """
    if allowed_tools is None:
        tools = _FP_TOOLS_UNSET
    else:
        tools = _FP_TOOLS_SET + "".join(
            _fingerprint_component(tool) for tool in sorted(allowed_tools)
        )

    # Normalised WITHOUT `parse_kiro_engine`, and the reason is purity rather
    # than any particular status code: `parse_kiro_engine` VALIDATES, so calling
    # it here would make digest computation a validation site. A fingerprint
    # helper has one job -- map a request to a stable string -- and must not
    # decide whether that request is acceptable; the engine is validated below,
    # on the path that owns that decision. An invalid engine simply hashes as
    # itself and mismatches, which is all this function needs to be correct.
    # `KiroEngine` is a `str` Enum so joining a member happens to work, but
    # `.value` is the documented contract (`step_fingerprint`: "a KiroEngine
    # member's repr is not stable across versions") and an explicit branch
    # cannot be broken by a later switch to a non-str Enum. `str()` runs only on
    # the non-member path, so the unsafe `str(KiroEngine.V2) == "KiroEngine.V2"`
    # form is unreachable.
    if engine is None:
        engine_value = ""
    elif isinstance(engine, KiroEngine):
        engine_value = engine.value
    else:
        engine_value = str(engine)

    # `None` and `{}` share one encoding, and that is VERIFIED rather than
    # assumed -- the opposite answer to `allowed_tools` above. Every use of
    # `env_vars` in `create_terminal` collapses them (`env_vars or {}` when
    # merging session env, and `if env_vars:` before persisting), so both mean
    # "no extra environment" and no caller can be served a materially different
    # terminal by the distinction. Sorted by key so declaration order cannot
    # manufacture a false conflict, with key AND value each length-prefixed so
    # no pair can forge a boundary.
    env = "".join(
        _fingerprint_component(key) + _fingerprint_component(value)
        for key, value in sorted((env_vars or {}).items())
    )

    # The DELIVERED TASK is part of the request identity, and leaving it out was
    # a silent-drop bug rather than a matter of taste (review on PR #634).
    # `create_terminal` itself delivers `initial_message` -- it schedules the
    # deferred init that sends it -- so a key hit returns EARLY, above that
    # scheduling. Unhashed, seeding a key for task A and retrying the otherwise
    # identical request with task B handed back A's terminal and discarded B
    # entirely: no conflict, no delivery, no log line. Hashed, that second call
    # is a 409, which is what both the endpoint's "exact request" contract and
    # the CLI's own help text already promise.
    #
    # The orchestration type rides along because it selects HOW the message is
    # delivered, so the same text under a different type is a different
    # operation. `OrchestrationType` is normalised via `.value` for the reason
    # `engine` is: the enum member's repr is not stable across versions.
    #
    # Note this does NOT change the handoff CLI path, where the message is sent
    # AFTER creation by `_send_direct_input_handoff`/`_run_step_and_build_result`
    # rather than passed here -- `initial_message` is None on both attempts, so
    # a handoff retry still reuses its worker. That asymmetry is the honest one:
    # these endpoints own delivery and can therefore conflict on it; the handoff
    # path does not, and deduplicating ITS submission needs the durable run
    # record tracked separately (#715), not this fingerprint.
    if initial_message_orchestration_type is None:
        orchestration_value = ""
    elif isinstance(initial_message_orchestration_type, OrchestrationType):
        orchestration_value = initial_message_orchestration_type.value
    else:
        orchestration_value = str(initial_message_orchestration_type)

    parts = [
        provider or "",
        agent_profile or "",
        session_name or "",
        working_directory or "",
        caller_id or "",
        model or "",
        "1" if use_worktree else "0",
        engine_value,
        tools,
        env,
        resume_session_id or "",
        initial_message or "",
        orchestration_value,
    ]
    return hashlib.sha256(
        "\x00".join(_fingerprint_component(part) for part in parts).encode("utf-8")
    ).hexdigest()


async def create_terminal(
    provider: str,
    agent_profile: str,
    session_name: Optional[str] = None,
    new_session: bool = False,
    working_directory: Optional[str] = None,
    allowed_tools: Optional[list[str]] = None,
    registry: PluginRegistry | None = None,
    env_vars: Optional[dict[str, str]] = None,
    caller_id: Optional[str] = None,
    defer_init: bool = False,
    initial_message: Optional[str] = None,
    initial_message_orchestration_type: Optional[OrchestrationType] = None,
    engine: Optional[KiroEngine | str] = None,
    kiro_capability_probe: Optional[Callable[[KiroEngine, set[str]], KiroCapabilities]] = None,
    model: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    use_worktree: bool = False,
    group: Optional[List[str]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
) -> Terminal:
    """Create a new terminal with an initialized CLI agent.

    This function orchestrates the complete terminal creation workflow:
    0. If ``idempotency_key`` maps to a terminal from a prior call, return
       IT instead -- no tmux window, no provider process, no new DB row
    1. Generate unique terminal ID and window name
    2. Create tmux session/window (new or existing)
    3. Save terminal metadata to database
    4. Initialize the CLI provider (starts the agent)
    5. Set up terminal logging via tmux pipe-pane

    Args:
        provider: Provider type string (e.g., "kiro_cli", "claude_code")
        agent_profile: Name of the agent profile to use
        session_name: Optional custom session name. If not provided, auto-generated.
        new_session: If True, creates a new tmux session. If False, adds to existing.
        working_directory: Optional working directory for the terminal shell
        env_vars: Operator-forwarded env vars (``cao launch --env``). On
            ``new_session=True``, these are stored on the session record and
            inherited by every worker spawned later in the same session. On
            ``new_session=False``, the persisted session vars are merged in
            automatically and the explicit ``env_vars`` argument is merged on
            top, winning on key conflict — per-step vars (e.g. workflow
            routing ids) must reach the window even inside an existing
            session. See issues #248 and #408.
        caller_id: Terminal ID of the supervisor that created this terminal
            via handoff/assign. Recorded so send_message can route callbacks
            structurally instead of parsing IDs out of message text (issue #284).
            None for operator-launched terminals.
        engine: Explicit Kiro engine. For Kiro, it must agree with the selected
            profile's engine when both are present; omitted resolves to v2.
        kiro_capability_probe: Optional test seam for the bounded wrapper probe.
        model: Explicit per-call model override, forwarded to the provider
            (where supported -- see each provider's own __init__) ahead of
            the agent profile's own static `model` field. Lets a caller
            (e.g. MCP handoff/assign's own `model` parameter) pin a specific
            model for one worker without needing a dedicated agent profile.
            None = behavior unchanged (profile.model, if any, still applies).
        use_worktree: If True, provision an isolated ``git worktree`` (issue
            #100) for this terminal instead of using ``working_directory`` as
            given -- resolves the repo root from ``working_directory`` (or the
            server's own cwd when unset), creates a fresh worktree on its own
            branch there, and overrides ``working_directory`` to the new
            worktree path before the tmux session/window is created. Requires
            the resolved directory to actually be inside a git repository.
        group: Ordered, general-to-specific grouping array for list_siblings
            discovery (#432). None = this terminal opts out of discovery.
        metadata: Free-form JSON describing what this terminal is doing.
            Also updatable later by the running agent via the
            ``update_metadata`` MCP tool.
        idempotency_key: Review on PR #634, issue #616. When given and a PRIOR
            call already created a terminal for the SAME KEY **and the same
            request**, that terminal is returned as-is and nothing else in
            this function runs -- no tmux window, no provider process, no new
            DB row. This is what makes a retry after a lost response safe:
            the caller that never saw the first response (e.g. a killed CLI
            process) can call again with the SAME key and land on the terminal
            the first, already-committed attempt produced, instead of creating
            a second worker. Persisted atomically with the terminal row (see
            ``database.create_terminal``); ``None`` (default) is the existing,
            unprotected behavior every current caller keeps.

            The key does NOT identify the request on its own -- it is matched
            together with a fingerprint of ELEVEN fields (see
            ``_request_fingerprint``). Presenting a key that a DIFFERENT
            request already claimed raises ``IdempotencyKeyConflict``
            (HTTP 409) rather than handing back a terminal that answers
            someone else's question. A key whose terminal no longer exists is
            treated as stale and simply creates fresh -- that is not a
            conflict.

            THE FIELD SET IS CLOSED BY ENUMERATION, not by however many review
            rounds happened to run. Every caller-reachable parameter of BOTH
            create endpoints -- ``POST /sessions`` (including everything
            ``CreateSessionBody`` and its ``CreateTerminalBody`` base carry)
            and ``POST /sessions/{name}/terminals`` -- is classified below,
            hashed or excluded-with-a-reason. Adding a parameter to either
            endpoint means classifying it here.

            HASHED (11) -- these determine what the terminal IS, or what
            privileges and context it launches with:
            ``provider``, ``agent_profile``, ``session_name``,
            ``working_directory``, ``caller_id``, ``model``, ``use_worktree``,
            ``engine``, ``allowed_tools``, ``env_vars``,
            ``resume_session_id``.

            EXCLUDED, each for a checked reason:

            - ``idempotency_key`` itself. It is the lookup key; hashing it
              would make every key match only itself.
            - ``group`` and ``metadata``. Discovery labels that are separately
              mutable AFTER creation via ``PATCH /terminals/{id}/group`` and
              the ``update_metadata`` MCP tool, so a create-time key is not
              their integrity boundary -- a caller who cares about their value
              cannot rely on creation to fix it anyway.
            - ``initial_message`` and ``initial_message_orchestration_type``.
              The delivered payload and its routing, not the terminal: neither
              is persisted on the row, and a genuine retry re-sends the same
              message. These create endpoints do not own the prompt.
            - ``defer_init``. Excluded, and this one was decided against the
              instinct that it looks like identity, because three things check
              out against the code:
              (a) On ``POST /sessions`` it is not caller-settable at all --
              ``session_service.create_session`` derives it as
              ``defer_init=initial_message is not None``. Hashing it would
              therefore make two otherwise-identical requests conflict purely
              because one supplied a message and the other did not, i.e. it
              would partially hash ``initial_message`` through the back door,
              contradicting the deliberate decision above not to hash the
              prompt.
              (b) It leaves NO permanent difference in the created terminal.
              The only row column it touches is ``shell_command``, which the
              deferred path sets to ``None`` up front and then writes after
              ``provider.initialize()`` returns, converging on the same value
              the synchronous path records immediately.
              (c) On a key HIT the returned status is read live by
              ``get_terminal``, not taken from this request, so ``defer_init``
              cannot change what a hit returns. And the short-circuit never
              waits for initialisation for ANY caller -- that is its purpose --
              so there is no wait guarantee here for a differing
              ``defer_init`` to violate. Hashing it would manufacture
              conflicts while buying no guarantee.
            - ``memory_manager``. Never reaches this function: the endpoint
              uses it to spawn a SEPARATE sidecar terminal with
              ``agent_profile="memory_manager"`` in a background task, so it
              cannot alter the identity of the terminal this key maps to.
              Excluded from the fingerprint, and no longer a duplication hole:
              the spawn is gated only on the flag's truthiness and this
              function still returns the same ``Terminal`` shape whether it
              created one or matched a key, so the endpoint cannot tell a reuse
              from a fresh create -- it therefore hands the sidecar its OWN key
              derived from the caller's (``<key>:memory-manager-sidecar``),
              making that create idempotent in its own right instead of relying
              on a signal it cannot get. A keyed retry now resolves to the
              first call's sidecar rather than spawning a second one.
            - ``new_session``. Not a caller parameter on either endpoint --
              each route passes its own fixed value -- so no caller can vary
              it under a shared key.
            - Framework and auth parameters (``request``,
              ``background_tasks``, ``_scopes``) and the ``body`` wrapper
              itself, which is expanded into its fields above.

            ``engine`` being in that set also closes a validation BYPASS
            (review on PR #634). The Kiro engine checks below run AFTER this
            short-circuit, so a key hit used to return 200 for an ``engine``
            the very same request would otherwise have been rejected with 400.
            A first call carrying an invalid engine raises before any terminal
            or key row is written, so a key hit implies the STORED engine was
            valid -- and any later differing engine now mismatches the
            fingerprint and is refused.

            ACCEPTED MISATTRIBUTION, recorded rather than left silent. Two
            cases, only one of which is this change's:

            - USED key + invalid engine: was 200 (the bypass above), now 409
              "this key was already used for a different request". So the
              operator is told to change their KEY when their ENGINE is what
              is wrong. Accepted rather than fixed, because reaching the real
              validator means loading the agent profile and running the Kiro
              capability probe -- a ``subprocess.run`` -- which is exactly the
              work this short-circuit exists to skip; paying it on every retry
              would trade away the property the feature is FOR in exchange for
              a better message. The request is refused either way; only the
              stated reason is imprecise.
            - FRESH key + invalid engine: 404 on
              ``POST /sessions/{name}/terminals`` (400 on ``POST /sessions``),
              because the validator raises a BARE ``ValueError`` -- not a
              ``KiroCapabilityError`` -- so it falls past that route's 400 arm
              to its generic "not found" arm. That is PRE-EXISTING, is
              unchanged by this change, and is deliberately not fixed here --
              correcting it means reworking error mapping this change does not
              own. Noted only so the 409 above is not mistaken for a
              regression from a 400 that never existed.

            This resolves an apparent tension with the commit directly beneath
            this one, which stopped ``engine`` being forwarded to a non-Kiro
            provider on the handoff reuse path. That change treats ``engine``
            as provider-SPECIFIC; this one treats it as part of request
            IDENTITY. Both hold: precisely because ``engine`` only means
            something for one provider, two requests differing in it are
            different requests, and the pair must not be conflated by a key.

            Two accepted residuals, recorded so they are not mistaken for
            bugs. Note neither is an exclusion from the field set above --
            those are enumerated there with their reasons; these are limits of
            what a fingerprint over those fields can distinguish:

            1. Two callers that BOTH have ``caller_id=None`` and are otherwise
               identical in all eleven fields are indistinguishable by
               fingerprint, so the second reuses the first's terminal. At that
               point the two requests are the same request by every property
               the server can observe, and reuse is the defensible answer.
               This is a REAL case rather than a hypothetical one, and
               specifically on the fresh-session path: ``caller_id`` is a
               caller parameter on ``POST /sessions/{name}/terminals`` ONLY --
               ``POST /sessions`` does not expose it -- so every keyed
               fresh-session create arrives with ``caller_id=None`` and this
               residual is the norm there, not the exception.
            2. The DELIVERED PROMPT is not hashed, so two same-shape requests
               carrying different messages reuse one terminal. This is
               deliberate and must not be "fixed" by adding the prompt -- a
               genuine retry re-sends the same prompt, and these create
               endpoints are not the prompt's owner.

            KNOWN DIVERGENCE, stated so the next reader need not rediscover
            it: even with eleven fields this remains a WEAKER contract than
            the other reuse path in this repo.
            ``agent_step._validate_reused_terminal`` RAISES on a provider or
            engine mismatch against the PERSISTED row, and ``RunStepRequest``
            rejects ``env_vars`` combined with ``reuse_terminal_id`` outright.
            Here a mismatch is refused only insofar as it changes one of the
            eleven hashed fields, and the comparison is
            request-against-request rather than
            request-against-persisted-metadata. The practical gap: a field
            that is excluded above, or a difference between the request and
            what the mapped terminal actually persisted, is not caught here.

    Returns:
        Terminal object with all metadata populated

    Raises:
        ValueError: If session already exists (new_session=True) or not found (new_session=False)
        IdempotencyKeyConflict: If ``idempotency_key`` was already used for a
            different request (surfaced as HTTP 409 by both create endpoints)
        TerminalRecordCorruptError: If the terminal a key maps to has a stored
            row that does not satisfy the ``Terminal`` model (HTTP 500)
        TerminalLimitError: If the node's tracked-terminal cap (CAO_MAX_TERMINALS /
            server.max_terminals; unset = unlimited) is already reached
        TimeoutError: If provider initialization times out
    """
    # Idempotency resolution runs BEFORE the terminal cap check below, and the
    # order is deliberate: a key HIT returns an already-existing terminal and
    # allocates nothing, so charging it against the cap would 429 a legitimate
    # retry on a full node -- the one case this feature exists to make safe.
    # The cap still precedes every actual allocation (worktree, tmux window, DB
    # row, provider process), which is all its own placement-guard needs.
    request_fingerprint: Optional[str] = None
    if idempotency_key:
        request_fingerprint = _request_fingerprint(
            provider,
            agent_profile,
            session_name,
            working_directory,
            caller_id,
            model,
            use_worktree,
            engine,
            allowed_tools,
            env_vars,
            resume_session_id,
            initial_message,
            initial_message_orchestration_type,
        )
        existing_record = get_idempotency_record(idempotency_key)
        existing_terminal_id = existing_record.terminal_id if existing_record else None
        if existing_terminal_id is not None:
            # Only the LOOKUP is guarded. `Terminal(**row)` is deliberately
            # OUTSIDE this try (review on PR #634): pydantic's ValidationError
            # subclasses ValueError, so a single try around both would catch a
            # row that EXISTS but does not validate and treat it as an absent
            # one -- deleting a LIVE terminal's mapping below and creating a
            # second worker for the same key, which is the exact duplication
            # this feature exists to prevent, reported as "no longer exists".
            # The `terminals` row and the `Terminal` model genuinely differ
            # (no `working_directory` on the model, `metadata` vs
            # `metadata_json`), so a future column rename reaches this arm; it
            # must fail loudly rather than silently duplicate the job.
            try:
                row = get_terminal(existing_terminal_id)
            except ValueError:
                # The key's mapping outlived the terminal it pointed to (e.g.
                # a completed-and-torn-down handoff, retried long after the
                # fact) -- there is nothing left to recover, so fall through
                # and create fresh rather than raising on an operator who
                # simply reused a key from a job that already finished.
                #
                # The stale row must be deleted FIRST (review on PR #634):
                # deleting a terminal does not cascade to idempotency_keys, so
                # leaving this row in place would make the replacement
                # terminal's own idempotency insert below collide on the same
                # primary key and raise IntegrityError -- turning a graceful
                # fallthrough into a guaranteed 500 on every such retry. The
                # expected_terminal_id guard makes this a compare-and-delete:
                # if a concurrent caller already replaced the mapping, this
                # deletes nothing and this attempt's own insert below is the
                # one that raises IntegrityError instead.
                delete_idempotency_key(idempotency_key, existing_terminal_id)
                logger.info(
                    "idempotency_key %r maps to terminal %r, which no longer exists; "
                    "creating a new terminal",
                    idempotency_key,
                    existing_terminal_id,
                )
            else:
                # Reached ONLY once the mapped terminal is confirmed to still
                # exist, and that ordering is the whole point: the stale-key
                # branch above already fell through, so a key whose terminal is
                # gone is never compared and never conflicts. Checking the
                # fingerprint first would 409 the legitimate case this feature
                # was built for -- an operator reusing a key from a job that
                # already finished.
                if existing_record is not None and (
                    existing_record.request_fingerprint != request_fingerprint
                ):
                    # A different request under the same key is operator error,
                    # not a retry, and returning the stored terminal here is not
                    # a merely-wrong return value: _handoff_impl feeds it
                    # straight into reuse_terminal_id, so this caller's prompt
                    # would be delivered into the OTHER caller's running worker,
                    # in that worker's session, under its tool restrictions --
                    # and this caller's teardown would then delete it.
                    raise IdempotencyKeyConflict(
                        f"idempotency_key {idempotency_key!r} was already used for a "
                        "different request; use a distinct key"
                    )
                # A genuine retry: same key, same request. Return the terminal
                # the first call produced without doing any real work -- the
                # property haofeif signed off on, unchanged by the check above.
                try:
                    return Terminal(**row)
                except ValidationError as exc:
                    # Re-raised as a non-ValueError so a corrupt STORED row is
                    # reported as a 500 rather than being blamed on the caller
                    # as a 400/404. See TerminalRecordCorruptError.
                    raise TerminalRecordCorruptError(
                        f"terminal {existing_terminal_id!r} is mapped by "
                        f"idempotency_key {idempotency_key!r} but its stored row does "
                        f"not satisfy the Terminal model: {exc}"
                    ) from exc

    # Per-node terminal cap (one-agent-per-pod k8s topology; worker pods set
    # CAO_MAX_TERMINALS=1). Checked FIRST, before any resource (worktree, tmux
    # window, DB row, provider process) is allocated, so a full node rejects
    # cleanly with nothing to roll back. Best-effort under concurrency: two
    # simultaneous creates can both pass the check (no cross-request lock),
    # which is acceptable for the cap's placement-guard purpose.
    max_terminals = get_max_terminals()
    if max_terminals is not None:
        tracked_count = count_runtime_allocated_terminals()
        if tracked_count >= max_terminals:
            raise TerminalLimitError(
                f"Terminal limit reached: this node already has {tracked_count} tracked "
                f"terminal(s) and CAO_MAX_TERMINALS/server.max_terminals is "
                f"{max_terminals}. Delete a terminal or target a different node."
            )

    terminal_id: Optional[str] = None
    # The window name the failure handler rolls back. ``window_name`` itself is
    # first bound inside the try, so a failure before that point (capability
    # probe, worktree) would leave it unassigned; this mirror is set from the
    # create worker's result, the only point after which there is a window to
    # roll back, and is None before it.
    rollback_window_name: Optional[str] = None
    session_created = False  # tracks whether THIS call created the tmux session
    # harness-control#186: tracks whether THIS call created a new WINDOW in an
    # already-existing session (the `new_session=False` branch below — what
    # every MCP spawn/assign-into-existing-session call does). Independent of
    # `session_created` above: on failure, the cleanup path already tears
    # down the whole session (window included) when THIS call created a brand
    # new one, but had no equivalent for a window added to a session that
    # already existed — see the `except` block.
    window_created = False
    # Reassigned to the resolved repo root once a worktree is actually created
    # below (Step 1b), so the failure-cleanup path (the `except` block) knows
    # whether there is a worktree to roll back too. Still None if Step 1b never
    # ran (use_worktree=False) or itself failed before create_worktree returned.
    worktree_repo_root: Optional[str] = None
    try:
        # Resolve profile policy and Kiro engine BEFORE allocating any backend
        # resource. A KAS request must probe then fail closed with no window,
        # database row, FIFO, Herdr registration, or provider process.
        try:
            profile = load_agent_profile(agent_profile)
        except FileNotFoundError:
            profile = None
        # Production loaders return AgentProfile. Treat a test double or an
        # otherwise malformed object as no selected profile rather than
        # accepting arbitrary attributes as configuration.
        if profile is not None and not isinstance(profile, AgentProfile):
            profile = None

        if provider == ProviderType.KIRO_CLI.value:
            resolved_engine = resolve_kiro_engine(
                explicit=engine,
                profile=getattr(profile, "engine", None),
            )
            if allowed_tools is None and profile is not None:
                from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
                from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

                mcp_server_names = grantable_server_names(profile)
                allowed_tools = resolve_allowed_tools(
                    profile.allowedTools, profile.role, mcp_server_names
                )
            # Kiro runs headlessly, so current CAO behavior always bypasses its
            # interactive approval prompt. Profile/MCP policy remains enforced
            # by CAO, while unrestricted profiles additionally force legacy UI.
            # Mirror the launch-time precedence (`model or profile.model`, see
            # _get_profile_model): probing the profile snapshot alone would let an
            # explicit override launch --model on a wrapper never probed for it.
            requested = requested_kiro_capabilities(
                resolved_engine,
                model=model or (profile.model if profile else None),
                yolo=True,
            )
            probe = kiro_capability_probe or probe_kiro_capabilities
            await asyncio.to_thread(probe, resolved_engine, requested)
            if resolved_engine == KiroEngine.KAS:
                raise KiroPhase0KASError(
                    bool(profile and (profile.allowedTools or profile.toolsSettings))
                )
        else:
            if engine is not None:
                raise ValueError("Kiro engine selection is only valid for provider 'kiro_cli'")
            resolved_engine = None

        # Resolve tool policy before persistence for non-Kiro providers too.
        if allowed_tools is None and profile is not None:
            from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
            from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

            mcp_server_names = grantable_server_names(profile)
            allowed_tools = resolve_allowed_tools(
                profile.allowedTools, profile.role, mcp_server_names
            )

        # Step 1: Generate unique identifiers
        terminal_id = generate_terminal_id()

        if not session_name:
            session_name = generate_session_name()

        window_name = generate_window_name(agent_profile)

        # Step 1b: Provision an isolated git worktree (issue #100, Phase 1) before
        # the tmux session/window below consumes `working_directory` -- the
        # worktree's own path REPLACES whatever `working_directory` was given
        # (explicit or caller-inherited), so the terminal always launches inside
        # its own isolated checkout rather than the shared one it would
        # otherwise have used.
        if use_worktree:
            # `find_repo_root`/`create_worktree` are synchronous `subprocess.run`
            # calls (a full worktree checkout can take seconds to tens of
            # seconds on a large repo); `create_terminal` is awaited directly on
            # the shared event loop, so running them in-line here would freeze
            # every other cao-server request (status monitor ticks, inbox
            # delivery, unrelated terminal calls) for the duration. Offload to a
            # thread, same posture as `delete_terminal`'s own blocking subprocess
            # work (see its `run_in_executor` call site in api/main.py).
            worktree_repo_root = await asyncio.to_thread(
                worktree_service.find_repo_root, working_directory or os.getcwd()
            )
            working_directory = await asyncio.to_thread(
                worktree_service.create_worktree, worktree_repo_root, terminal_id
            )

        # Resolve AFTER the worktree block, not before: when `use_worktree` is set
        # the block above REPLACES `working_directory` with the new worktree path,
        # so resolving earlier would both launch tmux in the pre-worktree directory
        # (defeating the isolation #100 provides) and persist that stale path as the
        # terminal's working_directory. This is the effective launch cwd either way.
        resolved_working_directory = _resolve_working_directory(working_directory)

        # Normalize the session name BEFORE anything keys off it: the lifecycle
        # lock below is per session NAME, so it must be taken on the SAME string
        # the tmux create and the registry row use, or a create and a teardown of
        # what is really one session would take two different locks.
        if new_session and not session_name.startswith(SESSION_PREFIX):
            # Ensure session name has the CAO prefix for identification
            session_name = f"{SESSION_PREFIX}{session_name}"

        # Step 3: Build a runtime skill catalog only for providers that consume
        # it at launch time (see RUNTIME_SKILL_PROMPT_PROVIDERS).
        skill_prompt = (
            build_skill_catalog(profile.skills if profile else None)
            if provider in RUNTIME_SKILL_PROMPT_PROVIDERS
            else None
        )

        # Step 3b: Soft-enforcement guard: kimi_cli/codex have NO native tool-blocking
        # mechanism (kimi runs --yolo; restrictions are prompt-level text
        # only), so a restricted policy on them is advisory, not enforced.
        # Surface that loudly at launch so operators route restricted or
        # write-capable roles to hard-enforcement providers instead.
        if (
            provider in SOFT_ENFORCEMENT_PROVIDERS
            and allowed_tools is not None
            and "*" not in allowed_tools
        ):
            logger.warning(
                f"Terminal {terminal_id}: provider '{provider}' cannot enforce tool "
                f"restrictions ({enforcement_for(provider)} enforcement) but profile "
                f"'{agent_profile}' requests {allowed_tools}. Treat this worker as "
                f"unrestricted; for enforced restrictions use one of: "
                f"{', '.join(native_providers())}."
            )
        elif (
            provider == ProviderType.KIRO_CLI.value
            and agent_profile
            and kiro_install_predates_native_enforcement(agent_profile, allowed_tools)
        ):
            # Kiro enforces the installed agent JSON's `tools`. A file written
            # before CAO put the policy there still says ["*"], so the
            # restriction this request carries is not what the agent runs with.
            logger.warning(
                f"Terminal {terminal_id}: the installed Kiro agent '{agent_profile}' has "
                f'tools ["*"]; it predates native enforcement, so the requested '
                f"restriction {allowed_tools} is NOT applied. Treat this worker as "
                f"unrestricted until `cao install {agent_profile} --provider kiro_cli` is re-run."
            )

        # Step 3c: Create the tmux session/window and its registry row as ONE
        # atomic step, under the per-session-name lifecycle lock (#498). This
        # merges what used to be two separate steps -- the tmux create and the
        # metadata persist -- precisely because they must become visible together.
        #
        # Note that everything above is already outside the lock by
        # construction: profile load, Kiro engine resolution, tool-policy
        # resolution and worktree provisioning are either pure reads or concern
        # no session state, so the critical section stays down to the tmux +
        # registry writes that actually have to be atomic against a concurrent
        # teardown. That also lets the registry row be written exactly once, with
        # its final allowed_tools and engine, inside the section.
        #
        # Why locked: without mutual exclusion a concurrent delete_session for
        # the same name interleaves arbitrarily -- the teardown can decide the
        # name is dead and then kill the session this call just created, or
        # sweep between the tmux create and the row write, leaving one store
        # holding state the other doesn't know about. Serializing per NAME (not
        # globally) leaves creates of DIFFERENT sessions fully concurrent.
        #
        # Why on a worker thread: the lock is a threading primitive (the only
        # kind reachable from both this coroutine and the synchronous teardown
        # the API runs via to_thread -- see services/session_lock.py). Acquiring
        # it directly here would block the EVENT LOOP for as long as a
        # concurrent teardown of this name holds it (its tmux kill-verify poll
        # and per-terminal FIFO joins are each seconds), freezing every other
        # request. Off-loop, only this worker thread waits.
        #
        # Why the section ends here: provider.initialize() below can take tens
        # of seconds, and a teardown of this name must never queue behind an
        # agent launch. Everything inside is short, synchronous state mutation.
        deferred_delete_on_failure: bool | None = None
        session_incarnation_id: str | None = None

        def _create_session_or_window_locked() -> Tuple[str, bool, bool]:
            """Runs under the lifecycle lock on a worker thread.

            Returns (window_name, session_created, window_created) -- the caller
            needs all three: create_window may rename the window, and the
            failure path keys its cleanup off which one this call created.

            A failure after the backend create RETURNS rolls that resource back
            HERE, still holding the lock, before re-raising: on return the tmux
            session/window and its registry row both exist, and on such a raise
            neither does. Without that the outer flags below would still be False
            (they are only assigned from a successful RETURN), the `except`
            cleanup would tear down nothing, and the failure would leave a live
            tmux session with no registry row -- the exact divergence #498 exists
            to eliminate. A "database is locked" OperationalError out of
            db_create_terminal is an ordinary outcome under CAO's concurrent
            writers, so this is a routine path, not a pathological one.

            NOT covered (pre-existing, and deliberately not claimed): a failure
            INSIDE the backend create itself, after it has already made the tmux
            resource but before it returns. `TmuxClient.create_session` lands the
            session at `server.new_session(...)` and only then reads
            `session.windows[0].name` -- a fresh list-windows fetch that can raise
            (IndexError, or its own `ValueError` when the name is None), with
            `create_window` shaped the same way. That leaks a session/window this
            closure never learns about, so the rollback below cannot fire. Same
            gap existed pre-#498, which set its flag only after the create
            returned. Closing it needs the guard to extend INTO the backend
            create; tracked separately.

            Why the rollback is INSIDE the lock rather than reported out to the
            outer cleanup path: the lock's entire purpose is that, for one
            session NAME, create and teardown are serialized so the name is
            never observable half-built. Rolling back after release would reopen
            that window -- between the release and the kill, another thread can
            acquire the name and legitimately succeed (a new_session=False
            create adding a window to what it sees as a live session, or a
            teardown plus a fresh new_session=True create rebuilding the name) --
            and the late kill would then destroy an incarnation this call does
            not own, leaving ITS row pointing at nothing. Under the lock the
            name goes free -> free with no observable intermediate state.
            """
            nonlocal deferred_delete_on_failure, session_incarnation_id
            assert session_name is not None  # narrowed by the caller
            with session_lifecycle_lock(session_name):
                if new_session:
                    # Prevent duplicate sessions
                    if get_backend().session_exists(session_name):
                        raise ValueError(f"Session '{session_name}' already exists")

                    # Wipe any stale mapping a prior aborted lifecycle for this
                    # name may have left behind, so a no-env relaunch can't
                    # inherit them.
                    clear_session_env(session_name)

                    session_incarnation_id = uuid.uuid4().hex

                    # Create new tmux session with initial window
                    effective_env = dict(env_vars or {})
                    get_backend().create_session(
                        session_name,
                        window_name,
                        terminal_id,
                        resolved_working_directory,
                        extra_env=effective_env or None,
                    )
                    created_window_name = window_name
                    created_session, created_window = True, False
                else:
                    # Add window to existing session. Same lock, same reason: a
                    # window added mid-teardown would otherwise survive the
                    # session kill (or its row would be swept while the window
                    # lives on).
                    if not get_backend().session_exists(session_name):
                        raise ValueError(f"Session '{session_name}' not found")
                    # Resolve the durable logical session generation before
                    # creating a new backend object. Old retained tombstones may
                    # share this reusable label but cannot define the current
                    # incarnation.
                    session_incarnation_id = _resolve_existing_session_incarnation_locked(
                        session_name
                    )
                    # Merge explicit per-step env_vars over the persisted session
                    # env (per-step wins on conflict): workflow routing ids like
                    # CAO_WORKFLOW_RUN_ID must reach the window even when it
                    # joins an existing session (issue #408).
                    effective_env = {**get_session_env(session_name), **(env_vars or {})}
                    created_window_name = get_backend().create_window(
                        session_name,
                        window_name,
                        terminal_id,
                        resolved_working_directory,
                        extra_env=effective_env,
                    )
                    created_session, created_window = False, True

                if defer_init:
                    deferred_delete_on_failure = _deferred_failure_delete_from_creation(
                        caller_id, effective_env
                    )
                    if deferred_delete_on_failure is False:
                        # Restart recovery may be retried after server startup.
                        # Fence this external-owner row BEFORE it becomes
                        # visible in SQLite so a concurrent recovery pass never
                        # mistakes this process's live initialization for a
                        # crash-stranded row from the previous process.
                        _mark_deferred_init_external_owner_active(terminal_id)

                # From here the backend resource EXISTS, so every remaining step
                # is guarded: on failure the resource is rolled back under this
                # same lock before the exception leaves the closure. See the
                # docstring for why the rollback belongs here and not in the
                # caller's `except`.
                try:
                    if created_session:
                        # Drop rows a previous incarnation of this session name
                        # left behind. Inside the lock, so it can never race the
                        # row write of a concurrent create for the same name.
                        _purge_stale_session_rows_for_recreate(session_name)

                        if env_vars:
                            # Persist forwarded env only after the tmux session
                            # actually exists; rolled back below if a later step
                            # tears the session down again.
                            set_session_env(session_name, env_vars)

                    # Persist the registry row INSIDE the critical section so the
                    # tmux session/window and its row become visible together. A
                    # teardown that observes the new tmux state is then guaranteed
                    # to also observe the row, instead of killing a session whose
                    # row it cannot see and leaving it orphaned. The row carries
                    # the launch policy already resolved above (allowed_tools,
                    # engine), so API reads and snapshots report what was actually
                    # launched.
                    db_create_terminal(
                        terminal_id,
                        session_name,
                        created_window_name,
                        provider,
                        agent_profile,
                        allowed_tools,
                        caller_id=caller_id,
                        engine=resolved_engine.value if resolved_engine is not None else None,
                        group=group,
                        metadata=metadata,
                        working_directory=resolved_working_directory,
                        deferred_init_external_owner=bool(
                            defer_init and deferred_delete_on_failure is False
                        ),
                        session_incarnation_id=session_incarnation_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=request_fingerprint,
                        new_session_incarnation=created_session,
                    )
                except BaseException:
                    _clear_deferred_init_external_owner_active(terminal_id)
                    _roll_back_backend_create_locked(
                        session_name,
                        created_window_name,
                        created_session=created_session,
                    )
                    raise
                return created_window_name, created_session, created_window

        # The worker is UN-CANCELLABLE once dispatched: cancelling this await
        # detaches only the awaiter, while the thread proceeds to take the
        # lifecycle lock, create the backend session/window, and commit the
        # registry row — into the void. CancelledError is a BaseException, so
        # the `except Exception` cleanup below never sees it, and the outer
        # created-flags are still False so it would tear down nothing anyway:
        # a live session + row with no FIFO, no provider, and no caller that
        # knows the terminal exists — the exact divergence #498 eliminates.
        # So shield the worker, and on cancellation hand its outcome to a
        # compensator that rolls back whatever it built (under the lifecycle
        # lock) before letting the cancellation continue.
        create_worker = asyncio.ensure_future(asyncio.to_thread(_create_session_or_window_locked))
        try:
            window_name, session_created, window_created = await asyncio.shield(create_worker)
            rollback_window_name = window_name
        except asyncio.CancelledError:
            if not create_worker.cancelled():
                compensator = asyncio.ensure_future(
                    _finish_and_roll_back_cancelled_create(create_worker, session_name, terminal_id)
                )
                try:
                    await asyncio.shield(compensator)
                except asyncio.CancelledError:
                    # A repeat cancellation landed while the compensator ran;
                    # the shielded task still completes on the loop. The
                    # ORIGINAL cancellation is re-raised below either way.
                    pass
            _clear_deferred_init_external_owner_active(terminal_id)
            raise

        # Step 4/5: Set up the FIFO event-driven output pipeline for pipe-pane
        # backends (tmux). Event-inbox backends (herdr) deliver via their own
        # socket events and their pipe_pane is a no-op, so skip the FIFO there and
        # rely on the herdr inbox registration below.
        if not get_backend().supports_event_inbox():
            fifo_path = FIFO_DIR / f"{terminal_id}.fifo"

            # Reader must exist BEFORE pipe-pane starts so it captures from the
            # start. Enroll it in the pipe-pane liveness watchdog (issue #388):
            # supply a probe for tmux's live pane content and a re-arm that
            # re-attaches a stalled forwarder. The re-arm does stop-then-start,
            # NOT a bare pipe_pane() — a stalled pane still reports pane_pipe=1,
            # so the backend's ``pipe-pane -o`` toggle would just switch the
            # dead pipe OFF instead of restarting it.
            def _probe_pane(s=session_name, w=window_name) -> str:
                return get_backend().get_history(s, w, tail_lines=PIPE_LIVENESS_TAIL_LINES)

            def _rearm_pipe(s=session_name, w=window_name, p=str(fifo_path)) -> None:
                get_backend().stop_pipe_pane(s, w)
                get_backend().pipe_pane(s, w, p)

            fifo_manager.create_reader(terminal_id, pane_probe=_probe_pane, rearm=_rearm_pipe)

            # Configure pipe-pane to stream output to the FIFO. This enables
            # real-time event-driven processing via StatusMonitor and LogWriter
            # (LogWriter writes TERMINAL_LOG_DIR/{id}.log from the FIFO). A pane
            # has a single pipe-pane target, so we pipe ONLY to the FIFO.
            get_backend().pipe_pane(session_name, window_name, str(fifo_path))

            # Nudge the shell so it re-renders its prompt AFTER pipe-pane attaches.
            # pipe-pane only captures output produced after it starts; on a fast
            # shell the initial prompt is drawn before the pipe attaches, leaving
            # the StatusMonitor buffer empty so wait_for_shell() times out. A bare
            # Enter produces a fresh prompt line that flows through the pipe.
            get_backend().send_special_key(session_name, window_name, "Enter")

        # Step 6: Create and initialize the CLI provider
        # This starts the agent (e.g., runs "kiro-cli chat --agent developer").
        # Only runtime-prompt providers (Claude Code, Codex, Kimi) receive
        # the skill catalog here; Kiro (skill:// resources) and OpenCode
        # (OPENCODE_CONFIG_DIR/skills symlink) discover skills natively;
        # Copilot gets the catalog baked at install time.
        provider_instance = provider_manager.create_provider(
            provider,
            terminal_id,
            session_name,
            window_name,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
            model=model or (profile.model if profile else None),
            engine=resolved_engine,
            resume_session_id=resume_session_id,
        )

        # Deferred-init path: return fast so callers (e.g. MCP assign) do not
        # block on `provider.initialize()`. The remaining initialize + input
        # send runs as a background task, so two concurrent assigns can each
        # kick off their init in parallel. Kiro-cli 2.11's per-tool client
        # timeout (~120s observed) previously cancelled assign RPCs when init
        # took long enough to push the round-trip past that cap; deferring init
        # keeps the tool call under 2s.
        if defer_init:
            shell_command = None  # unknown until initialize() runs
            # Freeze lifecycle ownership while creation inputs are still in
            # hand. A deferred failure may coincide with a database outage, so
            # deciding who owns teardown by re-reading the terminal row later
            # is not reliable enough. Cross-node callback ownership is likewise
            # explicit in the launch env at this point.
            assert deferred_delete_on_failure is not None
            deferred_task = _schedule_deferred_init(
                provider_instance,
                terminal_id,
                initial_message,
                initial_message_orchestration_type,
                registry,
                initial_caller_id=caller_id,
                delete_on_failure=deferred_delete_on_failure,
            )
            # A handful of unit/integration seams patch the private scheduler
            # with a non-Task mock. Production always returns an asyncio.Task;
            # if the seam replaced it, release the pre-insert fence here so one
            # test cannot leak process-local recovery state into another.
            if deferred_delete_on_failure is False and not isinstance(deferred_task, asyncio.Task):
                _clear_deferred_init_external_owner_active(terminal_id)
        else:
            await provider_instance.initialize()

            # Persist shell_command baseline if the provider captured one
            shell_command = provider_instance.shell_baseline
            if not isinstance(shell_command, str):
                shell_command = None
            if shell_command:
                update_terminal_shell_command(terminal_id, shell_command)
            runtime_variant = getattr(provider_instance, "runtime_variant", None)
            if isinstance(runtime_variant, str) and runtime_variant:
                update_terminal_provider_variant(terminal_id, runtime_variant)

        # Build and return the Terminal object. In the deferred-init path the
        # provider is still initializing on a background task, so the terminal
        # is NOT ready for input yet — report UNKNOWN (not IDLE) so a client
        # can't mistake it for ready and send input early. Callers poll
        # GET /terminals/{id} for the live status once init completes. The
        # synchronous path has already reached IDLE by here.
        initial_status = TerminalStatus.UNKNOWN if defer_init else TerminalStatus.IDLE
        terminal = Terminal(
            id=terminal_id,
            name=window_name,
            provider=ProviderType(provider),
            session_name=session_name,
            agent_profile=agent_profile,
            caller_id=caller_id,
            allowed_tools=allowed_tools,
            engine=resolved_engine,
            shell_command=shell_command,
            group=group,
            metadata=metadata,
            deferred_init_failure=None,
            session_incarnation_id=session_incarnation_id,
            status=initial_status,
            last_active=datetime.now(),
        )

        logger.info(
            f"Created terminal: {terminal_id} in session: {session_name} (new_session={new_session})"
        )
        dispatch_plugin_event(
            registry,
            "post_create_terminal",
            PostCreateTerminalEvent(
                session_id=terminal.session_name,
                terminal_id=terminal.id,
                agent_name=terminal.agent_profile,
                provider=provider,
            ),
        )

        # Register with herdr inbox service for message delivery
        svc = get_herdr_inbox_service()
        if svc:
            try:
                pane_id = get_backend().get_pane_id(terminal_id, session_name, window_name)
                is_kiro = provider == ProviderType.KIRO_CLI.value
                svc.register_terminal(terminal_id, pane_id, is_kiro)
            except Exception as e:
                logger.warning(f"Failed to register terminal {terminal_id} with herdr inbox: {e}")
        return terminal

    except Exception as e:
        logger.error(f"Failed to create terminal: {e}")
        # Everything this call built is torn down by ONE owned operation
        # (``_roll_back_failed_create``) on ONE worker thread, and the await
        # is not cancellable: if the create request is cancelled while the
        # rollback waits on the lifecycle lock, the cleanup still runs to the
        # end and the cancellation is re-raised afterwards. Before this the
        # backend rollback was its own ``await`` inside this handler, so a
        # cancellation landing there unwound the handler with the row,
        # provider registration and worktree still in place.
        await _await_uncancellable(
            _roll_back_failed_create,
            terminal_id,
            session_name,
            rollback_window_name,
            session_created=session_created,
            window_created=window_created,
            worktree_repo_root=worktree_repo_root,
        )
        raise


def _notify_cross_node_caller(terminal_id: str, session_name: str, message: str) -> bool:
    """Deliver a deferred-init failure to a CROSS-NODE supervisor, if one is recorded.

    A worker created remotely (assign with ``target_host``) has no local
    ``caller_id`` row — its supervisor's terminal lives on ANOTHER node. The
    creating supervisor injected ``CAO_CALLBACK_URL`` / ``CAO_CALLBACK_TERMINAL_ID``
    into the session env at creation time (persisted via ``set_session_env``),
    so read them back and POST the failure through that callback endpoint.
    Elastic workers use the authenticated broker gateway; ordinary remote
    workers call the supervisor directly. Best-effort; returns True only when
    the remote POST succeeded. Note the session-env store is process-local -
    after a cao-server restart the route is gone and this degrades to the
    log-only path.
    """
    try:
        session_env = get_session_env(session_name)
        callback_url = (session_env.get(CALLBACK_URL_ENV) or "").rstrip("/")
        callback_terminal_id = session_env.get(CALLBACK_TERMINAL_ID_ENV)
        if not callback_url or not callback_terminal_id:
            return False
        response = requests.post(
            f"{callback_url}/terminals/{callback_terminal_id}/inbox/messages",
            params={"sender_id": terminal_id, "message": message},
            headers=elastic_worker_gateway_headers() or None,
            timeout=CROSS_NODE_NOTIFY_TIMEOUT,
        )
        response.raise_for_status()
        return True
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        logger.warning(
            "Deferred-init failure notify: cross-node delivery for worker %s failed: %s",
            terminal_id,
            exc,
        )
        return False


def _notify_elastic_terminal_ended(terminal_id: str) -> None:
    """Tell the broker that a one-shot worker terminal ended without completion."""
    worker_id = os.environ.get("CAO_ELASTIC_WORKER_ID", "").strip()
    broker_url = os.environ.get("CAO_ELASTIC_BROKER_URL", "").strip().rstrip("/")
    release_token = os.environ.get("CAO_ELASTIC_RELEASE_TOKEN", "").strip()
    if not worker_id or not broker_url or not release_token:
        return
    try:
        response = requests.post(
            f"{broker_url}/workers/{worker_id}/terminal-ended",
            json={"terminal_id": terminal_id},
            headers={"X-CAO-Release-Token": release_token},
            timeout=5.0,
        )
        # A completion or another teardown signal may already have released the
        # lease. In that case this notification is redundant.
        if response.status_code != 404:
            response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning(
            "Could not report terminal %s ending for elastic worker %s: %s",
            terminal_id,
            worker_id,
            exc,
        )


def _notify_caller_of_deferred_failure(
    terminal_id: str,
    message: str,
    registry: "PluginRegistry | None",
    delete_worker: bool,
) -> None:
    """Make a deferred-init failure observable to the supervisor that assigned
    the worker, then optionally tear the worker down.

    Runs in a worker thread (blocking DB + tmux I/O). The supervisor is the
    worker's ``caller_id``; we enqueue a PENDING inbox message to it so the
    failure surfaces as the supervisor's next input instead of leaving it to
    wait forever on a callback that will never come. When there is no LOCAL
    caller row, the worker may have been created by a CROSS-NODE supervisor
    (assign with ``target_host``) — in that case the failure is POSTed to the
    supervisor node recorded in the session's callback env (see
    ``_notify_cross_node_caller``). Every step is best-effort and
    independently guarded — a failure to notify must not prevent teardown,
    and a failure to tear down must not crash the background task.
    """
    caller_id = None
    session_name = None
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            caller_id = metadata.get("caller_id")
            session_name = metadata.get("tmux_session")
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        logger.warning(
            "Deferred-init failure notify: could not read metadata for %s: %s",
            terminal_id,
            exc,
        )

    if caller_id:
        try:
            create_inbox_message(sender_id=terminal_id, receiver_id=caller_id, message=message)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning(
                "Deferred-init failure notify: could not enqueue inbox message to "
                "caller %s for worker %s: %s",
                caller_id,
                terminal_id,
                exc,
            )
    elif session_name and _notify_cross_node_caller(terminal_id, session_name, message):
        pass  # delivered to the cross-node supervisor's inbox
    else:
        logger.warning(
            "Deferred-init failure for %s has no caller_id to notify; failure is " "log-only.",
            terminal_id,
        )

    if delete_worker:
        try:
            # Pass registry so post_kill_terminal hooks fire — parity with the
            # DELETE endpoint and agent_step teardown.
            delete_terminal(terminal_id, registry=registry)
        except Exception as exc:  # noqa: BLE001 — teardown is best-effort
            logger.warning(
                "Deferred-init failure: teardown of worker %s failed (zombie "
                "window may remain): %s",
                terminal_id,
                exc,
            )
        # This process is PID 1 in an elastic worker pod, so deleting its tmux
        # terminal does not change the pod phase. Tell the broker explicitly or
        # the failed lease remains Ready until its completion timeout.
        _notify_elastic_terminal_ended(terminal_id)


def _sanitize_deferred_failure_message(message: str) -> str:
    """Bound/control-clean an error string before persisting it in terminal metadata."""

    message = str(message)[:_DEFERRED_INIT_FAILURE_SCAN_MAX]
    cleaned = "".join(
        ch
        for ch in message
        if (ch in {"\n", "\t"} or ord(ch) >= 32) and not 0xD800 <= ord(ch) <= 0xDFFF
    ).strip()
    return cleaned[:_DEFERRED_INIT_FAILURE_MESSAGE_MAX]


def _deferred_failure_fallback_path(terminal_id: str) -> Path:
    return TERMINAL_LOG_DIR / f"{terminal_id}{_DEFERRED_INIT_FAILURE_FALLBACK_SUFFIX}"


def _deferred_init_complete_fallback_path(terminal_id: str) -> Path:
    return TERMINAL_LOG_DIR / f"{terminal_id}{_DEFERRED_INIT_COMPLETE_FALLBACK_SUFFIX}"


def _fsync_parent_directory(path: Path) -> None:
    """Make an atomic rename durable across host crashes, not only process crashes."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(str(path.parent), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_deferred_sidecar(target: Path, payload: bytes) -> None:
    """Publish a complete, fsynced sidecar; keep prior truth on a failed write."""

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            written = 0
            while written < len(payload):
                count = os.write(fd, payload[written:])
                if count <= 0:
                    raise OSError("short write while persisting deferred-init sidecar")
                written += count
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        if tmp.read_bytes() != payload:
            raise OSError("deferred-init sidecar bytes do not match the complete payload")
        os.replace(tmp, target)
        os.chmod(target, 0o600)
        _fsync_parent_directory(target)
    finally:
        tmp.unlink(missing_ok=True)


def _write_deferred_failure_fallback(terminal_id: str, failure: dict[str, Any]) -> bool:
    """Atomically persist deferred failure outside SQLite as a last-resort tombstone."""

    try:
        target = _deferred_failure_fallback_path(terminal_id)
        payload = json.dumps(failure, ensure_ascii=False, sort_keys=True).encode("utf-8")
        _write_deferred_sidecar(target, payload)
        return True
    except Exception as exc:  # noqa: BLE001 — fallback is best-effort but explicit
        logger.error("Could not persist deferred-init fallback for %s: %s", terminal_id, exc)
        return False


def _publish_deferred_failure_fallback(terminal_id: str, failure: dict[str, Any]) -> bool:
    """Publish a failure sidecar only while its terminal row is not proven gone.

    The existence check and atomic sidecar rename share the same lock as
    ``delete_terminal_row``. A concurrent DELETE therefore orders either before
    this function (row missing => no new sidecar) or after it (DELETE removes
    the just-published sidecar). A transient DB read failure is not proof that
    the row disappeared, so fail toward publishing the external-owner evidence.
    """

    with _DEFERRED_INIT_SIDECAR_LOCK:
        try:
            if get_terminal_metadata(terminal_id) is None:
                return False
        except Exception:  # noqa: BLE001 — DB outage is why this fallback exists
            pass
        return _write_deferred_failure_fallback(terminal_id, failure)


def _read_deferred_failure_fallback(terminal_id: str) -> dict[str, Any] | None:
    try:
        path = _deferred_failure_fallback_path(terminal_id)
        raw = path.read_bytes()
        # Refuse an unexpectedly large/corrupt fallback before JSON allocation.
        if len(raw) > 65536:
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def get_deferred_init_failure(terminal_id: str, candidate: Any = None) -> dict[str, Any] | None:
    """Return authoritative deferred-init failure from DB value or fallback sidecar."""

    if isinstance(candidate, dict):
        return candidate
    return _read_deferred_failure_fallback(terminal_id)


def _delete_deferred_failure_fallback(terminal_id: str) -> None:
    try:
        _deferred_failure_fallback_path(terminal_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove deferred-init fallback for %s: %s", terminal_id, exc)


def _write_deferred_init_complete_fallback(terminal_id: str) -> bool:
    """Durably record successful deferred init when the DB ownership clear is unavailable."""

    try:
        target = _deferred_init_complete_fallback_path(terminal_id)
        _write_deferred_sidecar(target, b"complete\n")
        return True
    except Exception as exc:  # noqa: BLE001 — fail-closed retention is safer than deletion
        logger.error(
            "Could not persist deferred-init success fallback for %s: %s", terminal_id, exc
        )
        return False


def _publish_deferred_init_complete_fallback(terminal_id: str) -> bool:
    """Publish successful-init fallback without racing terminal deletion."""

    with _DEFERRED_INIT_SIDECAR_LOCK:
        try:
            if get_terminal_metadata(terminal_id) is None:
                return False
        except Exception:  # noqa: BLE001 — preserve success truth through DB outage
            pass
        return _write_deferred_init_complete_fallback(terminal_id)


def _has_deferred_init_complete_fallback(terminal_id: str) -> bool:
    try:
        return _deferred_init_complete_fallback_path(terminal_id).is_file()
    except OSError:
        return False


def _delete_deferred_init_complete_fallback(terminal_id: str) -> None:
    try:
        _deferred_init_complete_fallback_path(terminal_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not remove deferred-init success fallback for %s: %s", terminal_id, exc
        )


def should_retain_deferred_failure_tombstone(
    terminal_id: str, metadata: dict[str, Any] | None = None
) -> bool:
    """Return whether lifecycle cleanup must retain this terminal's registry row.

    A durable failure (DB or sidecar) is always retained. Creation-time external
    ownership is retained only while deferred init is still pending. If clearing
    that DB bit failed after a successful init, the completion sidecar is the
    durable success override and we opportunistically repair the stale bit.
    """

    if metadata is None:
        metadata = get_terminal_metadata(terminal_id)
    metadata = metadata or {}
    if get_deferred_init_failure(terminal_id, metadata.get("deferred_init_failure")) is not None:
        return True
    if not metadata.get("deferred_init_external_owner"):
        return False
    if not _has_deferred_init_complete_fallback(terminal_id):
        return True
    try:
        if update_terminal_deferred_init_external_owner(terminal_id, False):
            _delete_deferred_init_complete_fallback(terminal_id)
    except Exception as exc:  # noqa: BLE001 — the sidecar remains authoritative
        logger.debug("Deferred-init success ownership repair for %s deferred: %s", terminal_id, exc)
    return False


def _purge_stale_session_rows_for_recreate(session_name: str) -> None:
    """Remove stale rows for a reused session name without erasing tombstones.

    ``new_session=True`` historically bulk-deleted every DB row sharing the
    session label.  External-observer deferred failures deliberately outlive the
    provider session, so a later replacement session may legitimately reuse the
    same label while those rows still carry unacknowledged failure truth.  Keep
    such rows and remove only ordinary stale records; row deletion goes through
    ``delete_terminal_row`` so any lifecycle sidecars are reclaimed too.
    """

    for terminal in list_terminals_by_session(session_name):
        terminal_id = str(terminal["id"])
        metadata = get_terminal_metadata(terminal_id)
        if should_retain_deferred_failure_tombstone(terminal_id, metadata):
            logger.info(
                "Preserving deferred-init tombstone %s while recreating session %s",
                terminal_id,
                session_name,
            )
            continue
        delete_terminal_row(terminal_id, metadata, registry=None)


def _resolve_existing_session_incarnation_locked(session_name: str) -> str:
    """Resolve/backfill the durable incarnation of an already-live session.

    Caller must hold the per-session lifecycle lock. The durable pointer takes
    precedence over terminal state, including when every current row failed.
    For older sessions without a pointer, active rows or exact backend identity
    proof establish membership before an atomic pointer/backfill commit.

    Legacy active rows with no incarnation are backfilled atomically. If one
    active row already carries an incarnation, all legacy active siblings are
    joined to that same value. Multiple distinct active incarnation ids are a
    corrupt lifecycle state and fail closed before a new backend window is
    created.
    """

    current = get_session_incarnation(session_name)
    if current is not None:
        return current

    rows = list_terminals_by_session(session_name)
    active_rows: list[dict[str, Any]] = []
    legacy_failed_rows: list[dict[str, Any]] = []
    for row in rows:
        failure = get_deferred_init_failure(str(row["id"]), row.get("deferred_init_failure"))
        if failure is None and not row.get("deferred_init_runtime_reclaimed"):
            active_rows.append(row)
        elif (
            failure is not None
            and not row.get("deferred_init_runtime_reclaimed")
            and not row.get("session_incarnation_id")
        ):
            legacy_failed_rows.append(row)

    # A legacy failed sibling can still be present beside a healthy conductor.
    # Backfill it only after proving exact membership, never by its session label.
    if active_rows:
        for row in legacy_failed_rows:
            exact = get_backend().cleanup_terminal_exact(
                str(row["id"]), session_name, row.get("tmux_window"), close=False
            )
            if exact.outcome == TerminalCleanupOutcome.UNKNOWN:
                raise TerminalRecordCorruptError(
                    f"Could not verify legacy failed sibling in {session_name!r}"
                )
            if exact.outcome == TerminalCleanupOutcome.STILL_PRESENT:
                active_rows.append(row)

    # Sessions predating the durable pointer may have only failed terminals.
    # Failure is not evidence that their backend session was replaced. Use the
    # exact terminal identity to prove membership without closing any window.
    if not active_rows and rows:
        for row in rows:
            if row.get("deferred_init_runtime_reclaimed"):
                continue
            exact = get_backend().cleanup_terminal_exact(
                str(row["id"]), session_name, row.get("tmux_window"), close=False
            )
            if exact.outcome == TerminalCleanupOutcome.UNKNOWN:
                raise TerminalRecordCorruptError(
                    f"Could not verify session incarnation for {session_name!r}"
                )
            if exact.outcome == TerminalCleanupOutcome.STILL_PRESENT:
                active_rows.append(row)
        if not active_rows:
            raise TerminalRecordCorruptError(
                f"No terminal proves the current incarnation of session {session_name!r}"
            )

    incarnation_ids = {
        str(row["session_incarnation_id"])
        for row in active_rows
        if row.get("session_incarnation_id")
    }
    if len(incarnation_ids) > 1:
        raise TerminalRecordCorruptError(
            f"Session {session_name!r} has multiple live incarnation ids: "
            f"{sorted(incarnation_ids)!r}"
        )

    incarnation_id = next(iter(incarnation_ids)) if incarnation_ids else uuid.uuid4().hex
    legacy_ids = [str(row["id"]) for row in active_rows if not row.get("session_incarnation_id")]
    if not update_terminals_session_incarnation(
        legacy_ids, incarnation_id, session_name=session_name
    ):
        raise TerminalRecordCorruptError(
            f"Could not atomically backfill session incarnation for "
            f"{session_name!r}: {legacy_ids!r}"
        )
    return incarnation_id


def _deferred_failure_delete_worker(terminal_id: str) -> bool:
    """Whether CAO, rather than an external lifecycle owner, must tear down.

    Local callers, cross-node callbacks and elastic workers all have an explicit
    CAO-owned failure-delivery/release contract. A terminal with none of those
    is operator/external-observer owned (Bridge is the primary example): delete
    would erase the only durable failure surface and turn a known init failure
    into a generic 404/orphaned observation.
    """

    try:
        metadata = get_terminal_metadata(terminal_id) or {}
    except Exception:  # noqa: BLE001 — unknown ownership must retain evidence
        return bool(os.environ.get("CAO_ELASTIC_WORKER_ID"))
    if metadata.get("caller_id"):
        return True
    session_name = metadata.get("tmux_session")
    if session_name:
        try:
            session_env = get_session_env(str(session_name))
            if session_env.get(CALLBACK_URL_ENV) and session_env.get(CALLBACK_TERMINAL_ID_ENV):
                return True
        except Exception:  # noqa: BLE001 — fail closed toward retaining evidence
            pass
    return bool(os.environ.get("CAO_ELASTIC_WORKER_ID"))


def _deferred_failure_delete_from_creation(
    caller_id: str | None, env_vars: Optional[dict[str, str]]
) -> bool:
    """Freeze deferred-failure ownership from terminal-creation inputs."""

    return (
        bool(caller_id)
        or bool(
            env_vars and env_vars.get(CALLBACK_URL_ENV) and env_vars.get(CALLBACK_TERMINAL_ID_ENV)
        )
        or bool(os.environ.get("CAO_ELASTIC_WORKER_ID"))
    )


async def _clear_deferred_init_external_owner(terminal_id: str) -> None:
    """Clear creation-time retention once deferred init becomes a normal live terminal."""

    for attempt in range(1, _DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS + 1):
        try:
            updated = await asyncio.to_thread(
                update_terminal_deferred_init_external_owner, terminal_id, False
            )
            if updated:
                await asyncio.to_thread(_delete_deferred_init_complete_fallback, terminal_id)
                return
            # The update helper returns False only after a successful DB query
            # proves the terminal row no longer exists. Do not turn an explicit
            # concurrent DELETE into a new orphan completion sidecar.
            await asyncio.to_thread(_delete_deferred_init_complete_fallback, terminal_id)
            return
        except Exception as exc:  # noqa: BLE001 — retry a transient DB failure
            logger.warning(
                "Deferred-init ownership clear attempt %d/%d for %s failed: %s",
                attempt,
                _DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS,
                terminal_id,
                exc,
            )
        if attempt < _DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS:
            await asyncio.sleep(0.05 * attempt)
    logger.error(
        "Could not clear deferred-init external ownership for successful terminal %s; "
        "persisting successful-init sidecar fallback",
        terminal_id,
    )
    await asyncio.to_thread(_publish_deferred_init_complete_fallback, terminal_id)


async def recover_interrupted_deferred_init_external_owners() -> bool:
    """Turn restart-stranded external deferred inits into durable failures.

    Deferred-init tasks live only in the cao-server process.  After a restart,
    any row that still carries the creation-time external-owner bit can no
    longer make progress by itself.  Preserve an already-persisted failure (or
    sidecar), repair a successful-init completion sidecar, and otherwise publish
    an explicit ``interrupted_init`` failure so external observers see ERROR
    instead of a permanently pending tombstone/404 ambiguity.
    """

    # A False return means the durable enumeration was incomplete and a
    # startup retry must run. Current-process active workers are skipped below.
    try:
        terminal_ids = await asyncio.to_thread(
            list_pending_deferred_init_external_owner_terminal_ids
        )
    except Exception as exc:  # noqa: BLE001 — startup remains resilient
        logger.warning("Could not list interrupted deferred-init terminals: %s", exc)
        return False

    complete_scan = True
    for terminal_id in terminal_ids:
        if _is_deferred_init_external_owner_active(terminal_id):
            logger.debug(
                "Skipping restart recovery for current-process deferred-init terminal %s",
                terminal_id,
            )
            continue
        try:
            metadata = await asyncio.to_thread(get_terminal_metadata, terminal_id)
        except Exception as exc:  # noqa: BLE001 — retry on next restart/cleanup pass
            complete_scan = False
            logger.warning(
                "Could not inspect deferred-init terminal %s during restart recovery: %s",
                terminal_id,
                exc,
            )
            continue
        if not metadata:
            continue
        if (
            get_deferred_init_failure(terminal_id, metadata.get("deferred_init_failure"))
            is not None
        ):
            continue
        if _has_deferred_init_complete_fallback(terminal_id):
            await _clear_deferred_init_external_owner(terminal_id)
            continue
        await _surface_deferred_init_failure(
            terminal_id,
            kind="interrupted_init",
            message=(
                f"Worker {terminal_id} deferred initialization was interrupted by "
                "cao-server restart before completion. Re-assign the task."
            ),
            exception_type="ServerRestart",
            registry=None,
            delete_on_failure=False,
        )
    return complete_scan


async def retry_interrupted_deferred_init_external_owners(
    *,
    initial_delay: float = 1.0,
    max_delay: float = 30.0,
) -> None:
    """Retry an incomplete startup recovery scan until one full pass succeeds.

    This is intentionally not an eternal polling loop. Once the previous
    process's durable rows have been scanned successfully there is no restart
    cohort left to discover. Retrying only after an incomplete pass closes the
    transient SQLite hole without continuously reclassifying live workers.
    """

    delay = max(0.0, float(initial_delay))
    cap = max(delay, float(max_delay))
    while True:
        await asyncio.sleep(delay)
        try:
            if await recover_interrupted_deferred_init_external_owners():
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — maintenance must remain best-effort
            logger.warning("Deferred-init restart recovery retry failed: %s", exc)
        delay = min(cap, max(0.1, delay * 2 if delay else 0.1))


def _persist_deferred_init_failure(
    terminal_id: str,
    *,
    kind: str,
    message: str,
    exception_type: str | None = None,
) -> dict[str, Any] | None:
    """Persist one server-owned deferred-init failure outside consumer metadata."""

    failure = {
        "phase": "deferred_init",
        "kind": str(kind),
        "message": _sanitize_deferred_failure_message(message),
    }
    if exception_type:
        failure["exception_type"] = str(exception_type)
    if not update_terminal_deferred_init_failure(terminal_id, failure):
        return None
    _delete_deferred_failure_fallback(terminal_id)
    return failure


async def _surface_deferred_init_failure(
    terminal_id: str,
    *,
    kind: str,
    message: str,
    registry: PluginRegistry | None,
    exception_type: str | None = None,
    delete_on_failure: bool | None = None,
) -> bool:
    """Persist/notify one init failure and apply lifecycle ownership.

    Returns True when CAO owns teardown, False when an external observer owns
    the retained terminal. Failure metadata is written before either branch so
    GET /terminals can remain an authoritative error surface across restarts.
    """

    delete_worker = (
        bool(delete_on_failure)
        if delete_on_failure is not None
        else await asyncio.to_thread(_deferred_failure_delete_worker, terminal_id)
    )
    persisted = False
    terminal_missing = False
    failure_payload = {
        "phase": "deferred_init",
        "kind": str(kind),
        "message": _sanitize_deferred_failure_message(message),
    }
    if exception_type:
        failure_payload["exception_type"] = str(exception_type)
    for attempt in range(1, _DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS + 1):
        try:
            failure = await asyncio.to_thread(
                _persist_deferred_init_failure,
                terminal_id,
                kind=kind,
                message=message,
                exception_type=exception_type,
            )
            persisted = failure is not None
            if persisted:
                break
            # None means the DB query succeeded but the row is gone. A
            # concurrent explicit DELETE owns the terminal now; publishing a
            # failure sidecar afterwards would create an unreferenced tombstone.
            terminal_missing = True
            break
        except Exception as exc:  # noqa: BLE001 — lifecycle action must still run
            logger.warning(
                "Deferred-init failure persistence attempt %d/%d for %s failed: %s",
                attempt,
                _DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS,
                terminal_id,
                exc,
            )
        if attempt < _DEFERRED_INIT_FAILURE_PERSIST_ATTEMPTS:
            await asyncio.sleep(0.05 * attempt)
    if not persisted:
        logger.error(
            "Deferred-init failure for %s could not be persisted in SQLite",
            terminal_id,
        )
        # CAO-owned failures are immediately notified/teardown and do not need a
        # long-lived tombstone. External owners do: if SQLite is temporarily
        # unavailable, persist a small sidecar so GET /terminals remains able to
        # surface ERROR after the DB recovers or the server restarts.
        if not delete_worker and not terminal_missing:
            await asyncio.to_thread(
                _publish_deferred_failure_fallback, terminal_id, failure_payload
            )
    notification = _sanitize_deferred_failure_message(message)
    if delete_worker:
        notification += " The failed worker terminal will be torn down."
    else:
        notification += " The failed worker terminal is retained for its external lifecycle owner."
    await asyncio.to_thread(
        _notify_caller_of_deferred_failure,
        terminal_id,
        notification,
        registry,
        delete_worker,
    )
    return delete_worker


# --- deferred-init submit verification ----------------------------------------
# send_input delivers via paste-buffer → fixed sleep → Enter (clients/tmux.py).
# That fixed sleep only guesses when the TUI is input-ready; when it guesses
# wrong the Enter (or the whole paste) is dropped and the message sits
# unsubmitted in the prompt box. In the deferred-init path nobody blocks on
# completion, so a dropped submit leaves the worker IDLE forever with the task
# never started and NO exception raised — the supervisor then waits on a
# callback that can never arrive. Confirm the worker actually began processing
# and re-submit if it did not.
_DEFERRED_SUBMIT_CONFIRM_TIMEOUT = 8.0  # per-attempt wait for the PROCESSING edge
_DEFERRED_SUBMIT_MAX_RESUBMITS = 3
# Statuses proving the worker accepted the task (left the ready IDLE state).
# WAITING_USER_ANSWER counts: the worker consumed the input and is now asking.
_DEFERRED_STARTED_STATUSES = {
    TerminalStatus.PROCESSING,
    TerminalStatus.COMPLETED,
    TerminalStatus.WAITING_USER_ANSWER,
}


def _worker_is_started_direct(terminal_id: str, provider) -> bool:
    """Direct visible-screen status check bypassing the event-driven status cache.

    The deferred-init retry loop polls ``status_monitor.get_status()`` which
    returns the **cached** status updated only by the event-driven pipeline
    (pyte screener at rising-edge/quiescence edges). When that lags behind
    reality the cached status stays IDLE even though the worker already
    transitioned to PROCESSING.

    Kimi requires evidence from the rolling byte buffer cleared by send_input
    before dispatch. Capture-pane history can retain a previous completed turn,
    so it is never supplied to Kimi's execution-evidence latch. Other opted-in
    providers use their existing live capture-pane status contract.

    Only providers that set ``supports_direct_status_probe = True`` should
    be passed to this function; the ``get_status()`` contract for other
    providers (e.g. kiro_cli, antigravity_cli, cursor_cli) relies on
    dispatch bookkeeping and cannot distinguish IDLE from COMPLETED on a
    rendered capture-pane snapshot. Kimi opts in with a separate evidence hook.
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return False
        session_name = metadata.get("tmux_session")
        window_name = metadata.get("tmux_window")
        if not session_name or not window_name:
            return False
        if getattr(provider, "requires_execution_evidence", False) is True:
            # The evidence parser mutates a provider-side acceptance latch.  It
            # must therefore run atomically with the StatusMonitor buffer epoch
            # reset performed by send_input; sampling with get_buffer() and
            # mutating later lets an old in-flight probe certify a newer turn.
            if status_monitor.probe_execution_evidence(terminal_id, provider):
                return True
            # Kimi deliberately refuses cached PROCESSING/COMPLETED as pickup
            # evidence because those statuses can be retained from an older
            # turn. ERROR is different: after this dispatch it is a terminal
            # provider verdict, not evidence of successful work, and the only
            # safe recovery is to stop re-delivery and let the caller observe
            # the error. This also covers providers that adopt the same strict
            # execution-evidence contract later.
            return status_monitor.get_status(terminal_id) == TerminalStatus.ERROR
        output = get_backend().get_history(session_name, window_name, tail_lines=200)
        status = provider.get_status(output)
    except Exception:
        logger.debug(
            "Direct status probe for %s failed (falling through to cached path)",
            terminal_id,
            exc_info=True,
        )
        return False
    return status in _DEFERRED_STARTED_STATUSES


def _capture_current_composer_region(terminal_id: str) -> Optional[str]:
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return None
        provider = provider_manager.get_provider(terminal_id)
        if provider is None:
            return None
        backend = get_backend()
        viewport = backend.get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            strip_escapes=True,
            visible_only=True,
        )
        return provider.extract_current_composer(viewport)
    except Exception:
        logger.debug("Failed to capture current composer for %s", terminal_id, exc_info=True)
        return None


def _normalized_box_text(text: str) -> str:
    return "".join(character.casefold() for character in text if character.isalnum())


def _message_visible_in_box(terminal_id: str, message: str) -> bool:
    """True when the current editable composer contains the message text.

    A bare Enter is safe only when the provider extracts the bounded trailing
    message probe from its current composer. The pane can retain historical
    deliveries, so cursor-adjacent or transcript text is not an input boundary.
    A miss takes the safer full-redelivery path.
    """
    normalized_message = _normalized_box_text(message)
    probe = normalized_message[-_CURRENT_COMPOSER_PROBE_MAX_CHARS:]
    if len(probe) < 8:
        return False

    composer = _capture_current_composer_region(terminal_id)
    if composer is None:
        return False
    return probe in _normalized_box_text(composer)


def redeliver_dropped_message(
    terminal_id: str,
    message: str,
    attempt: int,
    provider=None,
    *,
    full_resend_requires_probe: bool = False,
    registry: "PluginRegistry | None" = None,
    sender_id: Optional[str] = None,
    orchestration_type: Optional[OrchestrationType] = None,
) -> bool:
    """Re-deliver a message the TUI never accepted (blocking; to_thread it).

    One attempt of the confirm-and-redeliver loop shared by the deferred-init
    path (#479) and the synchronous step path (#562). First, when the provider
    opts in via ``supports_direct_status_probe``, a live capture-pane check
    catches a worker that IS already running but whose cached status lags
    behind (#496) — returns True (started) without sending anything. A caller
    that already holds the provider instance passes it; otherwise it is
    resolved from the registry, best-effort (a resolution failure means no
    probe, never a failed redelivery). Then the box check picks the
    redelivery: if the delivered text is still visible in the current composer
    only the Enter was swallowed (send a bare Enter); if it is absent the
    paste itself was dropped (re-deliver in full). See
    ``_message_visible_in_box`` for why guessing wrong must be avoided.

    ``full_resend_requires_probe`` gates the full re-send on the provider
    being probe-capable. Reason: the current-composer check cannot establish
    that a prompt which has already scrolled away was processed, and under the
    pyte screen path status detection runs only
    at rising-edge/quiescence — a whole turn can process inside one burst,
    leaving the cached status IDLE throughout while the prompt scrolls off —
    so for a provider without a direct status probe there is no way to
    distinguish "paste dropped" from "worker already ran" — and re-pasting
    the full message into a working worker silently runs the task twice.
    When the gate is on and the provider is not probe-capable, the
    bare-Enter branch (which cannot duplicate a task) is still taken
    whenever the text is visible; otherwise nothing is sent and False is
    returned, leaving the caller's own timeout to classify the outcome. The
    deferred-init path keeps the default (off) because it loops on
    ``wait_until_status`` for the PROCESSING edge before ever reaching here,
    and that pre-existing behavior is unchanged by this helper's extraction.

    The full re-send is forwarded to ``send_input`` as ``redelivery=True``: it
    repeats the dispatch CAO is already waiting on, so the provider must treat
    it as another delivery attempt of the same logical turn rather than as a
    newly dispatched turn.

    Returns True when the worker was found already started and nothing was
    sent; False when a redelivery was attempted (or deliberately skipped).
    """
    if provider is None:
        try:
            provider = provider_manager.get_provider(terminal_id)
        except Exception:
            provider = None
    probe_capable = provider is not None and getattr(
        provider, "supports_direct_status_probe", False
    )
    if probe_capable:
        if _worker_is_started_direct(terminal_id, provider):
            return True
    if _message_visible_in_box(terminal_id, message):
        logger.warning(
            "Delivery to %s unsubmitted (Enter swallowed); " "re-submitting via Enter (attempt %d)",
            terminal_id,
            attempt,
        )
        send_special_key(terminal_id, "Enter")
        return False
    if getattr(provider, "execution_evidence_ambiguous", False) is True:
        logger.warning(
            "Delivery to %s is unconfirmed after execution context was evicted; "
            "skipping full re-send to avoid a duplicate task (attempt %d)",
            terminal_id,
            attempt,
        )
        return False
    if full_resend_requires_probe and not probe_capable:
        # No probe → cannot rule out a working worker whose prompt left the
        # pane; a full re-send could silently duplicate the task. Skip the
        # re-send and let the caller's own deadline classify the outcome.
        logger.warning(
            "Delivery to %s not accepted and provider is not probe-capable; "
            "skipping full re-send to avoid a duplicate task (attempt %d)",
            terminal_id,
            attempt,
        )
        return False
    logger.warning(
        "Delivery to %s not accepted (paste dropped); " "re-delivering message (attempt %d)",
        terminal_id,
        attempt,
    )
    send_input(
        terminal_id,
        message,
        registry=registry,
        sender_id=sender_id,
        orchestration_type=orchestration_type,
        redelivery=True,
    )
    return False


async def _confirm_worker_started_or_resubmit(
    terminal_id: str,
    message: str,
    registry: "PluginRegistry | None",
    sender_id: Optional[str],
    orchestration_type: Optional[OrchestrationType],
    provider=None,
) -> bool:
    """Confirm a deferred-init worker began processing; re-submit if not.

    Returns True once the terminal reaches a started status, False if it is
    still stuck at IDLE after all resubmit attempts. Blocking tmux/DB I/O runs
    off the loop via to_thread so concurrent deferred inits aren't frozen.
    """
    if provider is None:
        try:
            provider = provider_manager.get_provider(terminal_id)
        except Exception:
            provider = None

    async def wait_for_start() -> bool:
        if getattr(provider, "requires_execution_evidence", False) is True:
            # Cached PROCESSING/COMPLETED may be dispatch-derived too. Poll
            # independent evidence for the full grace period before resending.
            deadline = time.monotonic() + _DEFERRED_SUBMIT_CONFIRM_TIMEOUT
            while True:
                if await asyncio.to_thread(_worker_is_started_direct, terminal_id, provider):
                    return True
                if time.monotonic() >= deadline:
                    return False
                await asyncio.sleep(0.5)
        return await wait_until_status(
            terminal_id,
            _DEFERRED_STARTED_STATUSES,
            timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
            polling_interval=0.5,
        )

    if await wait_for_start():
        return True

    for attempt in range(1, _DEFERRED_SUBMIT_MAX_RESUBMITS + 1):
        # The redelivery decision (box check + #496's direct-probe guard for
        # providers that opt in) lives in ``redeliver_dropped_message`` —
        # shared with the synchronous step path (#562).
        already_started = await asyncio.to_thread(
            redeliver_dropped_message,
            terminal_id,
            message,
            attempt,
            provider,
            registry=registry,
            sender_id=sender_id,
            orchestration_type=orchestration_type,
        )
        if already_started:
            return True
        if await wait_for_start():
            return True

    return False


def _schedule_deferred_init(
    provider_instance,
    terminal_id: str,
    initial_message: Optional[str],
    orchestration_type: Optional[OrchestrationType],
    registry: PluginRegistry | None,
    *,
    initial_caller_id: Optional[str] = None,
    delete_on_failure: bool | None = None,
) -> asyncio.Task | None:
    """Kick off provider.initialize() in the background and, on success,
    deliver the initial message via send_input.

    Runs as an asyncio task on the running event loop so it doesn't block
    the caller. Because assign() has already returned success=True by the
    time this runs, a failure here must be made OBSERVABLE to the supervisor
    rather than silently swallowed — otherwise the supervisor waits forever
    on a callback that can never arrive and a later inspect 404s. On failure
    we notify the caller's inbox (best-effort) and then tear the worker down.

    ``TerminalInputBlockedError`` (the worker is parked on a WAITING_USER_ANSWER
    prompt right after init) is NOT a teardown case: the worker is alive and
    answerable via answer_user_prompt, so we leave it in place and only log.
    """

    async def _run() -> None:
        caller_id: Optional[str] = initial_caller_id
        try:
            await provider_instance.initialize()
            shell_command = provider_instance.shell_baseline
            if isinstance(shell_command, str) and shell_command:
                update_terminal_shell_command(terminal_id, shell_command)
            runtime_variant = getattr(provider_instance, "runtime_variant", None)
            if isinstance(runtime_variant, str) and runtime_variant:
                update_terminal_provider_variant(terminal_id, runtime_variant)
            if initial_message:
                # For assign/handoff the sender is the CALLER (the supervisor),
                # not this MCP server; _assign_impl on the MCP-server side already
                # embedded the callback instructions into initial_message. The
                # deferred path is also reached from POST /sessions?initial_message=
                # (session_service.create_session), which has no supervisor and no
                # orchestration_type requirement on its caller.
                # We still pass sender_id=caller_id if present in DB metadata
                # so plugin events see it.
                metadata = await asyncio.to_thread(get_terminal_metadata, terminal_id)
                if metadata:
                    caller_id = metadata.get("caller_id")
                # Round-3 review fix (call-me-ram): a raw POST /sessions caller that
                # supplies initial_message with no orchestration_type previously sailed
                # straight past send_input's WAITING_USER_ANSWER guard entirely -- the
                # guard only fires for OrchestrationType.ASSIGN/HANDOFF, so an unstated
                # type meant no protection at all against pasting the initial task into
                # a live choice prompt. Every call that reaches THIS function is by
                # construction an unattended initial-task delivery (never an interactive
                # human answer -- those go through answer_user_prompt's own separate
                # /terminals/{id}/input call, which never routes through
                # _schedule_deferred_init), so defaulting an unstated orchestration_type
                # to ASSIGN here is always correct and cannot affect answer_user_prompt.
                effective_orchestration_type = orchestration_type or OrchestrationType.ASSIGN
                # send_input is blocking tmux I/O — off the loop so it can't
                # freeze the server for concurrent requests.
                await asyncio.to_thread(
                    send_input,
                    terminal_id,
                    initial_message,
                    registry=registry,
                    sender_id=caller_id,
                    orchestration_type=effective_orchestration_type,
                )
                # Delivery can be silently dropped (Enter swallowed / paste lost)
                # when the TUI isn't input-ready. Confirm the worker actually
                # started and re-submit if not; if it never starts, surface the
                # failure so the supervisor re-routes instead of waiting forever.
                started = await _confirm_worker_started_or_resubmit(
                    terminal_id,
                    initial_message,
                    registry,
                    caller_id,
                    # Same guard-eligible default as the initial send_input above --
                    # a resubmit is still an unattended initial-task delivery, so it
                    # must not silently drop back to the unguarded original type.
                    effective_orchestration_type,
                    provider=provider_instance,
                )
                # Strict execution-evidence providers (currently Kimi) do not
                # accept cached PROCESSING/COMPLETED as pickup proof. Their
                # direct probe does, however, stop redelivery on a provider
                # ERROR that appears after dispatch: replaying the task cannot
                # fix a rejected model/session and only repeats side effects.
                # Surface that failure to stock-CAO callers instead of silently
                # treating ERROR as a successful start, but keep the terminal
                # alive so external observers (including Bridge) can inspect
                # the actual provider error rather than racing a teardown/404.
                if started and getattr(provider_instance, "requires_execution_evidence", False):
                    current_status = await asyncio.to_thread(status_monitor.get_status, terminal_id)
                    if current_status == TerminalStatus.ERROR:
                        provider_error_message: str | None = None
                        if isinstance(provider_instance, BaseProvider):
                            try:
                                error_buffer = status_monitor.get_buffer(terminal_id)
                                provider_error_message = await asyncio.to_thread(
                                    provider_instance.get_error_message, error_buffer
                                )
                            except Exception as exc:  # noqa: BLE001 - detail is diagnostic only
                                logger.debug(
                                    "Could not extract provider error detail for %s: %s",
                                    terminal_id,
                                    exc,
                                )
                        logger.error(
                            "Deferred init for %s: provider entered ERROR after task delivery.",
                            terminal_id,
                        )
                        await _surface_deferred_init_failure(
                            terminal_id,
                            kind="provider_error_after_delivery",
                            message=provider_error_message
                            or (
                                f"Worker {terminal_id} accepted the assigned task but the "
                                "provider entered ERROR before producing a result. Correct "
                                "the provider/model configuration and re-assign the task."
                            ),
                            registry=registry,
                            delete_on_failure=delete_on_failure,
                        )
                        return
                if not started:
                    logger.error(
                        "Deferred init for %s: worker never started after "
                        "resubmits; task not delivered.",
                        terminal_id,
                    )
                    await _surface_deferred_init_failure(
                        terminal_id,
                        kind="task_not_started",
                        message=(
                            f"Worker {terminal_id} received the assigned task but "
                            f"never started processing (input not accepted after "
                            f"retries). Re-assign the task after correcting the provider "
                            "or delivery state."
                        ),
                        registry=registry,
                        delete_on_failure=delete_on_failure,
                    )
                    return
            await _clear_deferred_init_external_owner(terminal_id)
        except TerminalInputBlockedError as e:
            # The worker initialized but is parked on an interactive prompt
            # (WAITING_USER_ANSWER). It is alive and can be driven via
            # answer_user_prompt — do NOT delete it. Just surface the state to
            # the supervisor so it knows delivery is pending on a prompt.
            logger.warning(
                "Deferred init for terminal %s: worker is waiting on a user "
                "prompt; task not yet delivered. Leaving worker alive for "
                "answer_user_prompt. (%s)",
                terminal_id,
                e,
            )
            await _clear_deferred_init_external_owner(terminal_id)
            await asyncio.to_thread(
                _notify_caller_of_deferred_failure,
                terminal_id,
                f"Worker {terminal_id} is waiting on an interactive prompt; the "
                f"assigned task has not been delivered. Use answer_user_prompt to "
                f"clear the prompt, then re-send the task yourself (e.g. via "
                f"send_message) -- it is not automatically re-delivered once the "
                f"prompt is answered.",
                registry,
                delete_worker=False,
            )
        except Exception as e:
            # exc_info=True preserves the traceback for debugging; {e!r} avoids
            # newline/control-character injection into logs and the inbox message
            # (the exception text can contain provider-supplied content).
            logger.error(
                "Deferred init for terminal %s failed: %r.",
                terminal_id,
                e,
                exc_info=True,
            )
            await _surface_deferred_init_failure(
                terminal_id,
                kind="provider_init_error",
                message=f"Worker {terminal_id} failed to initialize: {e}",
                exception_type=type(e).__name__,
                registry=registry,
                delete_on_failure=delete_on_failure,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error(f"Deferred init for {terminal_id}: no running event loop; init skipped")
        _clear_deferred_init_external_owner_active(terminal_id)
        return None
    task = loop.create_task(_run())
    if delete_on_failure is False:
        # Direct callers of this private scheduler may not have come through
        # create_terminal's pre-insert fence. Marking again is idempotent.
        _mark_deferred_init_external_owner_active(terminal_id)
    _deferred_init_tasks.add(task)

    def _on_done(done_task: asyncio.Task) -> None:
        _deferred_init_tasks.discard(done_task)
        _clear_deferred_init_external_owner_active(terminal_id)

    task.add_done_callback(_on_done)
    return task


def get_terminal(terminal_id: str) -> Dict:
    """Get terminal data."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        public_metadata = metadata.get("metadata")
        deferred_failure = get_deferred_init_failure(
            terminal_id, metadata.get("deferred_init_failure")
        )

        # Deferred init can fail before provider status becomes meaningful. The
        # DB-backed failure marker is authoritative and survives server restart,
        # unlike StatusMonitor's in-memory latch.
        if deferred_failure is not None:
            status = TerminalStatus.ERROR.value
        else:
            observed_status = status_monitor.get_status(terminal_id)
            # External deferred initialization has a short two-phase window:
            # the provider can render ERROR before the background init task has
            # durably written ``deferred_init_failure``. Publishing that
            # transient ERROR lets an external observer settle a generic
            # failure and delete the terminal before the structured detail is
            # visible. Keep the public verdict non-final until the marker (or
            # fallback sidecar) exists; the init task reads StatusMonitor
            # directly and is therefore not blocked by this API-facing gate.
            # A success sidecar overrides a stale ownership bit after a DB
            # outage; the retention helper honors it and repairs the bit when
            # possible, so later ordinary provider errors remain visible.
            if (
                metadata.get("deferred_init_external_owner")
                and observed_status == TerminalStatus.ERROR
                and should_retain_deferred_failure_tombstone(terminal_id, metadata)
            ):
                status = TerminalStatus.UNKNOWN.value
            else:
                status = observed_status.value

        return {
            "id": metadata["id"],
            "name": metadata["tmux_window"],
            "provider": metadata["provider"],
            "session_name": metadata["tmux_session"],
            "agent_profile": metadata["agent_profile"],
            "caller_id": metadata.get("caller_id"),
            "allowed_tools": metadata.get("allowed_tools"),
            "engine": metadata.get("engine"),
            "group": metadata.get("group"),
            "metadata": public_metadata,
            "deferred_init_failure": deferred_failure,
            "session_incarnation_id": metadata.get("session_incarnation_id"),
            "status": status,
            "last_active": metadata["last_active"],
        }

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


def update_group(terminal_id: str, group: Optional[List[str]]) -> bool:
    """Replace a terminal's group array.

    Used by consumers whose own grouping can change after a terminal already
    exists (e.g. harness-control folder/project reassignment) so ``group``
    doesn't go stale (#432). ``None``/``[]`` opts the terminal back out of
    discovery.

    Returns:
        False if the terminal does not exist, True otherwise.
    """
    return update_terminal_group(terminal_id, group)


def update_metadata(terminal_id: str, metadata: Optional[Dict[str, Any]]) -> bool:
    """Replace a terminal's free-form metadata dict.

    Whole-dict replace, not a merge: concurrent calls are last-write-wins
    (tedswinyar, PR #433 review). Acceptable for this field -- callers should
    re-send the full intended dict each time rather than assuming a partial
    update accumulates on top of a prior one.

    Returns:
        False if the terminal does not exist, True otherwise.
    """
    return update_terminal_metadata(terminal_id, metadata)


def list_siblings(
    caller_id: str, depth: Optional[int] = None, cross_session: bool = False
) -> List[Dict[str, Any]]:
    """Resolve ``caller_id``'s own group and return matching sibling terminals.

    Depth is clamped server-side to ``[1, len(caller_group)]`` (#432): it can
    never be widened past the caller's own group length, and an explicit 0 is
    rejected by the API layer's query-param validation before this is ever
    called (never silently reinterpreted as an unscoped, all-terminals
    query). ``depth=None`` defaults to the caller's full own group length —
    the widest scope the caller is allowed to see.

    A caller with no group set finds no siblings (participates in no
    discovery, per #432) rather than erroring.

    Session-scoped by default (issue #432 design discussion): results are
    additionally filtered to the caller's own ``tmux_session`` unless
    ``cross_session=True`` is explicitly passed — see
    ``list_siblings_by_group_prefix``'s own docstring for the full rationale.

    Returns:
        List of ``{id, group, metadata, status}`` dicts for every OTHER
        terminal whose group shares the resolved prefix. ``status`` is a
        live, point-in-time snapshot (tedswinyar, PR #433 review): a handoff
        terminal that has COMPLETED can still delete itself between this
        call returning and a caller's follow-up ``send_message`` to it, so a
        discovered sibling is never a guarantee it's still reachable --
        ``status`` lets a caller skip an obviously-finished sibling
        proactively, but callers should still expect sends to occasionally
        fail against a sibling that disappeared in that window.
    """
    caller_metadata = get_terminal_metadata(caller_id)
    caller_group = caller_metadata.get("group") if caller_metadata else None
    if not caller_group:
        return []
    caller_session = caller_metadata.get("tmux_session") if caller_metadata else None
    max_depth = len(caller_group)
    effective_depth = max_depth if depth is None else depth
    effective_depth = max(1, min(effective_depth, max_depth))
    prefix = caller_group[:effective_depth]
    siblings = list_siblings_by_group_prefix(
        caller_id, prefix, caller_session=caller_session, cross_session=cross_session
    )
    for sibling in siblings:
        sibling["status"] = status_monitor.get_status(sibling["id"]).value
    return siblings


def get_working_directory(terminal_id: str) -> Optional[str]:
    """Get the current working directory of a terminal's pane.

    Args:
        terminal_id: The terminal identifier

    Returns:
        Working directory path, or None if pane has no directory

    Raises:
        ValueError: If terminal not found
        Exception: If unable to query working directory
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        working_dir = get_backend().get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]
        )
        return working_dir

    except Exception as e:
        logger.error(f"Failed to get working directory for terminal {terminal_id}: {e}")
        raise


def send_input(
    terminal_id: str,
    message: str,
    registry: PluginRegistry | None = None,
    sender_id: str | None = None,
    orchestration_type: OrchestrationType | None = None,
    redelivery: bool = False,
    frozen_memory: str | None = None,
) -> bool:
    """Send input to terminal via tmux paste buffer.

    Uses bracketed paste mode (-p) to bypass TUI hotkey handling. The number
    of Enter keys sent after pasting is determined by the provider's
    ``paste_enter_count`` property (e.g., some TUIs need 2 Enters because
    bracketed paste triggers multi-line mode).

    ``redelivery`` is set only by :func:`redeliver_dropped_message`'s full
    re-send: the very same logical dispatch is being delivered a second time
    because the first attempt never reached the agent, not a new turn. It is
    forwarded as ``mark_redelivery_received()`` so a provider can refresh its
    per-delivery-attempt state without advancing its logical turn count. It is
    declared ahead of ``frozen_memory`` so the memory block stays the final
    parameter.

    ``frozen_memory`` is forwarded UNCHANGED to :func:`inject_memory_context` and
    is otherwise none of this function's business — not inspected, not validated,
    not logged. It is last and defaulted so existing positional callers (notably
    ``agent_step.run_agent_step``, which passes exactly two arguments) are
    unaffected.
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        if (
            metadata.get("provider") == ProviderType.KIRO_CLI.value
            and resolve_kiro_engine(persisted=metadata.get("engine")) == KiroEngine.KAS
        ):
            raise KiroPhase0KASError(profile_has_v2_policy=False)

        provider = provider_manager.get_provider(terminal_id)
        orchestration_value = (
            orchestration_type.value
            if isinstance(orchestration_type, OrchestrationType)
            else str(orchestration_type or "")
        )

        if provider:
            current_status = status_monitor.get_status(terminal_id)

            # Guard: refuse to type into a terminal whose provider process has
            # exited. Without this check, queued messages would be pasted into
            # a bare shell and executed as arbitrary commands.
            if current_status == TerminalStatus.ERROR:
                raise TerminalInputBlockedError(
                    f"Terminal {terminal_id} provider is in ERROR state "
                    "(provider process may have exited). Refusing to deliver input."
                )

            if (
                provider.blocks_orchestrated_input_while_waiting_user_answer is True
                and orchestration_value
                in {OrchestrationType.ASSIGN.value, OrchestrationType.HANDOFF.value}
                and current_status == TerminalStatus.WAITING_USER_ANSWER
            ):
                raise TerminalInputBlockedError(
                    f"Terminal {terminal_id} is waiting for a user answer. "
                    "Use answer_user_prompt to submit a selection or approval before "
                    f"sending {orchestration_value} input."
                )

        # Inject memory context into the very first user message after init.
        # Phase 1 wires injection inline for every provider. The Kiro
        # AgentSpawn hook will replace this path once the plugin
        # migration PR lands; until then, inline injection is the only
        # delivery path.
        # Keep the original message for the PostSendMessageEvent so
        # plugins/webhooks see what the caller sent — not the
        # internal <cao-memory> block that we paste into the TUI.
        original_message = message
        message = inject_memory_context(message, terminal_id, frozen_memory)

        # Check how many Enter keys the provider needs after paste
        enter_count = provider.paste_enter_count if provider else 1

        # Arm the StatusMonitor stickiness gate so that the next provider-
        # detected PROCESSING transition is honored (overriding the latched
        # IDLE/COMPLETED). Without this, sticky ready-status would block
        # the genuine PROCESSING signal that arrives once the agent starts
        # working on the new message.
        if provider and provider.assume_processing_on_dispatch is True:
            status_monitor.notify_input_sent(terminal_id, assume_processing=True)
        else:
            status_monitor.notify_input_sent(terminal_id)

        # Clear ONLY the rolling byte buffer BEFORE sending keys, so stale idle
        # prompts from BEFORE the input can't trigger a false COMPLETED
        # (kiro-cli 2.11's TUI keeps the "ask a question" placeholder in the raw
        # buffer, which combined with input_received=True would return COMPLETED
        # within seconds of send_input). Clearing here — not after send_keys —
        # avoids a race: send_keys includes a submit-delay sleep during which
        # the agent can begin emitting output; a post-send_keys clear would wipe
        # that newly-emitted first chunk of the turn (lost from
        # GET /terminals/{id}/output?mode=full and from early detection). This
        # uses clear_rolling_buffer (byte-only), which preserves the sticky-latch
        # arm set by notify_input_sent above; reset_buffer would wipe the arm and
        # latch-block the IDLE→PROCESSING transition for the whole turn.
        # Give stateful providers the same explicit generation boundary as the
        # rolling byte buffer.  Grok uses this to distinguish a new,
        # byte-identical completion from a retained completion screen.
        status_monitor.clear_rolling_buffer(terminal_id, provider)

        # Mark the provider before send_keys rather than after it.  send_keys
        # includes the provider-specific submit delay, during which a fast CLI
        # can already emit its first processing and completion frames.  Those
        # frames must be parsed as belonging to this turn, not as a stale
        # post-clear redraw.  StatusMonitor has already armed and cleared the
        # same dispatch boundary above.
        #
        # A redelivery re-sends the dispatch CAO is still waiting on, so it gets
        # the same boundary but must not be counted as a new logical turn.
        if provider:
            if redelivery:
                provider.mark_redelivery_received()
            else:
                provider.mark_input_received()
            provider.record_dispatched_message(message)

        get_backend().send_keys(
            metadata["tmux_session"],
            metadata["tmux_window"],
            message,
            enter_count=enter_count,
            force_bracketed_paste=True,
            submit_delay=provider.paste_submit_delay if provider else 0.3,
        )

        update_last_active(terminal_id)
        logger.info(f"Sent input to terminal: {terminal_id}")
        if registry is not None and sender_id is not None and orchestration_type is not None:
            # Telemetry (opt-in; no-ops without the [otel] extra or when the SDK
            # is disabled): record a GenAI ``execute_tool`` span for the dispatch,
            # count it, and propagate the active trace context into the plugin
            # event so downstream consumers can continue the trace.
            from cli_agent_orchestrator.telemetry import (
                execute_tool_span,
                inject_traceparent,
                record_orchestration_dispatch,
            )

            with execute_tool_span(
                f"send_message:{orchestration_value}",
                conversation_id=metadata["tmux_session"],
            ):
                record_orchestration_dispatch(orchestration_value)
                dispatch_plugin_event(
                    registry,
                    "post_send_message",
                    PostSendMessageEvent(
                        session_id=metadata["tmux_session"],
                        sender=sender_id,
                        receiver=terminal_id,
                        message=original_message,
                        orchestration_type=orchestration_type,
                        traceparent=inject_traceparent(),
                    ),
                )
        return True

    except Exception as e:
        logger.error(f"Failed to send input to terminal {terminal_id}: {e}")
        raise


def send_special_key(terminal_id: str, key: str) -> bool:
    """Send a tmux special key sequence (e.g., C-d, C-c) to terminal.

    Unlike send_input(), this sends the key as a tmux key name (not literal text)
    and does not append a carriage return. Used for control signals like Ctrl+D (EOF).

    Args:
        terminal_id: Target terminal identifier
        key: Tmux key name (e.g., "C-d", "C-c", "Escape")

    Returns:
        True if the key was sent successfully

    Raises:
        ValueError: If terminal not found
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Arm StatusMonitor stickiness: special keys (Enter on a permission
        # prompt, C-c interrupting work, C-d sending EOF) all initiate a new
        # processing cycle that must be allowed to push past any latched
        # ready status.
        status_monitor.notify_input_sent(terminal_id)
        get_backend().send_special_key(metadata["tmux_session"], metadata["tmux_window"], key)

        update_last_active(terminal_id)
        logger.info(f"Sent special key '{key}' to terminal: {terminal_id}")
        return True

    except Exception as e:
        logger.error(f"Failed to send special key to terminal {terminal_id}: {e}")
        raise


def exit_terminal_cli(terminal_id: str) -> None:
    """Send the provider-specific exit command to gracefully shut down the CLI.

    Mirrors the ``POST /terminals/{id}/exit`` endpoint: resolve the provider,
    send ``provider.exit_cli()`` — as a tmux key sequence when it is one (e.g.
    ``C-d``), else as literal input (e.g. ``/exit``). This is the graceful CLI
    shutdown that should precede ``delete_terminal`` (which goes straight to
    ``kill_window``). Both the endpoint and ``run_agent_step`` call this so the
    exit-then-delete lifecycle is implemented once.

    Raises:
        ValueError: if no provider is registered for ``terminal_id``.
    """
    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        raise ValueError(f"Provider not found for terminal {terminal_id}")
    exit_command = provider.exit_cli()
    # Some providers use tmux key sequences (e.g., "C-d" for Ctrl+D) instead of
    # text commands (e.g., "/exit"). Key sequences must be sent via
    # send_special_key() to be interpreted by tmux, not as literal text.
    if exit_command.startswith(("C-", "M-")):
        send_special_key(terminal_id, exit_command)
    else:
        send_input(terminal_id, exit_command)


def get_output(terminal_id: str, mode: OutputMode = OutputMode.FULL) -> str:
    """Get terminal output.

    ``FULL`` mode returns the StatusMonitor rolling buffer (the streamed output
    accumulated from the FIFO pipeline), which is bounded to the most recent
    ``state_buffer_max`` bytes (server setting, see settings_service.py; 32KB
    default); it falls back to a tmux history capture only when that buffer
    is empty. This is a deliberate trade-off in the
    event-driven architecture (instant, no tmux call) — it is *not* unbounded
    scrollback, so very long sessions are truncated to the tail. Use the
    on-disk ``{id}.log`` (LogWriter) or the delete-time ``{id}.scrollback``
    snapshot when complete history is required.

    For ``LAST`` mode, if the provider declares ``extraction_retries > 0``,
    retries extraction with 10 s delays between attempts.  This handles
    TUI-based providers (e.g. Antigravity CLI's renderer) whose notification
    spinners can temporarily obscure response text in the tmux capture buffer.

    If the provider exposes an ``extraction_tail_lines`` attribute, that
    fixed value is used for the history capture and the escalating-fetch
    logic below is skipped.

    Otherwise, extraction uses an escalating fetch strategy: start with a
    small capture window and widen until the response marker is found.
    Steps: 200 -> 500 -> 1000 -> 5000.  If no marker is found at 5000 lines,
    the raw tail is returned with a [PARTIAL RESPONSE] prefix so the caller
    knows the output may be incomplete.
    """
    # Escalation steps used when the provider does not declare extraction_tail_lines.
    _ESCALATION_STEPS = [200, 500, 1000, 5000]

    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        # Get output from StatusMonitor buffer (instant, no tmux call)
        full_output = status_monitor.get_buffer(terminal_id)
        if not full_output:
            # Fallback to backend history only if buffer not available (edge case)
            full_output = get_backend().get_history(
                metadata["tmux_session"], metadata["tmux_window"]
            )

        if mode == OutputMode.FULL:
            return full_output
        elif mode == OutputMode.LAST:
            provider = provider_manager.get_provider(terminal_id)
            if provider is None:
                raise ValueError(f"Provider not found for terminal {terminal_id}")

            # If the provider pins a fixed scrollback depth, honour it and skip
            # escalation — the provider knows what it needs.
            fixed_extract_lines = getattr(provider, "extraction_tail_lines", None)
            if fixed_extract_lines is not None:
                full_output = get_backend().get_history(
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    tail_lines=fixed_extract_lines,
                )
                retries = provider.extraction_retries
                last_err: Exception | None = None
                for attempt in range(1 + retries):
                    try:
                        if attempt > 0:
                            time.sleep(10.0)
                            full_output = get_backend().get_history(
                                metadata["tmux_session"],
                                metadata["tmux_window"],
                                tail_lines=fixed_extract_lines,
                            )
                        return provider.extract_last_message_from_script(full_output)
                    except OutputExtractionRejected:
                        # A deliberate content refusal — private reasoning, or a
                        # region that held nothing but chrome and echo. Retrying
                        # cannot help, and the raw fallback below would republish
                        # the very content that was refused.
                        raise
                    except ValueError as exc:
                        last_err = exc
                        logger.debug(
                            "Output extraction attempt %d/%d for %s failed: %s",
                            attempt + 1,
                            1 + retries,
                            terminal_id,
                            exc,
                        )
                # Re-raise as the narrower type: the terminal and provider both
                # resolved, so this is a missing response marker, not a bad
                # reference. Keeps the API boundary from reporting it as 404
                # (issue #570).
                raise OutputExtractionError(str(last_err)) from last_err

            # Escalating fetch: try progressively larger capture windows until
            # the response marker is found or we hit the cap.
            last_err = None
            full_output = ""
            for step_lines in _ESCALATION_STEPS:
                full_output = get_backend().get_history(
                    metadata["tmux_session"],
                    metadata["tmux_window"],
                    tail_lines=step_lines,
                )
                try:
                    result = provider.extract_last_message_from_script(full_output)
                    if step_lines > _ESCALATION_STEPS[0]:
                        logger.debug(
                            "get_output: %s marker found at %d lines",
                            terminal_id,
                            step_lines,
                        )
                    return result
                except OutputExtractionRejected:
                    # Deliberate content refusal: escalate nothing, fall back to
                    # nothing. See the fixed-tail branch above.
                    raise
                except ValueError as exc:
                    last_err = exc
                    logger.debug(
                        "get_output: %s no marker at %d lines, escalating",
                        terminal_id,
                        step_lines,
                    )

            # All tail-based steps failed — try full scrollback before giving up.
            logger.debug(
                "get_output: %s escalation exhausted, trying full_history",
                terminal_id,
            )
            full_output = get_backend().get_history(
                metadata["tmux_session"],
                metadata["tmux_window"],
                full_history=True,
            )
            try:
                result = provider.extract_last_message_from_script(full_output)
                logger.debug("get_output: %s marker found in full_history", terminal_id)
                return result
            except OutputExtractionRejected:
                # Deliberate content refusal: never degrade to the raw pane.
                raise
            except ValueError:
                pass

            # Some provider panes contain channels that are never publishable
            # as an agent response. Exhausting the capture window only proves
            # extraction failed; it does not make those raw bytes safe.
            if not getattr(provider, "allow_raw_transcript_fallback", True):
                raise OutputExtractionError(
                    f"{provider.__class__.__name__} could not extract a publishable "
                    "response after exhausting capture escalation."
                ) from last_err

            # Full scrollback also failed — distinguish overflow from no response.
            # If the buffer is close to full (>=90% of last escalation cap), the
            # response marker was likely produced but pushed past the scrollback
            # limit (overflow).  If the buffer is mostly empty, the agent never
            # produced a text response (e.g. only tool calls, crash, or timeout).
            actual_lines = full_output.count("\n") + 1
            overflow_threshold = int(_ESCALATION_STEPS[-1] * 0.9)
            if actual_lines >= overflow_threshold:
                logger.warning(
                    "get_output: %s response marker not found, buffer near-full "
                    "(%d lines >= %d threshold) — likely overflow",
                    terminal_id,
                    actual_lines,
                    overflow_threshold,
                )
                return (
                    f"[PARTIAL RESPONSE - response marker not found, buffer overflow likely "
                    f"({actual_lines} lines retrieved)]\n{full_output}"
                )
            else:
                logger.warning(
                    "get_output: %s response marker not found, buffer sparse "
                    "(%d lines < %d threshold) — agent likely produced no text response",
                    terminal_id,
                    actual_lines,
                    overflow_threshold,
                )
                return (
                    f"[NO RESPONSE - agent completed without producing a text response "
                    f"({actual_lines} lines in buffer)]\n{full_output}"
                )

    except Exception as e:
        logger.error(f"Failed to get output from terminal {terminal_id}: {e}")
        raise


def read_output_range(terminal_id: str, offset: int, length: int) -> str:
    """Read a byte range from a terminal's append-only on-disk log (U5 / #504).

    This is a SEPARATE read path from ``get_output``: that function returns the
    bounded rolling buffer / tmux tail, whereas this reads an exact byte window
    from ``TERMINAL_LOG_DIR / f"{terminal_id}.log"`` — the append-only,
    monotonic file LogWriter maintains (BR-1). Playback (FR-4.3 / FR-7.3) uses
    the ``terminal_offset_start`` / ``terminal_offset_len`` an event carries to
    fetch exactly the output produced around that event, without copying the
    log into the journal (BR-3).

    Args:
        terminal_id: The terminal whose log to read. Validated against the
            workflow name/id charset before it is joined into the log path, so
            a value containing ``/`` / ``..`` / a NUL can never escape
            ``TERMINAL_LOG_DIR`` (path-traversal defense; reuses
            ``_validate_key_part``).
        offset: Byte offset to seek to. Must be ``>= 0``. An offset at or beyond
            EOF is not an error — the read simply returns the available tail
            (empty string when nothing follows the offset) so playback degrades
            gracefully (BR-4).
        length: Maximum number of bytes to read. Clamped to
            ``TERMINAL_RANGE_MAX_LENGTH`` (BR-2) to bound the read.

    Returns:
        The decoded slice, ``bytes.decode("utf-8", errors="replace")`` so a
        range that starts or ends mid-multibyte-sequence never raises (BR-5,
        matching LogWriter's write encoding). Returns ``""`` for a valid
        terminal whose log does not exist yet (nothing has been logged) — a
        missing log is NOT a playback-breaking error (BR-4).

    Raises:
        ValueError: ``terminal_id`` fails id validation, or ``offset`` is
            negative. Translated to a 400 at the request boundary.
        OSError: A genuine file I/O failure (e.g. a permission error, or the
            path exists but is unreadable). Surfaced to the caller, NOT
            swallowed into an empty string — "nothing logged yet" (return "")
            and "the read failed" (raise) are deliberately distinct outcomes
            (BR-4 / construction error-handling guardrail).
    """
    # Path-traversal defense: reject any id that is not a plain key BEFORE it is
    # joined into the log path. Reuses the workflow key/id validator so the
    # charset rule is defined once (rejects "/", "..", ".", NUL, whitespace).
    _validate_key_part(terminal_id, "terminal_id")

    if offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")

    # Clamp the read window (BR-2). A non-positive length reads nothing rather
    # than raising — the route enforces length >= 1, so this is defense in depth.
    capped_length = max(0, min(length, TERMINAL_RANGE_MAX_LENGTH))

    log_path = TERMINAL_LOG_DIR / f"{terminal_id}.log"

    try:
        with open(log_path, "rb") as f:
            f.seek(offset)  # seeking past EOF is legal; the read below yields b""
            data = f.read(capped_length)
    except FileNotFoundError:
        # Valid terminal that has not logged anything yet (or whose log has been
        # cleaned up): an empty range, never an error (BR-4).
        logger.debug(
            "read_output_range: no log file for terminal %s (offset=%d, length=%d) — "
            "returning empty range",
            terminal_id,
            offset,
            capped_length,
        )
        return ""
    except OSError as e:
        # A genuine I/O failure (permission, etc.) is NOT the same as "nothing
        # logged" — surface it rather than masking a real fault as empty output.
        logger.error(
            "read_output_range: I/O error reading log for terminal %s "
            "(offset=%d, length=%d): %s",
            terminal_id,
            offset,
            capped_length,
            e,
        )
        raise

    return data.decode("utf-8", errors="replace")


def capture_terminal_snapshot(terminal_id: str) -> Optional[Dict]:
    """Persist a terminal's scrollback + metadata snapshot. NON-DESTRUCTIVE.

    The read-only first third of terminal teardown, split out so session
    teardown can run it BEFORE the session kill while leaving every destructive
    step until AFTER the kill is confirmed (#498). It has to precede the kill --
    scrollback only exists while the pane does -- and because it only reads tmux
    and writes two files under ``TERMINAL_LOG_DIR``, running it ahead of a kill
    that then fails to confirm changes no terminal state at all.

    Returns the terminal's metadata (both later thirds need it), or None when no
    registry row exists -- i.e. there is nothing to tear down. The returned dict
    carries one key that is NOT a registry column: ``live_working_directory``,
    the pane's cwd read here while the pane still exists.
    ``dismantle_terminal_runtime`` needs it for issue #100's worktree cleanup and
    cannot read it itself -- on the session-teardown path the pane is already
    gone by the time it runs -- so the single read is captured here and passed
    along rather than repeated.
    """
    metadata = get_terminal_metadata(terminal_id)
    if not metadata:
        return None

    # Read the pane's live working directory BEFORE anything destroys the pane.
    # Single read, reused for two purposes: the scrollback snapshot below, and
    # issue #100 Phase 1's worktree cleanup (recognizing a worktree-backed
    # terminal from its live cwd alone -- there is no separate CAO-side record
    # of which terminals are worktree-backed). Best-effort: a read failure
    # means the snapshot's working_directory field is None and no worktree
    # cleanup runs later.
    # Launch-time cwd is durable and is the best fallback once a pane has
    # already disappeared (Herdr pane/workspace close). Prefer a live read when
    # available, but never lose the worktree-cleanup identity solely because
    # the runtime ended before lifecycle reconciliation reached this row.
    live_working_directory = metadata.get("working_directory")
    try:
        observed_cwd = get_backend().get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]
        )
        if observed_cwd:
            live_working_directory = observed_cwd
    except Exception as e:
        logger.warning(f"Failed to read working directory for {terminal_id}: {e}")
    metadata["live_working_directory"] = live_working_directory

    # Snapshot scrollback + metadata before killing (for debugging/restore)
    try:
        # Capture plain text full scrollback (no -e, no line cap)
        scrollback = get_backend().get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            strip_escapes=True,
            full_history=True,
        )
        scrollback_path = TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"
        scrollback_path.write_text(scrollback, encoding="utf-8")

        import json as _json

        snapshot = {
            "terminal_id": terminal_id,
            "session_name": metadata["tmux_session"],
            "window_name": metadata["tmux_window"],
            "agent_profile": metadata.get("agent_profile"),
            "provider": metadata["provider"],
            "working_directory": live_working_directory,
            "allowed_tools": metadata.get("allowed_tools"),
            "caller_id": metadata.get("caller_id"),
        }
        snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
        snapshot_path.write_text(_json.dumps(snapshot, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Failed to snapshot terminal {terminal_id}: {e}")

    return metadata


def dismantle_terminal_runtime(
    terminal_id: str,
    metadata: Optional[Dict],
    kill_window: bool = True,
) -> bool:
    """Tear down a terminal's runtime state, but NOT its registry row.

    The destructive middle third: herdr inbox deregistration, pipe-pane stop,
    FIFO reader stop, status-monitor clear, the tmux window kill, worktree
    cleanup, provider cleanup, and the per-terminal bookkeeping registries. Every
    step is individually guarded and idempotent, so re-running it on an
    already-dismantled terminal is a no-op -- which is what makes a re-run after
    a failed session teardown safe.

    ``kill_window=False`` skips the two tmux-facing steps (pipe-pane stop and the
    window kill). Session teardown passes False because it has already confirmed
    the whole tmux SESSION gone, so the window no longer exists and both calls
    would only produce spurious warnings.

    For a retained deferred-init tombstone -- ``deferred_init_external_owner``,
    ``deferred_init_failure``, or ``deferred_init_runtime_reclaimed`` -- the two
    label-addressed tmux steps are REPLACED by ``cleanup_terminal_exact``, which
    keys on the terminal id instead. Session and window names are reusable, so a
    tombstone often shares them with a later, unrelated replacement; killing "the
    window called X" would destroy that replacement. ``kill_window=False`` makes
    the exact call identity-proof-only (no mutation), so a caller that already
    established the session is gone still gets a truthful verdict.

    Returns False when the runtime is NOT fully dismantled and the caller must
    keep the registry row for a durable retry. That covers a deferred provider
    cleanup (Grok's private-home owner could not yet be inspected/stopped), a
    FIFO reader that would not stop, and -- for tombstones -- an exact cleanup
    that answered anything other than DELETED or ABSENT (UNKNOWN and
    STILL_PRESENT both mean the runtime may still be live). Reporting True on a
    deferral would turn a temporary process race into a permanent leak, or
    record a still-live runtime as reclaimed.

    An unproven exact result also defers all remaining teardown: in particular,
    a retained failure can be rediscovered after restart while its original
    process still uses its worktree and private provider home. Retaining only
    the registry row cannot undo destruction of those live resources.

    Ordering note: stopping the FIFO reader before killing the window is
    preferred but not load-bearing -- since issue #382 the reader loop uses a
    non-blocking fd plus a ``select`` timeout and holds its own keepalive write
    end, so it can never park waiting on the pane and always observes the stop
    flag within one poll interval.
    """
    runtime_complete = True

    # A retained tombstone outlives its terminal. Before touching anything
    # that is addressed by the terminal's session/window NAME, establish
    # whether the live object at that name is still OURS. Label-addressed
    # teardown stays the historical path for an ordinary live-terminal delete;
    # for a tombstone it is exactly the mistake this contract exists to
    # prevent. (Cross-node/elastic and local paths are otherwise unchanged.)
    tombstone = False
    if metadata and (
        metadata.get("deferred_init_external_owner")
        or metadata.get("deferred_init_failure")
        or metadata.get("deferred_init_runtime_reclaimed")
    ):
        tombstone = True
        try:
            exact = get_backend().cleanup_terminal_exact(
                terminal_id,
                metadata.get("tmux_session"),
                metadata.get("tmux_window"),
                close=kill_window,
            )
        except Exception as e:  # noqa: BLE001 — unproven identity must not read as cleaned
            runtime_complete = False
            logger.warning(
                "Exact cleanup of deferred-init terminal %s raised; retaining its "
                "runtime cleanup for a durable retry: %s",
                terminal_id,
                e,
            )
        else:
            if not exact.reclaimed:
                runtime_complete = False
                logger.warning(
                    "Deferred-init terminal %s was not reclaimed exactly (%s: %s); "
                    "retaining its runtime cleanup for a durable retry",
                    terminal_id,
                    exact.outcome.value,
                    exact.detail,
                )
            else:
                logger.info(
                    "Deferred-init terminal %s exact cleanup: %s (%s)",
                    terminal_id,
                    exact.outcome.value,
                    exact.detail,
                )

        if not runtime_complete:
            return False

    # Unregister from herdr inbox service
    svc = get_herdr_inbox_service()
    if svc:
        try:
            svc.unregister_terminal(terminal_id)
        except Exception as e:
            logger.warning(f"Failed to unregister terminal {terminal_id} from herdr inbox: {e}")

    if metadata and kill_window and not tombstone:
        # Stop pipe-pane logging. Before the FIFO steps below, so the pane stops
        # writing to the FIFO before its reader (and the FIFO file) go away.
        try:
            get_backend().stop_pipe_pane(metadata["tmux_session"], metadata["tmux_window"])
        except Exception as e:
            logger.warning(f"Failed to stop pipe-pane for {terminal_id}: {e}")

    # Deliberately OUTSIDE the `if metadata:` block below: both of these need
    # only terminal_id. Gating them on metadata meant a failed snapshot (a
    # `get_terminal_metadata` that raised, or "database is locked") skipped them,
    # orphaning the FIFO reader thread and the status-detector buffers for a
    # terminal whose row the by-id sweep then deleted anyway -- a reader with
    # nothing left to read from and no record it exists.
    try:
        if fifo_manager.stop_reader(terminal_id) is False:
            runtime_complete = False
    except Exception as e:
        logger.warning(f"Failed to stop FIFO reader for {terminal_id}: {e}")
        runtime_complete = False

    # Clear state detector buffers for this terminal
    try:
        status_monitor.clear_terminal(terminal_id)
    except Exception as e:
        logger.warning(f"Failed to clear state detector for {terminal_id}: {e}")

    if metadata:
        if kill_window and not tombstone:
            # Kill the tmux window (this terminates the agent process)
            try:
                get_backend().kill_window(metadata["tmux_session"], metadata["tmux_window"])
            except Exception as e:
                logger.warning(f"Failed to kill tmux window for {terminal_id}: {e}")

        # issue #100 Phase 1: if this terminal was worktree-backed (its live
        # cwd matched the CAO-managed worktree path shape), remove the
        # worktree + branch now that the process using it is gone.
        # `remove_worktree` is itself best-effort/never-raises, matching
        # every other step in this teardown.
        #
        # The parsed terminal_id MUST match the terminal actually being
        # deleted here, not just "some" CAO worktree path. Without this
        # guard: a worktree-backed terminal A (cwd
        # .../.cao/worktrees/A) can spawn a non-worktree terminal B with
        # working_directory explicitly set to A's cwd (handoff/assign
        # both accept an explicit working_directory, and "here" -- the
        # caller's own directory -- is a common choice). Deleting B --
        # including handoff's automatic success teardown -- would then
        # read B's pane cwd (== A's worktree path), parse terminal_id
        # "A" out of it, and force-remove A's still-running worktree.
        # Mismatched parses now fall through as a no-op leak (Phase 3
        # territory) instead of destroying another terminal's checkout.
        parsed = worktree_service.parse_worktree_path(metadata.get("live_working_directory"))
        if parsed is not None:
            worktree_repo_root, worktree_terminal_id = parsed
            if worktree_terminal_id == terminal_id:
                worktree_service.remove_worktree(worktree_repo_root, worktree_terminal_id)

    # Grok cleanup can be deferred when a private-home owner cannot yet be
    # inspected/stopped.  Keep both the provider mapping and DB metadata so
    # a subsequent DELETE can retry; reporting success here would turn a
    # temporary process race into a permanent private-home leak.
    if provider_manager.cleanup_provider(terminal_id) is False:
        return False
    with _memory_injected_lock:
        _memory_injected_terminals.discard(terminal_id)
    # Drop any per-curator dispatch lock so the registry doesn't grow
    # forever as memory_manager terminals come and go.
    from cli_agent_orchestrator.services.memory_service import _curator_locks

    _curator_locks.pop(terminal_id, None)
    # ``runtime_complete`` aggregates every component above that can report
    # failure, including the exact-identity cleanup for tombstones. The caller
    # (``_retain_deferred_failure_tombstone``) only writes the durable
    # ``deferred_init_runtime_reclaimed`` marker when this is True, so anything
    # short of "exactly DELETED or ABSENT, and every other step succeeded" stays
    # retryable instead of being recorded as reclaimed.
    return runtime_complete


def delete_terminal_row(
    terminal_id: str,
    metadata: Optional[Dict],
    registry: PluginRegistry | None = None,
) -> bool:
    """Drop a terminal's registry row and emit ``post_kill_terminal``.

    The final third of terminal teardown, split out so session teardown can
    defer it past its kill-confirmation point (#498) -- deleting a row for a
    session that turns out to still be alive is exactly how the registry and
    tmux diverge. ``metadata`` is what ``capture_terminal_snapshot`` returned;
    it is needed for the event payload because the row is gone by the time the
    event is built.

    ``registry=None`` drops the row WITHOUT emitting. Session teardown passes
    None and emits the events itself once it has released the lifecycle lock, so
    that no third-party plugin ever runs inside its critical section; the
    single-terminal ``delete_terminal`` path holds no such lock and passes its
    registry straight through.
    """
    # Serialize row deletion with fallback publication. Otherwise a deferred
    # init task can exhaust DB retries, a concurrent DELETE can remove the row
    # and existing sidecars, and then the background task can publish a new
    # orphan sidecar after DELETE has already returned.
    with _DEFERRED_INIT_SIDECAR_LOCK:
        deleted = db_delete_terminal(terminal_id)
        # Sidecars are keyed by terminal id and are safe to remove even when the
        # DB row was already deleted by another lifecycle owner.
        _delete_deferred_failure_fallback(terminal_id)
        _delete_deferred_init_complete_fallback(terminal_id)
    logger.info(f"Deleted terminal: {terminal_id}")
    if deleted and metadata:
        dispatch_plugin_event(
            registry,
            "post_kill_terminal",
            PostKillTerminalEvent(
                session_id=metadata["tmux_session"],
                terminal_id=terminal_id,
                agent_name=metadata.get("agent_profile"),
            ),
        )
    return deleted


def delete_terminal(terminal_id: str, registry: PluginRegistry | None = None) -> bool:
    """Delete terminal and kill its tmux window.

    Single-terminal teardown: all three thirds back to back, in the order they
    have always run. Session teardown does NOT use this -- it interleaves its own
    tmux kill-confirmation between them (see ``services/session_service.py``).

    Returns False when the teardown was deferred (see
    ``dismantle_terminal_runtime``), leaving the row in place for a retry.
    """
    try:
        metadata = capture_terminal_snapshot(terminal_id)
        if not dismantle_terminal_runtime(terminal_id, metadata):
            logger.warning(
                "Terminal %s cleanup deferred; retaining metadata for a retry", terminal_id
            )
            return False
        return delete_terminal_row(terminal_id, metadata, registry=registry)

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise
