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
import logging
import os
import re
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import (
    create_inbox_message,
)
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    delete_terminals_by_session,
    get_terminal_metadata,
    list_siblings_by_group_prefix,
    update_last_active,
    update_terminal_group,
    update_terminal_metadata,
    update_terminal_shell_command,
)
from cli_agent_orchestrator.constants import (
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
    TerminalStatus,
)
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateTerminalEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.providers.base import OutputExtractionError
from cli_agent_orchestrator.providers.kiro_capabilities import (
    KiroCapabilities,
    KiroPhase0KASError,
    probe_kiro_capabilities,
    requested_kiro_capabilities,
)
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services import worktree_service
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.herdr_inbox_registry import get_herdr_inbox_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.step_output_store import _validate_key_part
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.path_validation import resolve_and_validate_path
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
    wait_until_status,
)

logger = logging.getLogger(__name__)

# Upper bound (bytes) on a single offset-ranged read of a terminal log
# (U5 / #504, BR-2). ``read_output_range`` clamps its ``length`` to this so a
# caller (playback fetching output around a selected event) can never trigger
# an unbounded read of a large log file. 1 MiB is a defensible ceiling: it is
# far larger than any realistic per-event output window (the rolling
# STATE_BUFFER_MAX is only 8 KiB) yet bounds the worst-case allocation and
# response size to a fixed, predictable amount regardless of on-disk log size.
TERMINAL_RANGE_MAX_LENGTH = 1024 * 1024

# Track terminals that have already received memory injection (first message only).
_memory_injected_terminals: set = set()
_memory_injected_lock = threading.Lock()

# Strong references to in-flight deferred-init background tasks. asyncio keeps
# only a WEAK reference to tasks from loop.create_task, so without this a
# deferred provider.initialize() + input-send task could be GC'd mid-run,
# silently leaving a worker uninitialized. Tasks drop themselves on completion.
_deferred_init_tasks: set = set()


