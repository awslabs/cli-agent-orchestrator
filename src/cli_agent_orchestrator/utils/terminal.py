"""Session utilities for CLI Agent Orchestrator."""

import asyncio
import logging
import re
import time
import uuid
from typing import Optional, Union

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, SESSION_PREFIX
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

# Allowlist for tmux session/window names. tmux uses ':' and '.' as target
# delimiters and treats leading '-' as an option, so we constrain names to
# safe characters only. The 64-char cap matches typical tmux name lengths.
_VALID_TMUX_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,63}$")


def validate_tmux_name(name: str, kind: str = "name") -> str:
    """Validate a tmux session or window name against an allowlist.

    Rejects names containing tmux target delimiters (':', '.'), shell
    metacharacters, leading dashes (parsed as flags), or any character
    outside ``[A-Za-z0-9_-]``. The first character must be alphanumeric
    or underscore.

    Args:
        name: Candidate session or window name.
        kind: Label used in the error message (e.g. ``"session_name"``).

    Returns:
        The validated name unchanged.

    Raises:
        ValueError: If ``name`` is not a string or fails the allowlist.
    """
    # fullmatch() (not match()): Python's `$` anchor can match before a
    # trailing newline, so `match()` would accept `"name\n"`. fullmatch
    # forces the entire string to satisfy the pattern.
    if not isinstance(name, str) or not _VALID_TMUX_NAME.fullmatch(name):
        # Use repr() so control characters (newlines, escapes) in a hostile
        # name cannot smuggle log/response-injection payloads through the
        # error string.
        raise ValueError(f"Invalid {kind}: {name!r}")
    return name


def generate_session_name() -> str:
    """Generate a unique session name with SESSION_PREFIX."""
    session_uuid = uuid.uuid4().hex[:8]
    return validate_tmux_name(f"{SESSION_PREFIX}{session_uuid}", "session_name")


def generate_terminal_id() -> str:
    """Generate terminal ID without prefix."""
    return uuid.uuid4().hex[:8]


def generate_window_name(agent_profile: str) -> str:
    """Generate window name from agent profile with unique suffix."""
    return validate_tmux_name(f"{agent_profile}-{uuid.uuid4().hex[:4]}", "window_name")


def _resolve_window(terminal_id: str) -> "tuple[str, str] | None":
    """Resolve (session_name, window_name) for a terminal from its provider.

    The provider is registered (provider_manager.create_provider) before
    initialize() runs, so it is the most reliable in-process source for the
    backend coordinates the wait helpers need when they have to query the
    backend directly. Returns None if no provider is registered for
    ``terminal_id`` yet.
    """
    from cli_agent_orchestrator.providers.manager import provider_manager

    provider = provider_manager.get_provider(terminal_id)
    if provider is None:
        return None
    return provider.session_name, provider.window_name


async def wait_for_shell(
    terminal_id: str,
    timeout: float = 10.0,
    stable_duration: float = 2.0,
    polling_interval: float = 0.3,
) -> bool:
    """Wait for shell to be ready by checking if the output buffer is stable and non-empty.

    For pipe-pane backends (tmux) this reads the StatusMonitor's in-memory
    buffer, populated by the FIFO reader → event bus → StatusMonitor pipeline.
    Returns True when the buffer is non-empty and has not changed for
    *stable_duration* seconds.

    Event-inbox backends (herdr) deliberately skip that pipeline — their
    pipe_pane is a no-op and create_terminal() never starts a FIFO reader for
    them (the FIFO setup is gated on ``not supports_event_inbox()``), so the
    StatusMonitor buffer would stay empty forever and this would always time
    out. For those backends we read pane output directly via
    ``backend.get_history()`` instead.

    This does NOT use provider-specific status detection because the provider
    is already registered before initialize() runs, and provider patterns
    don't match raw shell output.
    """
    from cli_agent_orchestrator.backends.registry import get_backend
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    backend = get_backend()
    window = _resolve_window(terminal_id) if backend.supports_event_inbox() else None
    if backend.supports_event_inbox() and window is None:
        logger.warning(
            f"wait_for_shell [{terminal_id}]: event-inbox backend but no provider "
            f"registered; falling back to (empty) StatusMonitor buffer"
        )

    if window is not None:
        session_name, window_name = window

        def read_buffer() -> str:
            try:
                return backend.get_history(session_name, window_name, strip_escapes=True)
            except Exception as e:
                logger.debug(f"wait_for_shell [{terminal_id}]: backend history read failed: {e}")
                return ""

    else:

        def read_buffer() -> str:
            return status_monitor.get_buffer(terminal_id)

    logger.info(f"Waiting for shell to be ready for terminal {terminal_id}...")

    deadline = time.time() + timeout
    previous_buffer = ""
    last_change = time.time()

    while time.time() < deadline:
        buf = read_buffer()

        if buf != previous_buffer:
            previous_buffer = buf
            last_change = time.time()

        stable_elapsed = time.time() - last_change

        if buf.strip() and stable_elapsed >= stable_duration:
            logger.info(f"Shell ready for {terminal_id} (buffer stable, {len(buf)} bytes)")
            return True

        await asyncio.sleep(polling_interval)

    logger.warning(f"Timeout waiting for shell to be ready for {terminal_id}")
    return False


