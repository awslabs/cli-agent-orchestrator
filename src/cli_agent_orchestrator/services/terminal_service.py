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

import logging
import threading
import time
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import create_terminal as db_create_terminal
from cli_agent_orchestrator.clients.database import delete_terminal as db_delete_terminal
from cli_agent_orchestrator.clients.database import (
    get_terminal_metadata,
    update_last_active,
    update_terminal_shell_command,
)
from cli_agent_orchestrator.constants import FIFO_DIR, SESSION_PREFIX, TERMINAL_LOG_DIR
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import Terminal, TerminalStatus
from cli_agent_orchestrator.plugins import (
    PluginRegistry,
    PostCreateTerminalEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.fifo_reader import fifo_manager
from cli_agent_orchestrator.services.herdr_inbox_registry import get_herdr_inbox_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.plugin_dispatch import dispatch_plugin_event
from cli_agent_orchestrator.services.session_env import (
    clear_session_env,
    get_session_env,
    set_session_env,
)
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.skills import build_skill_catalog
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_terminal_id,
    generate_window_name,
)

logger = logging.getLogger(__name__)

# Track terminals that have already received memory injection (first message only).
_memory_injected_terminals: set = set()
_memory_injected_lock = threading.Lock()


class TerminalInputBlockedError(Exception):
    """Raised when orchestrated input would answer an active interactive prompt."""


def inject_memory_context(first_message: str, terminal_id: str) -> str:
    """Prepend <cao-memory> context block to the first user message.

    Tracks which terminals have already been injected so that only the very
    first user message after init receives the memory block.

    Calls MemoryService.get_memory_context_for_terminal() which returns
    a formatted <cao-memory>...</cao-memory> block (or empty string if
    no memories exist). Stateless — no file mutation, no backup/restore.
    """
    with _memory_injected_lock:
        if terminal_id in _memory_injected_terminals:
            return first_message
        _memory_injected_terminals.add(terminal_id)

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
}