def inject_memory_context(
    first_message: str, terminal_id: str, frozen_memory: str | None = None
) -> str:
    """Prepend <cao-memory> context block to the first user message.

    Tracks which terminals have already been injected so that only the very
    first user message after init receives the memory block.

    Calls MemoryService.get_curated_memory_context(), which returns a formatted
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
        svc = MemoryService()
        context = svc.get_curated_memory_context(terminal_id, task_description=first_message[:200])
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

# Providers whose tool restrictions are prompt-level text only (no native
# blocking mechanism) — a restricted policy on these is advisory, not enforced.
SOFT_ENFORCEMENT_PROVIDERS = {
    ProviderType.KIMI_CLI.value,
    ProviderType.CODEX.value,
    ProviderType.ANTIGRAVITY_CLI.value,
    ProviderType.OMP.value,
    ProviderType.MINIMAX_CODE.value,
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
    never-observable-half-built argument the closure's docstring makes. Under
    the lock, kill the session/window THIS call created, then drop the
    committed row, so the cancelled create leaves both stores exactly as it
    found them.

    Best-effort like its sibling: the cancellation is already propagating and
    is what the caller must see.
    """
    with session_lifecycle_lock(session_name):
        _roll_back_backend_create_locked(session_name, window_name, created_session=created_session)
        try:
            db_delete_terminal(terminal_id)
        except Exception:
            logger.exception(
                f"Rollback: failed to delete registry row {terminal_id} " "after a cancelled create"
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
) -> Terminal:
    """Create a new terminal with an initialized CLI agent.

    This function orchestrates the complete terminal creation workflow:
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

    Returns:
        Terminal object with all metadata populated

    Raises:
        ValueError: If session already exists (new_session=True) or not found (new_session=False)
        TimeoutError: If provider initialization times out
    """
    terminal_id: Optional[str] = None
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
                from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

                mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
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
            from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

            mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
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
        if provider in SOFT_ENFORCEMENT_PROVIDERS and allowed_tools and "*" not in allowed_tools:
            logger.warning(
                f"Terminal {terminal_id}: provider '{provider}' cannot enforce tool "
                f"restrictions (soft/prompt-level only) but profile '{agent_profile}' "
                f"requests {allowed_tools}. Treat this worker as unrestricted; for "
                f"enforced restrictions use claude_code, grok_cli, kiro_cli, or "
                f"copilot_cli."
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

                    # Create new tmux session with initial window
                    get_backend().create_session(
                        session_name,
                        window_name,
                        terminal_id,
                        resolved_working_directory,
                        extra_env=env_vars,
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
                    # Merge explicit per-step env_vars over the persisted session
                    # env (per-step wins on conflict): workflow routing ids like
                    # CAO_WORKFLOW_RUN_ID must reach the window even when it
                    # joins an existing session (issue #408).
                    created_window_name = get_backend().create_window(
                        session_name,
                        window_name,
                        terminal_id,
                        resolved_working_directory,
                        extra_env={**get_session_env(session_name), **(env_vars or {})},
                    )
                    created_session, created_window = False, True

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
                        delete_terminals_by_session(session_name)

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
                    )
                except BaseException:
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
            _schedule_deferred_init(
                provider_instance,
                terminal_id,
                initial_message,
                initial_message_orchestration_type,
                registry,
            )
        else:
            await provider_instance.initialize()

            # Persist shell_command baseline if the provider captured one
            shell_command = provider_instance.shell_baseline
            if not isinstance(shell_command, str):
                shell_command = None
            if shell_command:
                update_terminal_shell_command(terminal_id, shell_command)

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
        # Cleanup on failure: clean up FIFO reader, status monitor, provider, and session
        logger.error(f"Failed to create terminal: {e}")
        try:
            if terminal_id is not None:
                fifo_manager.stop_reader(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        try:
            if terminal_id is not None:
                status_monitor.clear_terminal(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        # Roll back the DB terminal row so a failed create does not leave an
        # orphan record: the stale row would still be listed for the session
        # and report UNKNOWN status even though nothing is running. Idempotent
        # (DELETE ... WHERE id = ?), so it is a no-op when the failure happened
        # before the row was written. Runs regardless of session_created so a
        if session_created and session_name:
            try:
                get_backend().kill_session(session_name)
            except:
                pass  # Ignore cleanup errors
            # Session is gone, drop any forwarded env we stashed for it so
            # secrets don't linger in memory or bleed into a future reuse
            # of the same name.
            clear_session_env(session_name)
        elif window_created and session_name and window_name:
            # harness-control#186: a window added to an ALREADY-EXISTING session
            # (new_session=False -- every MCP spawn/assign-into-existing-session
            # call) has no session-level teardown to fall back on above, since
            # `session_created` is False and the pre-existing session must stay
            # up. Live-reproduced without this: a provider init timeout here
            # (e.g. "Claude Code initialization timed out after 60s") rolls back
            # the DB row and stops the FIFO/provider/status-monitor above, but
            # the tmux WINDOW itself — the actual pane, still running whatever
            # shell/process the provider left behind — was never torn down.
            # Result: the caller (the spawning agent's MCP tool call) gets a
            # hard error back, AND a permanently orphaned window is left behind:
            # invisible to this terminal's own list/tree (the DB row is gone),
            # never cleaned up, sitting there indefinitely.
            try:
                get_backend().kill_window(session_name, window_name)
            except Exception:
                pass  # Ignore cleanup errors
        # The process-owning tmux session/window must be stopped before a
        # provider releases private on-disk state.  In particular Grok can
        # have an updater still writing $GROK_HOME while its initialization
        # fails; its cleanup verifies that no such process remains.
        cleanup_complete = True
        try:
            if terminal_id is not None:
                cleanup_complete = provider_manager.cleanup_provider(terminal_id) is not False
        except Exception:
            # Preserve the existing rollback contract for an unexpected
            # provider-manager failure. Only an explicit False is a Grok
            # cleanup deferral with enough information to retry safely.
            cleanup_complete = True
        # Do not erase the only retry handle before Grok has safely released
        # its private home.  The original create error is still raised below;
        # retaining this row makes the failed terminal discoverable and its
        # deletion retryable rather than leaking credentials/config forever.
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
            # A worktree WAS created (Step 1b succeeded) before some later step
            # failed -- roll it back too, same best-effort posture as everything
            # else in this block. Without this, a provider-init timeout (or any
            # later failure) on a worktree-backed terminal would leave an orphan
            # worktree + branch behind with no CAO-side record pointing at it.
            # Offloaded to a thread for the same reason Step 1b's create is:
            # `git worktree remove` is a blocking subprocess call and this
            # `except` block still runs on the shared event loop.
            await asyncio.to_thread(
                worktree_service.remove_worktree, worktree_repo_root, terminal_id
            )
        raise


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
    wait forever on a callback that will never come. Every step is best-effort
    and independently guarded — a failure to notify must not prevent teardown,
    and a failure to tear down must not crash the background task.
    """
    caller_id = None
    try:
        metadata = get_terminal_metadata(terminal_id)
        if metadata:
            caller_id = metadata.get("caller_id")
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

    This function does a live ``capture-pane`` to grab the visible screen
    (not the 8 KB rolling buffer, which is too small to reliably hold the
    footer) and calls ``provider.get_status()`` directly, catching the real
    state so the retry loop doesn't re-deliver into a working terminal.

    Only providers that set ``supports_direct_status_probe = True`` should
    be passed to this function; the ``get_status()`` contract for other
    providers (e.g. kiro_cli, antigravity_cli, cursor_cli) relies on
    dispatch bookkeeping and cannot distinguish IDLE from COMPLETED on a
    rendered capture-pane snapshot.
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            return False
        session_name = metadata.get("tmux_session")
        window_name = metadata.get("tmux_window")
        if not session_name or not window_name:
            return False
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


def _message_visible_in_box(terminal_id: str, message: str) -> bool:
    """True when the delivered message is still visible in the rendered pane.

    Despite the name, this matches against ``get_output`` — the whole rendered
    pane, which includes the transcript above the composer, not just the input
    box. A prompt echoed in the transcript therefore also reads as "visible",
    which is safe for the Enter-vs-full-resend decision (both actions are
    recovery for a worker believed idle) but must not be read as proof the
    text sits unsubmitted in the composer.

    Decides the resubmit action: if our text is there the paste landed and only
    the Enter was dropped (send a bare Enter); if it is absent the paste itself
    was dropped (re-deliver the full message). Guessing wrong the other way must
    be avoided — a bare Enter into an EMPTY box would submit a blank prompt and
    the real task would be lost. Collapse to [a-z0-9] so wrapping / whitespace /
    unicode punctuation in the rendered box can't defeat the match.
    """
    probe = re.sub(r"[^a-z0-9]", "", message.lower())[:24]
    if len(probe) < 8:
        # Too short to match reliably — treat as "not shown" so we re-deliver
        # in full rather than risk a blank submit.
        return False
    try:
        rendered = get_output(terminal_id)
    except Exception:
        return False
    return probe in re.sub(r"[^a-z0-9]", "", rendered.lower())


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
    redelivery: if the delivered text is still visible in the rendered pane
    only the Enter was swallowed (send a bare Enter); if it is absent the
    paste itself was dropped (re-deliver in full). See
    ``_message_visible_in_box`` for why guessing wrong must be avoided.

    ``full_resend_requires_probe`` gates the full re-send on the provider
    being probe-capable. Reason: ``_message_visible_in_box`` scans the whole
    rendered pane, and under the pyte screen path status detection runs only
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
    if await wait_until_status(
        terminal_id,
        _DEFERRED_STARTED_STATUSES,
        timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
        polling_interval=0.5,
    ):
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
        if await wait_until_status(
            terminal_id,
            _DEFERRED_STARTED_STATUSES,
            timeout=_DEFERRED_SUBMIT_CONFIRM_TIMEOUT,
            polling_interval=0.5,
        ):
            return True

    return False


def _schedule_deferred_init(
    provider_instance,
    terminal_id: str,
    initial_message: Optional[str],
    orchestration_type: Optional[OrchestrationType],
    registry: PluginRegistry | None,
) -> None:
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
        caller_id: Optional[str] = None
        try:
            await provider_instance.initialize()
            shell_command = provider_instance.shell_baseline
            if isinstance(shell_command, str) and shell_command:
                update_terminal_shell_command(terminal_id, shell_command)
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
                if not started:
                    logger.error(
                        "Deferred init for %s: worker never started after "
                        "resubmits; task not delivered — notifying caller and "
                        "tearing down.",
                        terminal_id,
                    )
                    await asyncio.to_thread(
                        _notify_caller_of_deferred_failure,
                        terminal_id,
                        (
                            f"Worker {terminal_id} received the assigned task but "
                            f"never started processing (input not accepted after "
                            f"retries). It has been deleted — re-assign the task."
                        ),
                        registry,
                        True,  # delete_worker
                    )
                    return
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
                "Deferred init for terminal %s failed: %r. "
                "Notifying caller and tearing down worker.",
                terminal_id,
                e,
                exc_info=True,
            )
            await asyncio.to_thread(
                _notify_caller_of_deferred_failure,
                terminal_id,
                f"Worker {terminal_id} failed to initialize: {e!r}. It has been "
                f"deleted — re-assign the task or report the failure.",
                registry,
                delete_worker=True,
            )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error(f"Deferred init for {terminal_id}: no running event loop; init skipped")
        return
    task = loop.create_task(_run())
    _deferred_init_tasks.add(task)
    task.add_done_callback(_deferred_init_tasks.discard)


def get_terminal(terminal_id: str) -> Dict:
    """Get terminal data."""
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        status = status_monitor.get_status(terminal_id).value

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
            "metadata": metadata.get("metadata"),
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
    frozen_memory: str | None = None,
) -> bool:
    """Send input to terminal via tmux paste buffer.

    Uses bracketed paste mode (-p) to bypass TUI hotkey handling. The number
    of Enter keys sent after pasting is determined by the provider's
    ``paste_enter_count`` property (e.g., some TUIs need 2 Enters because
    bracketed paste triggers multi-line mode).

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
        if provider:
            provider.mark_input_received()

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
            except ValueError:
                pass

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
    live_working_directory = None
    try:
        live_working_directory = get_backend().get_pane_working_directory(
            metadata["tmux_session"], metadata["tmux_window"]
        )
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

    Returns False when provider cleanup was DEFERRED (Grok's private-home owner
    could not yet be inspected/stopped), meaning the caller must keep the
    registry row so a later DELETE can retry; True when the runtime is fully
    dismantled. Reporting True on a deferral would turn a temporary process race
    into a permanent private-home leak.

    Ordering note: stopping the FIFO reader before killing the window is
    preferred but not load-bearing -- since issue #382 the reader loop uses a
    non-blocking fd plus a ``select`` timeout and holds its own keepalive write
    end, so it can never park waiting on the pane and always observes the stop
    flag within one poll interval.
    """
    # Unregister from herdr inbox service
    svc = get_herdr_inbox_service()
    if svc:
        try:
            svc.unregister_terminal(terminal_id)
        except Exception as e:
            logger.warning(f"Failed to unregister terminal {terminal_id} from herdr inbox: {e}")

    if metadata and kill_window:
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
        fifo_manager.stop_reader(terminal_id)
    except Exception as e:
        logger.warning(f"Failed to stop FIFO reader for {terminal_id}: {e}")

    # Clear state detector buffers for this terminal
    try:
        status_monitor.clear_terminal(terminal_id)
    except Exception as e:
        logger.warning(f"Failed to clear state detector for {terminal_id}: {e}")

    if metadata:
        if kill_window:
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
    return True


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
    deleted = db_delete_terminal(terminal_id)
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