async def wait_until_status(
    terminal_id: str,
    target_status: "TerminalStatus | set[TerminalStatus]",
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status by polling status_monitor.

    status_monitor.get_status() is backend-aware: for pipe-pane backends (tmux)
    it returns the pushed pipeline status, and for event-inbox backends (herdr)
    it derives status on demand from the provider's native status. So this poll
    works for both backends without special-casing here.
    """
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    targets = target_status if isinstance(target_status, set) else {target_status}
    target_str = ", ".join(s.value for s in targets)
    logger.info(
        f"wait_until_status [{terminal_id}]: waiting for {{{target_str}}}, timeout={timeout}s"
    )
    start = time.time()
    while time.time() - start < timeout:
        current = status_monitor.get_status(terminal_id)
        if current in targets:
            logger.info(f"wait_until_status [{terminal_id}]: reached {current.value}")
            return True
        await asyncio.sleep(polling_interval)
    logger.warning(f"wait_until_status [{terminal_id}]: timeout waiting for {{{target_str}}}")
    return False


def sync_backend_from_server() -> None:
    """Query the running cao-server's /health endpoint and align the local backend singleton.

    When ``cao-server --terminal herdr`` is used without setting ``terminal_backend``
    in config.json, CLI processes that call ``get_backend()`` default to tmux.
    This function bridges the gap by reading the server's active backend and
    calling ``set_backend()`` so subsequent ``get_backend()`` calls return the
    correct backend type.

    Failures (server unreachable, unexpected response) are logged and silently
    ignored — the CLI falls back to its normal config-based resolution.
    """
    from cli_agent_orchestrator.backends.factory import BackendFactory
    from cli_agent_orchestrator.backends.registry import set_backend

    try:
        resp = requests.get(f"{API_BASE_URL}/health", timeout=2.0)
        resp.raise_for_status()
        data = resp.json()
        backend_name = data.get("terminal_backend")
        if backend_name:
            set_backend(BackendFactory.create(backend_override=backend_name))
    except Exception as e:
        logger.debug(f"sync_backend_from_server: could not reach server: {e}")


def poll_until_done(
    terminal_id: str,
    timeout: float,
    polling_interval: float = 1.0,
    stable_polls: int = 3,
) -> None:
    """Poll terminal status until the agent is done (ready), errored, or timeout.

    "Done" is a ready status — COMPLETED or IDLE — that persists for
    ``stable_polls`` consecutive polls. Both count because providers differ in
    how they signal a finished turn: most reach COMPLETED (a detectable
    response-done marker like a Credits line or green arrow), but kiro-cli 2.11
    frequently finishes a turn with no such marker and settles back to its
    persistent idle prompt, so the detector reports IDLE for a genuinely
    finished turn. Requiring COMPLETED only would hang here (and time out)
    whenever a kiro agent completes — even though it is done. The rest of the
    system already treats IDLE/COMPLETED as equivalent ready states.

    The ``stable_polls`` requirement guards against returning on a transient
    idle *before* the agent has started working (callers sleep briefly before
    calling this, but that alone can't distinguish "idle, not started" from
    "idle, finished" — a stable ready window can).

    Raises click.ClickException on error, timeout, or request failure.
    """
    import click

    done_states = {TerminalStatus.COMPLETED.value, TerminalStatus.IDLE.value}
    start = time.time()
    consecutive_ready = 0
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            raise click.ClickException(
                f"Timed out after {int(elapsed)}s waiting for terminal {terminal_id}"
            )
        try:
            resp = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
            resp.raise_for_status()
            status = resp.json().get("status")
            if status == TerminalStatus.ERROR.value:
                raise click.ClickException("Terminal reached ERROR status")
            if status in done_states:
                consecutive_ready += 1
                if consecutive_ready >= stable_polls:
                    return
            else:
                consecutive_ready = 0
        except requests.exceptions.RequestException as e:
            raise click.ClickException(f"Failed to poll terminal status: {e}")
        time.sleep(polling_interval)


def wait_until_terminal_status(
    terminal_id: str,
    target_status: Union[TerminalStatus, set],
    timeout: float = 30.0,
    polling_interval: float = 1.0,
) -> bool:
    """Wait until terminal reaches target status by polling GET /terminals/{id}.

    Args:
        terminal_id: Terminal to poll status for.
        target_status: A single TerminalStatus or a set of acceptable statuses.
        timeout: Maximum wait time in seconds.
        polling_interval: Seconds between polls.

    Returns:
        True if the terminal reached one of the target statuses within timeout.
    """
    if isinstance(target_status, TerminalStatus):
        target_values = {target_status.value}
    else:
        target_values = {s.value for s in target_status}

    logger.info(
        f"wait_until_terminal_status [{terminal_id}]: waiting for "
        f"{{{', '.join(target_values)}}}, timeout={timeout}s"
    )
    start_time = time.time()
    last_seen: Optional[str] = None
    poll_count = 0
    while time.time() - start_time < timeout:
        poll_count += 1
        try:
            response = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}", timeout=5.0)
            if response.status_code == 200:
                current_status = response.json().get("status")
                last_seen = current_status
                if current_status in target_values:
                    logger.info(
                        f"wait_until_terminal_status [{terminal_id}]: reached "
                        f"{current_status} after {poll_count} polls "
                        f"({time.time() - start_time:.1f}s)"
                    )
                    return True
        except Exception as e:
            logger.debug(
                f"wait_until_terminal_status [{terminal_id}] poll #{poll_count} error: {e}"
            )
        time.sleep(polling_interval)
    logger.warning(
        f"wait_until_terminal_status [{terminal_id}]: timeout after {timeout}s "
        f"(polls={poll_count}, last_seen={last_seen!r})"
    )
    return False