# Providers whose tool restrictions are prompt-level text only (no native
# blocking mechanism) — a restricted policy on these is advisory, not enforced.
SOFT_ENFORCEMENT_PROVIDERS = {
    ProviderType.KIMI_CLI.value,
    ProviderType.CODEX.value,
    ProviderType.ANTIGRAVITY_CLI.value,
}


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
            automatically; the explicit ``env_vars`` argument is ignored to
            keep the per-session view consistent. See issue #248.
        caller_id: Terminal ID of the supervisor that created this terminal
            via handoff/assign. Recorded so send_message can route callbacks
            structurally instead of parsing IDs out of message text (issue #284).
            None for operator-launched terminals.

    Returns:
        Terminal object with all metadata populated

    Raises:
        ValueError: If session already exists (new_session=True) or not found (new_session=False)
        TimeoutError: If provider initialization times out
    """
    session_created = False  # tracks whether THIS call created the tmux session
    try:
        # Step 1: Generate unique identifiers
        terminal_id = generate_terminal_id()

        if not session_name:
            session_name = generate_session_name()

        window_name = generate_window_name(agent_profile)

        # Step 2: Create tmux session or window
        if new_session:
            # Ensure session name has the CAO prefix for identification
            if not session_name.startswith(SESSION_PREFIX):
                session_name = f"{SESSION_PREFIX}{session_name}"

            # Prevent duplicate sessions
            if get_backend().session_exists(session_name):
                raise ValueError(f"Session '{session_name}' already exists")

            # Wipe any stale mapping a prior aborted lifecycle for this name
            # may have left behind, so a no-env relaunch can't inherit them.
            clear_session_env(session_name)

            # Create new tmux session with initial window
            get_backend().create_session(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                extra_env=env_vars,
            )
            session_created = True  # only set after successful creation

            # Persist forwarded env only after the tmux session actually
            # exists; the failure path below clears it if a later step
            # tears the session back down.
            if env_vars:
                set_session_env(session_name, env_vars)
        else:
            # Add window to existing session
            if not get_backend().session_exists(session_name):
                raise ValueError(f"Session '{session_name}' not found")
            window_name = get_backend().create_window(
                session_name,
                window_name,
                terminal_id,
                working_directory,
                extra_env=get_session_env(session_name),
            )

        # Step 3: Load the profile once for allowed tool resolution before
        # provider initialization. The skill catalog is computed only for
        # providers that consume it at launch time (see RUNTIME_SKILL_PROMPT_PROVIDERS).
        try:
            profile = load_agent_profile(agent_profile)
        except FileNotFoundError:
            profile = None
        skill_prompt = (
            build_skill_catalog(profile.skills if profile else None)
            if provider in RUNTIME_SKILL_PROMPT_PROVIDERS
            else None
        )

        # Step 3b: Resolve allowed_tools from profile if not explicitly provided
        if allowed_tools is None and profile is not None:
            from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

            mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
            allowed_tools = resolve_allowed_tools(
                profile.allowedTools, profile.role, mcp_server_names
            )

        # Soft-enforcement guard: kimi_cli/codex have NO native tool-blocking
        # mechanism (kimi runs --yolo; restrictions are prompt-level text
        # only), so a restricted policy on them is advisory, not enforced.
        # Surface that loudly at launch so operators route restricted or
        # write-capable roles to hard-enforcement providers instead.
        if provider in SOFT_ENFORCEMENT_PROVIDERS and allowed_tools and "*" not in allowed_tools:
            logger.warning(
                f"Terminal {terminal_id}: provider '{provider}' cannot enforce tool "
                f"restrictions (soft/prompt-level only) but profile '{agent_profile}' "
                f"requests {allowed_tools}. Treat this worker as unrestricted; for "
                f"enforced restrictions use claude_code, kiro_cli, or "
                f"copilot_cli."
            )

        # Step 3c: Persist terminal metadata to database after restrictions
        # are resolved so API reads and snapshots report the actual launch policy.
        db_create_terminal(
            terminal_id,
            session_name,
            window_name,
            provider,
            agent_profile,
            allowed_tools,
            caller_id=caller_id,
        )

        # Step 4/5: Set up the FIFO event-driven output pipeline for pipe-pane
        # backends (tmux). Event-inbox backends (herdr) deliver via their own
        # socket events and their pipe_pane is a no-op, so skip the FIFO there and
        # rely on the herdr inbox registration below.
        if not get_backend().supports_event_inbox():
            # Reader must exist BEFORE pipe-pane starts so it captures from the start.
            fifo_manager.create_reader(terminal_id)

            # Configure pipe-pane to stream output to the FIFO. This enables
            # real-time event-driven processing via StatusMonitor and LogWriter
            # (LogWriter writes TERMINAL_LOG_DIR/{id}.log from the FIFO). A pane
            # has a single pipe-pane target, so we pipe ONLY to the FIFO.
            fifo_path = FIFO_DIR / f"{terminal_id}.fifo"
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
            model=profile.model if profile else None,
        )
        await provider_instance.initialize()

        # Persist shell_command baseline if the provider captured one
        shell_command = provider_instance.shell_baseline
        if not isinstance(shell_command, str):
            shell_command = None
        if shell_command:
            update_terminal_shell_command(terminal_id, shell_command)

        # Build and return the Terminal object
        terminal = Terminal(
            id=terminal_id,
            name=window_name,
            provider=ProviderType(provider),
            session_name=session_name,
            agent_profile=agent_profile,
            caller_id=caller_id,
            allowed_tools=allowed_tools,
            shell_command=shell_command,
            status=TerminalStatus.IDLE,
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
            fifo_manager.stop_reader(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        try:
            status_monitor.clear_terminal(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        try:
            provider_manager.cleanup_provider(terminal_id)
        except Exception:
            pass  # Ignore cleanup errors
        if session_created and session_name:
            try:
                get_backend().kill_session(session_name)
            except:
                pass  # Ignore cleanup errors
            # Session is gone, drop any forwarded env we stashed for it so
            # secrets don't linger in memory or bleed into a future reuse
            # of the same name.
            clear_session_env(session_name)
        raise


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
            "status": status,
            "last_active": metadata["last_active"],
        }

    except Exception as e:
        logger.error(f"Failed to get terminal {terminal_id}: {e}")
        raise


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
) -> bool:
    """Send input to terminal via tmux paste buffer.

    Uses bracketed paste mode (-p) to bypass TUI hotkey handling. The number
    of Enter keys sent after pasting is determined by the provider's
    ``paste_enter_count`` property (e.g., some TUIs need 2 Enters because
    bracketed paste triggers multi-line mode).
    """
    try:
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal '{terminal_id}' not found")

        provider = provider_manager.get_provider(terminal_id)
        orchestration_value = (
            orchestration_type.value
            if isinstance(orchestration_type, OrchestrationType)
            else str(orchestration_type or "")
        )
        if (
            provider
            and provider.blocks_orchestrated_input_while_waiting_user_answer is True
            and orchestration_value
            in {OrchestrationType.ASSIGN.value, OrchestrationType.HANDOFF.value}
            and status_monitor.get_status(terminal_id) == TerminalStatus.WAITING_USER_ANSWER
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
        message = inject_memory_context(message, terminal_id)

        # Check how many Enter keys the provider needs after paste
        enter_count = provider.paste_enter_count if provider else 1

        # Arm the StatusMonitor stickiness gate so that the next provider-
        # detected PROCESSING transition is honored (overriding the latched
        # IDLE/COMPLETED). Without this, sticky ready-status would block
        # the genuine PROCESSING signal that arrives once the agent starts
        # working on the new message.
        status_monitor.notify_input_sent(terminal_id)

        get_backend().send_keys(
            metadata["tmux_session"],
            metadata["tmux_window"],
            message,
            enter_count=enter_count,
            force_bracketed_paste=True,
            submit_delay=provider.paste_submit_delay if provider else 0.3,
        )

        # Notify the provider that external input was received.
        # This allows providers to adjust status
        # detection — specifically to stop reporting IDLE for the post-init
        # state and resume normal COMPLETED detection after a real task.
        if provider:
            provider.mark_input_received()

        update_last_active(terminal_id)
        logger.info(f"Sent input to terminal: {terminal_id}")
        if registry is not None and sender_id is not None and orchestration_type is not None:
            dispatch_plugin_event(
                registry,
                "post_send_message",
                PostSendMessageEvent(
                    session_id=metadata["tmux_session"],
                    sender=sender_id,
                    receiver=terminal_id,
                    message=original_message,
                    orchestration_type=orchestration_type,
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
    ``STATE_BUFFER_MAX`` bytes (8KB); it falls back to a tmux history capture
    only when that buffer is empty. This is a deliberate trade-off in the
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
                raise last_err  # type: ignore[misc]

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


def delete_terminal(terminal_id: str, registry: PluginRegistry | None = None) -> bool:
    """Delete terminal and kill its tmux window."""
    try:
        # Unregister from herdr inbox service
        svc = get_herdr_inbox_service()
        if svc:
            try:
                svc.unregister_terminal(terminal_id)
            except Exception as e:
                logger.warning(f"Failed to unregister terminal {terminal_id} from herdr inbox: {e}")

        # Get metadata before deletion
        metadata = get_terminal_metadata(terminal_id)

        if metadata:
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
                    "working_directory": get_backend().get_pane_working_directory(
                        metadata["tmux_session"], metadata["tmux_window"]
                    ),
                    "allowed_tools": metadata.get("allowed_tools"),
                    "caller_id": metadata.get("caller_id"),
                }
                snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
                snapshot_path.write_text(_json.dumps(snapshot, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning(f"Failed to snapshot terminal {terminal_id}: {e}")

            # Stop pipe-pane logging
            try:
                get_backend().stop_pipe_pane(metadata["tmux_session"], metadata["tmux_window"])
            except Exception as e:
                logger.warning(f"Failed to stop pipe-pane for {terminal_id}: {e}")

            # Stop FIFO reader and cleanup FIFO file. Must run BEFORE kill_window
            # so the reader thread (which reopens the FIFO on EOF) unblocks and
            # joins before the pane disappears.
            try:
                fifo_manager.stop_reader(terminal_id)
            except Exception as e:
                logger.warning(f"Failed to stop FIFO reader for {terminal_id}: {e}")

            # Clear state detector buffers for this terminal
            try:
                status_monitor.clear_terminal(terminal_id)
            except Exception as e:
                logger.warning(f"Failed to clear state detector for {terminal_id}: {e}")

            # Kill the tmux window (this terminates the agent process)
            try:
                get_backend().kill_window(metadata["tmux_session"], metadata["tmux_window"])
            except Exception as e:
                logger.warning(f"Failed to kill tmux window for {terminal_id}: {e}")

        # Cleanup provider state and database record
        provider_manager.cleanup_provider(terminal_id)
        with _memory_injected_lock:
            _memory_injected_terminals.discard(terminal_id)
        # Drop any per-curator dispatch lock so the registry doesn't grow
        # forever as memory_manager terminals come and go.
        from cli_agent_orchestrator.services.memory_service import _curator_locks

        _curator_locks.pop(terminal_id, None)
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

    except Exception as e:
        logger.error(f"Failed to delete terminal {terminal_id}: {e}")
        raise
