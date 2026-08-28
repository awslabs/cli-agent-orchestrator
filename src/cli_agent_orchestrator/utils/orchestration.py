"""Shared implementation for CAO's in-session orchestration primitives.

This module is the SINGLE seam behind both entry points that let one agent
orchestrate another: the ``assign``/``handoff``/``send_message``/
``delete_terminal`` tools registered on ``cao-mcp-server``
(``mcp_server/server.py``) and the ``cao agent ...`` CLI commands
(``cli/commands/agent.py``, issue #616). Both are thin wrappers that call the
functions here and render the result for their own transport (an MCP tool
return value vs. stdout/exit-code) -- neither entry point re-implements this
logic, so behavior can never drift between "orchestrate via MCP" and
"orchestrate via the CLI escape hatch" (e.g. when a terminal's MCP child
process has died but its shell is still alive).

HTTP-only: like ``mcp_server/``, every function here reaches Backplane state
exclusively through cao-server's FastAPI surface over ``API_BASE_URL``
(``requests``) -- never through ``clients.tmux`` / ``clients.database``.
This module lives under ``utils/`` (not ``mcp_server/``) specifically so the
CLI can import it without pulling in ``mcp_server/server.py``'s module-level
FastMCP server construction and tool registration (a side-effecting, heavier
import a short-lived CLI process has no reason to pay for).
"""

import logging
import os
import re
import time
from typing import Any, Callable, Dict, NamedTuple, Optional, Tuple

import requests

from cli_agent_orchestrator.constants import API_BASE_URL, DEFAULT_PROVIDER
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider
from cli_agent_orchestrator.utils.terminal import generate_session_name

logger = logging.getLogger(__name__)


def _mcp_timeout() -> float:
    """Get MCP request timeout from server settings."""
    return float(get_server_settings()["mcp_request_timeout"])


def _auth_headers() -> Dict[str, str]:
    """Return the ``Authorization`` header for the internal client->API hop, if any.

    Mirrors ``mcp_server.utils._auth_headers`` / ``mcp_server.app_tools._auth_headers``:
    attaches the operator-provisioned ``CAO_AUTH_LOCAL_TOKEN`` when the auth layer is
    enabled, and returns an empty mapping default-off so the no-auth posture stays
    byte-for-byte unchanged. Every ``requests`` call in this module passes
    ``headers=_auth_headers() or None`` -- without this, an auth-enabled deployment's
    cao-server rejects every one of these calls with a 401 and the CLI/MCP orchestration
    surface (assign, handoff, send_message, status, result, cancel, delete_terminal)
    cannot be used at all.
    """
    token = get_local_bearer()
    return {"Authorization": f"Bearer {token}"} if token else {}


# Environment variable to enable/disable automatic sender terminal ID injection.
# Defaults to enabled (issue #284): callback routing must not depend on the
# supervisor LLM remembering to hand-write its terminal ID into the message.
ENABLE_SENDER_ID_INJECTION = os.getenv("CAO_ENABLE_SENDER_ID_INJECTION", "true").lower() == "true"

# Terminal count threshold for cleanup nudge
TERMINAL_CLEANUP_NUDGE_THRESHOLD = 10

# Generous client-side timeout for a SYNCHRONOUS (non-deferred) terminal create
# call, used by handoff's early-terminal-id path (review on PR #634, issue #616).
# Provider init (shell warm-up + CLI startup + MCP registration + auth) can
# legitimately take up to ~45s server-side -- well past _mcp_timeout()'s 30s
# default. That default was never a problem before because nothing called
# _create_terminal non-deferred in production (assign always uses
# defer_init=True); this is the first caller of that path, so it gets its own
# padded timeout rather than silently inheriting one sized for something else.
_HANDOFF_CREATE_TIMEOUT_S = 150.0
_TERMINAL_ID_PATTERN = re.compile(r"^[a-f0-9]{8}$")


def _current_terminal_id() -> Optional[str]:
    """Return a valid CAO terminal ID from the calling process's environment, if configured.

    The canonical resolver for "who is calling" -- shared by the MCP tools
    (via ``CAO_TERMINAL_ID`` in the MCP subprocess's env) and the ``cao
    agent`` CLI commands (via the same env var in the invoking shell). Same
    validation either way: an unset var means "no caller identity available"
    (``None``), a malformed one is a hard error, never silently ignored.
    """
    terminal_id = os.environ.get("CAO_TERMINAL_ID")
    if not terminal_id:
        return None
    if not _TERMINAL_ID_PATTERN.fullmatch(terminal_id):
        raise ValueError(
            "Invalid CAO_TERMINAL_ID: expected an 8-character lowercase hexadecimal terminal ID"
        )
    return terminal_id


def _get_cleanup_nudge() -> str:
    """Return a cleanup nudge string if the session has too many terminals, else empty string."""
    try:
        current_terminal_id = _current_terminal_id()
        if not current_terminal_id:
            return ""
        resp = requests.get(
            f"{API_BASE_URL}/terminals/{current_terminal_id}",
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        if resp.status_code != 200:
            return ""
        session_name = resp.json().get("session_name")
        if not session_name:
            return ""
        resp = requests.get(
            f"{API_BASE_URL}/sessions/{session_name}/terminals",
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        if resp.status_code != 200:
            return ""
        count = len(resp.json())
        if count >= TERMINAL_CLEANUP_NUDGE_THRESHOLD:
            return (
                f" NOTE: This session has {count} terminals. "
                f"Consider calling delete_terminal on terminals you no longer need."
            )
    except Exception:
        pass
    return ""


def _resolve_child_allowed_tools(
    parent_allowed_tools: Optional[list], child_profile_name: str
) -> Optional[str]:
    """Resolve allowed_tools for a child terminal via intersection.

    The child gets at most the union of: what the parent allows + what the
    child profile specifies. If the parent is unrestricted ("*"), the child
    profile's allowedTools are used as-is.

    Returns:
        Comma-separated string of allowed tools, or None for unrestricted.
    """
    from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
    from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

    try:
        child_profile = load_agent_profile(child_profile_name)
        mcp_server_names = (
            list(child_profile.mcpServers.keys()) if child_profile.mcpServers else None
        )
        child_allowed = resolve_allowed_tools(
            child_profile.allowedTools, child_profile.role, mcp_server_names
        )
    except FileNotFoundError:
        child_allowed = None

    # If parent is unrestricted or has no restrictions, use child's tools
    if parent_allowed_tools is None or "*" in parent_allowed_tools:
        if child_allowed:
            return ",".join(child_allowed)
        return None

    # If child has no opinion (None), inherit parent's restrictions
    if child_allowed is None:
        return ",".join(parent_allowed_tools)

    # If child explicitly requests unrestricted ("*"), honor it
    if "*" in child_allowed:
        return None

    # Both have restrictions: child gets its own profile tools
    # (the child profile defines what it needs; parent's restrictions
    # are enforced by the parent not delegating unauthorized work)
    return ",".join(child_allowed)


def _create_terminal(
    agent_profile: str,
    working_directory: Optional[str] = None,
    engine: Optional[str] = None,
    defer_init: bool = False,
    initial_message: Optional[str] = None,
    initial_message_orchestration_type: Optional[OrchestrationType] = None,
    model: Optional[str] = None,
    use_worktree: bool = False,
    create_timeout: Optional[float] = None,
    idempotency_key: Optional[str] = None,
) -> Tuple[str, str]:
    """Create a new terminal with the specified agent profile.

    Args:
        agent_profile: Agent profile for the terminal
        working_directory: Optional working directory for the terminal
        idempotency_key: Review on PR #634, issue #616. Forwarded as a query
            param to whichever endpoint this call hits (existing-session or
            new-session); the server returns the terminal a PRIOR call with
            the SAME key already created instead of creating a new one --
            safe to pass again after a lost response. See
            ``terminal_service.create_terminal``'s docstring for the
            mechanics. ``None`` (default): today's behavior, unprotected.
        create_timeout: Client-side timeout for the create POST only (the
            metadata/working-directory GETs above it keep using
            ``_mcp_timeout()`` regardless -- they're fast reads either way).
            ``None`` (default) keeps today's behavior (``_mcp_timeout()``,
            30s), which is fine for ``defer_init=True`` (assign's path:
            returns in <2s by design) but too short for a SYNCHRONOUS create
            that waits out ``provider.initialize()`` (up to ~45s) -- pass an
            explicit, larger value for that case (see
            ``_HANDOFF_CREATE_TIMEOUT_S``).
        defer_init: If True, tell
            cao-server to skip the ``provider.initialize()`` wait and return
            as soon as the tmux window and DB record exist. Provider init
            (and, when ``initial_message`` is set, delivery of that message)
            runs as a background task on cao-server. The tool-call round-trip
            drops from tens of seconds to <2s, keeping it well under
            kiro-cli 2.11's ~60s per-tool client timeout.
        initial_message: This message is delivered to the newly created worker
            once its provider finishes initializing. For a new session, the
            message selects deferred initialization automatically; for an
            existing session, ``defer_init=True`` is required.
        initial_message_orchestration_type: Passed through to send_input for
            plugin event emission (assign/handoff).
        engine: Explicit Kiro engine for the child terminal.
        model: Explicit per-call model override for the new terminal, applied
            ahead of the agent profile's own static model field (where the
            resolved provider supports it). Honored by both the existing-
            session and new-session branches.
        use_worktree: If True, the created terminal gets an isolated git
            worktree (issue #100 Phase 1) instead of sharing
            ``working_directory`` as given. Honored by both the existing-
            session (assign/handoff) branch and the new-session branch --
            the latter previously dropped it silently (review on PR #634:
            a fresh-session ``cao agent handoff --use-worktree`` reported
            success while quietly not isolating the checkout) until
            ``POST /sessions`` grew the same parameter its
            ``/sessions/{name}/terminals`` sibling already had.

    Returns:
        Tuple of (terminal_id, provider)

    Raises:
        Exception: If terminal creation fails
    """
    provider = DEFAULT_PROVIDER
    parent_allowed_tools = None

    # Get current terminal ID from environment
    current_terminal_id = _current_terminal_id()
    if current_terminal_id:
        # Get terminal metadata via API
        response = requests.get(
            f"{API_BASE_URL}/terminals/{current_terminal_id}",
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        response.raise_for_status()
        terminal_metadata = response.json()

        # Treat the supervisor provider as a fallback, not an explicit override.
        provider = resolve_provider(agent_profile, fallback_provider=terminal_metadata["provider"])
        session_name = terminal_metadata["session_name"]
        parent_allowed_tools = terminal_metadata.get("allowed_tools")

        # If no working_directory specified, get conductor's current directory
        if working_directory is None:
            try:
                response = requests.get(
                    f"{API_BASE_URL}/terminals/{current_terminal_id}/working-directory",
                    headers=_auth_headers() or None,
                    timeout=_mcp_timeout(),
                )
                if response.status_code == 200:
                    working_directory = response.json().get("working_directory")
                    logger.info(f"Inherited working directory from conductor: {working_directory}")
                else:
                    logger.warning(
                        f"Failed to get conductor's working directory (status {response.status_code}), "
                        "will use server default"
                    )
            except Exception as e:
                logger.warning(
                    f"Error fetching conductor's working directory: {e}, will use server default"
                )

        # Resolve child's allowed_tools via inheritance
        child_allowed_tools = _resolve_child_allowed_tools(parent_allowed_tools, agent_profile)

        # Create new terminal in existing session - always pass working_directory
        params = {"provider": provider, "agent_profile": agent_profile}
        # Record the creating terminal so send_message can route callbacks
        # structurally instead of parsing IDs out of message text (issue #284).
        params["caller_id"] = current_terminal_id
        if working_directory:
            params["working_directory"] = working_directory
        if child_allowed_tools:
            params["allowed_tools"] = child_allowed_tools
        if provider == ProviderType.KIRO_CLI.value and engine is not None:
            params["engine"] = engine
        if model and model.strip():
            params["model"] = model
        if use_worktree:
            params["use_worktree"] = "true"
        if idempotency_key:
            params["idempotency_key"] = idempotency_key
        # The message payload goes in the JSON body, not the query string, so
        # prompt content isn't exposed in HTTP access logs and isn't subject to
        # URL-length limits. Only routing flags stay in params.
        json_body = None
        if defer_init:
            params["defer_init"] = "true"
            json_body = {}
            if initial_message is not None:
                json_body["initial_message"] = initial_message
            if initial_message_orchestration_type is not None:
                json_body["initial_message_orchestration_type"] = (
                    initial_message_orchestration_type.value
                    if isinstance(initial_message_orchestration_type, OrchestrationType)
                    else str(initial_message_orchestration_type)
                )

        response = requests.post(
            f"{API_BASE_URL}/sessions/{session_name}/terminals",
            params=params,
            json=json_body,
            headers=_auth_headers() or None,
            timeout=create_timeout if create_timeout is not None else _mcp_timeout(),
        )
        response.raise_for_status()
        terminal = response.json()
    else:
        # Create new session with terminal.
        # POST /sessions automatically uses deferred init when an initial
        # message is present. A bare defer_init flag still cannot be represented
        # on that endpoint, so reject that narrower shape rather than silently
        # changing it to synchronous initialization.
        if defer_init and initial_message is None:
            raise ValueError(
                "defer_init requires initial_message when creating a new session "
                "(no current CAO_TERMINAL_ID)"
            )
        session_name = generate_session_name()
        provider = resolve_provider(agent_profile, fallback_provider=provider)
        params = {
            "provider": provider,
            "agent_profile": agent_profile,
            "session_name": session_name,
        }
        if working_directory:
            params["working_directory"] = working_directory
        if provider == ProviderType.KIRO_CLI.value and engine is not None:
            params["engine"] = engine
        if model and model.strip():
            params["model"] = model
        if use_worktree:
            params["use_worktree"] = "true"
        if idempotency_key:
            params["idempotency_key"] = idempotency_key

        json_body = None
        if initial_message is not None:
            json_body = {"initial_message": initial_message}
            if initial_message_orchestration_type is not None:
                json_body["initial_message_orchestration_type"] = (
                    initial_message_orchestration_type.value
                    if isinstance(initial_message_orchestration_type, OrchestrationType)
                    else str(initial_message_orchestration_type)
                )

        response = requests.post(
            f"{API_BASE_URL}/sessions",
            params=params,
            json=json_body,
            headers=_auth_headers() or None,
            timeout=create_timeout if create_timeout is not None else _mcp_timeout(),
        )
        response.raise_for_status()
        terminal = response.json()

    return terminal["id"], provider


def _send_direct_input(
    terminal_id: str, message: str, orchestration_type: OrchestrationType
) -> None:
    """Send input directly to a terminal (bypasses inbox).

    Args:
        terminal_id: Terminal ID
        message: Message to send
        orchestration_type: Orchestration mode for plugin event emission

    Raises:
        Exception: If sending fails
    """
    response = requests.post(
        f"{API_BASE_URL}/terminals/{terminal_id}/input",
        params={
            "message": message,
            # "supervisor" fallback is safe here: sender_id is a display label
            # for plugin event emission, never a routable callback address
            # (unlike the hard-error paths added for issue #284).
            "sender_id": os.environ.get("CAO_TERMINAL_ID", "supervisor"),
            "orchestration_type": orchestration_type,
        },
        headers=_auth_headers() or None,
        timeout=_mcp_timeout(),
    )
    response.raise_for_status()


def _shape_handoff_message(provider: str, message: str) -> str:
    """Return the handoff prompt, prepending the codex [CAO Handoff] banner.

    Codex needs to be told this is a blocking handoff so it outputs results
    directly rather than calling send_message back to the supervisor. The
    banner embeds this caller's CAO_TERMINAL_ID -- which is why prompt
    shaping stays caller-side (the cao-server process does not have it).
    Other providers get the message unchanged.

    Raises:
        ValueError: codex provider with no CAO_TERMINAL_ID — never tell a worker
            its supervisor is terminal 'unknown' (issue #284).
    """
    if provider != "codex":
        return message

    supervisor_id = _current_terminal_id()
    if not supervisor_id:
        raise ValueError(
            "CAO_TERMINAL_ID not set - cannot identify the supervisor terminal "
            "for the handoff context. Run handoff from inside a CAO terminal."
        )
    return (
        f"[CAO Handoff] Supervisor terminal ID: {supervisor_id}. "
        "This is a blocking handoff — the orchestrator will automatically "
        "capture your response when you finish. Complete the task and output "
        "your results directly. Do NOT use send_message to notify the supervisor "
        "unless explicitly needed — just do the work and present your deliverables.\n\n"
        f"{message}"
    )


def _send_direct_input_handoff(terminal_id: str, provider: str, message: str) -> None:
    """Send handoff payload to an agent, prepending orchestrator instructions if needed.

    Retained for the assign path and any direct callers; the codex banner logic
    lives in ``_shape_handoff_message`` so the single-seam handoff path and this
    direct path produce byte-identical shaped prompts.
    """
    handoff_message = _shape_handoff_message(provider, message)
    _send_direct_input(terminal_id, handoff_message, OrchestrationType.HANDOFF)


class HandoffContext(NamedTuple):
    """Supervisor-derived context for a handoff, resolved WITHOUT creating a terminal.

    The worker terminal must be created in the SAME tmux session as the
    supervisor, inherit the supervisor's allowed-tools, and record the
    supervisor as its caller (issue #284). These are resolved caller-side from
    the supervisor metadata so the single combined run-step call carries them.
    """

    provider: str
    session_name: Optional[str]
    caller_id: Optional[str]
    allowed_tools: Optional[list]


def _resolve_handoff_provider(agent_profile: str) -> HandoffContext:
    """Resolve the handoff context for a worker WITHOUT creating a terminal.

    Mirrors the resolution branch of the former ``_create_terminal``: a worker
    inherits the supervisor's provider as a FALLBACK (not an override), is placed
    in the supervisor's session, records the supervisor as ``caller_id`` (#284),
    and inherits the supervisor's allowed-tools intersected with the child
    profile. When NOT run inside a CAO terminal there is no supervisor: a fresh
    session is auto-created (``session_name=None``) and no caller is recorded.

    This lets the codex fast-fail and codex prompt-shaping run caller-side before
    the single combined run-step call, while preserving the same-session /
    caller_id / allowed_tools behavior the old six-call path had.
    """
    current_terminal_id = _current_terminal_id()
    if not current_terminal_id:
        return HandoffContext(
            provider=resolve_provider(agent_profile, fallback_provider=DEFAULT_PROVIDER),
            session_name=None,
            caller_id=None,
            allowed_tools=None,
        )

    response = requests.get(
        f"{API_BASE_URL}/terminals/{current_terminal_id}",
        headers=_auth_headers() or None,
        timeout=_mcp_timeout(),
    )
    response.raise_for_status()
    terminal_metadata = response.json()

    provider = resolve_provider(agent_profile, fallback_provider=terminal_metadata["provider"])
    # Resolve the child's allowed-tools via the same inheritance the old path
    # used; _resolve_child_allowed_tools returns a comma-separated string (or
    # None for unrestricted), which we split into the list the payload expects.
    parent_allowed_tools = terminal_metadata.get("allowed_tools")
    child_allowed_tools = _resolve_child_allowed_tools(parent_allowed_tools, agent_profile)
    allowed_tools_list = child_allowed_tools.split(",") if child_allowed_tools else None
    return HandoffContext(
        provider=provider,
        session_name=terminal_metadata["session_name"],
        caller_id=current_terminal_id,
        allowed_tools=allowed_tools_list,
    )


def _terminal_id_from_detail(detail: str) -> Optional[str]:
    """Best-effort extraction of an 8-hex terminal id from an error detail.

    Fallback for an older server that returns a plain-string ``detail`` instead
    of the structured object. The current run-step endpoint returns terminal_id
    as a structured field (see ``_parse_run_step_error``); this regex is only
    used when that field is absent.
    """
    match = re.search(r"terminal ([a-f0-9]{8})\b", detail)
    return match.group(1) if match else None


def _parse_run_step_error(
    response: requests.Response,
) -> tuple[Optional[str], str, Optional[str]]:
    """Parse a run-step error response into ``(kind, message, terminal_id)``.

    The run-step endpoint returns a STRUCTURED detail object
    ``{"message", "kind", "terminal_id"}`` so callers read the failure kind and
    the live terminal as fields. Falls back to the legacy plain-string detail
    (+ regex terminal-id scrape) when the structured shape is absent, so a
    newer client still works against an older server.
    """
    try:
        payload = response.json()
    except ValueError:
        fallback = f"status {response.status_code}"
        return None, fallback, None

    detail = payload.get("detail")
    if isinstance(detail, dict):
        message = detail.get("message") or f"status {response.status_code}"
        return detail.get("kind"), message, detail.get("terminal_id")
    if isinstance(detail, str) and detail:
        return None, detail, _terminal_id_from_detail(detail)
    fallback = f"status {response.status_code}"
    return None, fallback, None


def _send_to_inbox(receiver_id: str, message: str) -> Dict[str, Any]:
    """Send message to another terminal's inbox (queued delivery when IDLE).

    Args:
        receiver_id: Target terminal ID
        message: Message content

    Returns:
        Dict with message details

    Raises:
        ValueError: If CAO_TERMINAL_ID not set
        Exception: If API call fails
    """
    sender_id = _current_terminal_id()
    if not sender_id:
        raise ValueError("CAO_TERMINAL_ID not set - cannot determine sender")

    response = requests.post(
        f"{API_BASE_URL}/terminals/{receiver_id}/inbox/messages",
        params={
            "sender_id": sender_id,
            "message": message,
        },
        headers=_auth_headers() or None,
        timeout=_mcp_timeout(),
    )
    response.raise_for_status()
    data: Dict[str, Any] = response.json()
    return data


def _extract_error_detail(response: requests.Response, fallback: str) -> str:
    """Extract a human-readable error detail from an API response."""
    try:
        payload = response.json()
    except ValueError:
        return fallback

    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return fallback


async def _run_step_and_build_result(
    payload: Dict[str, Any],
    agent_profile: str,
    provider: str,
    timeout: int,
    start_time: float,
) -> HandoffResult:
    """POST ``payload`` to ``/terminals/run-step`` and map the response to a HandoffResult.

    Shared by both of ``_handoff_impl``'s call shapes: the default single-call
    path (``payload`` carries a fresh ``session_name``/``caller_id``/
    ``allowed_tools`` for the server to create the worker with) and the
    early-terminal-id path (``payload`` carries ``reuse_terminal_id`` instead,
    for a terminal ``_handoff_impl`` already created and reported). Response
    interpretation -- timeout vs worker-error vs malformed-200 vs success -- is
    identical either way, so it lives here once rather than twice.

    When ``payload`` carries ``reuse_terminal_id``, an error response's
    ``terminal_id`` falls back to that value: the caller already knows this
    terminal exists, so there is no reason to lose track of it even if a
    legacy plain-string ``detail`` happens not to name it. For the fresh-create
    path ``reuse_terminal_id`` is absent, so this reduces to exactly the prior
    behavior (surface ``tid`` from the error detail, or ``None``).
    """
    known_terminal_id: Optional[str] = payload.get("reuse_terminal_id")
    # Allow the full step time plus the server-side ready-wait (up to 120s)
    # plus headroom; the server enforces the per-step timeout internally.
    client_timeout = float(timeout) + 180.0
    try:
        response = requests.post(
            f"{API_BASE_URL}/terminals/run-step",
            json=payload,
            headers=_auth_headers() or None,
            timeout=client_timeout,
        )
    except requests.Timeout:
        return HandoffResult(
            success=False,
            message=f"Handoff timed out after {timeout} seconds",
            output=None,
            terminal_id=known_terminal_id,
        )

    if response.status_code != 200:
        # Map the boundary's HTTPException back into a HandoffResult. The
        # run-step endpoint returns a STRUCTURED detail object
        # ({message, kind, terminal_id}) so we read terminal_id and the
        # failure kind as fields rather than scraping the message.
        kind, structured_detail, tid = _parse_run_step_error(response)
        # worker RAN LONG (timeout) vs CRASHED (terminal reached ERROR) must
        # be reported distinctly so a 5s crash is not mislabeled as an
        # N-second timeout. The structured `kind` is authoritative; the
        # status code is only the fallback when an older server omits it
        # (504 -> timeout, 502 -> error).
        if kind == "error" or (kind is None and response.status_code == 502):
            msg = f"Handoff failed: worker errored ({structured_detail})"
        elif kind == "timeout" or (kind is None and response.status_code == 504):
            msg = f"Handoff timed out after {timeout} seconds"
        else:
            msg = f"Handoff failed: {structured_detail}"
        return HandoffResult(
            success=False, message=msg, output=None, terminal_id=tid or known_terminal_id
        )

    data = response.json()
    terminal_id = data.get("terminal_id", known_terminal_id)
    # A 200 must carry last_message; surface a malformed body as a failure
    # rather than silently returning success-with-None.
    if "last_message" not in data:
        return HandoffResult(
            success=False,
            message="Handoff failed: malformed run-step response (no last_message)",
            output=None,
            terminal_id=terminal_id,
        )
    output = data["last_message"]

    execution_time = time.time() - start_time
    return HandoffResult(
        success=True,
        message=f"Successfully handed off to {agent_profile} ({provider}) in {execution_time:.2f}s"
        + _get_cleanup_nudge(),
        output=output,
        terminal_id=terminal_id,
    )


# Implementation functions
async def _handoff_impl(
    agent_profile: str,
    message: str,
    timeout: int = 600,
    working_directory: Optional[str] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    use_worktree: bool = False,
    on_terminal_id: Optional[Callable[[str], None]] = None,
    wait: bool = True,
    idempotency_key: Optional[str] = None,
) -> HandoffResult:
    """Implementation of handoff logic.

    Single-seam refactor (issue #312, N0). This is an HTTP client; it MUST NOT
    import services/clients. Its former six granular round-trips (create ->
    poll-ready -> input -> poll-complete -> output -> exit/delete) are
    collapsed into ONE call to the combined server-side ``POST
    /terminals/run-step`` endpoint, whose handler runs the shared
    ``run_agent_step`` substrate. Observable behavior is preserved (BR-8): same
    HandoffResult shape + success/failure semantics, same codex CAO_TERMINAL_ID
    fast-fail, same timeout contract, terminal auto-torn-down on success.

    Codex prompt-shaping (the [CAO Handoff] banner) stays CALLER-SIDE here: it
    depends on the CALLING PROCESS's ``CAO_TERMINAL_ID`` env var (the MCP
    subprocess, or a ``cao agent handoff`` shell), which the cao-server process
    does not have. We shape the prompt before the single call and pass the
    already-shaped text to the substrate, which sends it verbatim. This is the
    one behavior-equivalence risk flagged in the plan; keeping the shaping
    caller-side is the choice that preserves the exact existing codex banner
    regardless of which entry point (MCP tool or CLI command) calls this.

    ``on_terminal_id`` / ``wait`` (review on PR #634, issue #616): the MCP
    ``handoff`` tool calls this with neither set, taking the single-call path
    below exactly as written above -- BEHAVIOR UNCHANGED, still BR-8.
    ``cao agent handoff`` passes ``on_terminal_id`` so an operator who kills a
    blocking handoff has a real terminal_id to recover with (``cao agent
    status``/``result``/``cancel``) instead of blind-retrying into a second
    worker, and ``wait=False`` (``--no-wait``) to return immediately after
    creation without waiting, extracting, or tearing down at all.

    ``idempotency_key`` (review on PR #634, issue #616 -- haofeif's follow-up
    pass): closes the gap the paragraph above used to describe as
    out-of-scope. Forwarded to ``_create_terminal``'s early-terminal-id
    create call, which forwards it to the server; the server persists
    ``(idempotency_key -> terminal_id)`` atomically with the terminal row
    (see ``terminal_service.create_terminal``'s docstring), so a caller that
    supplies the SAME key on a retry -- including one whose FIRST attempt's
    HTTP response was lost entirely, not just a caller who saw a
    ``terminal_id`` and then died -- gets back the terminal that first,
    already-committed attempt created, not a second worker. ``None``
    (default, and always for the MCP tool): today's unprotected behavior.
    """
    start_time = time.time()
    terminal_id: Optional[str] = None

    try:
        # Resolve the supervisor context WITHOUT creating a terminal, so the
        # codex fast-fail (which needs CAO_TERMINAL_ID) can run before any
        # terminal exists, on every path below.
        ctx = _resolve_handoff_provider(agent_profile)
        provider = ctx.provider

        # Fail fast for codex: its handoff banner requires CAO_TERMINAL_ID. We
        # check before any terminal is created (no terminal_id to surface yet).
        if provider == "codex" and not _current_terminal_id():
            return HandoffResult(
                success=False,
                message=(
                    "Handoff failed: CAO_TERMINAL_ID not set - cannot identify the "
                    "supervisor terminal for the handoff context. Run handoff from "
                    "inside a CAO terminal."
                ),
                output=None,
                terminal_id=None,
            )

        if on_terminal_id is None and wait:
            # Default path: ONE combined call -- create -> ready-wait -> input ->
            # complete-wait -> extract -> teardown, all server-side via
            # run_agent_step. session_name places the worker in the supervisor's
            # session; caller_id/allowed_tools preserve #284 callback routing
            # and tool inheritance. Byte-for-byte the original single-seam
            # behavior (BR-8) -- this is what the MCP tool always takes.
            shaped_message = _shape_handoff_message(provider, message)
            payload: Dict[str, Any] = {
                "provider": provider,
                "agent": agent_profile,
                "prompt": shaped_message,
                "teardown": True,
                "timeout": float(timeout),
                "use_worktree": use_worktree,
            }
            if ctx.session_name:
                payload["session_name"] = ctx.session_name
            if ctx.caller_id:
                payload["caller_id"] = ctx.caller_id
            if ctx.allowed_tools:
                payload["allowed_tools"] = ctx.allowed_tools
            if working_directory:
                payload["working_directory"] = working_directory
            if provider == ProviderType.KIRO_CLI.value and engine is not None:
                payload["engine"] = engine
            if model and model.strip():
                payload["model"] = model
            return await _run_step_and_build_result(
                payload, agent_profile, provider, timeout, start_time
            )

        # Early-terminal-id path: create SYNCHRONOUSLY first (waits out
        # provider-ready server-side, same wait the default path's own create
        # phase does) so terminal_id is REAL and ready by the time we report
        # it -- a terminal_id from a deferred (still-initializing) create would
        # race run_agent_step's reuse_terminal_id branch, which skips its
        # readiness wait entirely on the assumption the reused terminal is
        # already settled. _create_terminal resolves the same session/
        # caller_id/allowed_tools inheritance _resolve_handoff_provider already
        # computed into ``ctx`` (it does so independently via its own metadata
        # GET); reassigning ``provider`` to its return value uses whatever it
        # actually persisted on the terminal as the source of truth for the
        # reuse call below, rather than assuming the two resolutions agree.
        terminal_id, provider = _create_terminal(
            agent_profile,
            working_directory,
            engine=engine,
            model=model,
            use_worktree=use_worktree,
            create_timeout=_HANDOFF_CREATE_TIMEOUT_S,
            idempotency_key=idempotency_key,
        )
        if on_terminal_id is not None:
            try:
                on_terminal_id(terminal_id)
            except Exception as exc:  # noqa: BLE001 -- a UI callback must never break the handoff
                logger.warning(
                    "handoff: on_terminal_id callback failed for terminal %s: %s", terminal_id, exc
                )

        if not wait:
            # --no-wait: send the prompt and return immediately. The terminal
            # is left running (no teardown) -- the operator owns its lifecycle
            # from here via status/result/cancel, mirroring assign's contract.
            _send_direct_input_handoff(terminal_id, provider, message)
            return HandoffResult(
                success=True,
                message=(
                    f"Handed off to {agent_profile} ({provider}); not waiting for completion "
                    f"(--no-wait). Check on it with `cao agent status {terminal_id}`, read its "
                    f"result with `cao agent result {terminal_id}`, or free it with "
                    f"`cao agent cancel --delete {terminal_id}`."
                ),
                output=None,
                terminal_id=terminal_id,
            )

        # Waiting, but the terminal already exists (on_terminal_id was given):
        # drive it to completion via reuse_terminal_id instead of a fresh
        # create. working_directory/model/use_worktree/session_name/caller_id/
        # allowed_tools are all "ignored when reusing" per run_agent_step's own
        # contract (already applied at create time above); engine is NOT
        # ignored -- it is validated against what got persisted, so it is
        # still forwarded here to match.
        shaped_message = _shape_handoff_message(provider, message)
        payload = {
            "provider": provider,
            "agent": agent_profile,
            "prompt": shaped_message,
            "reuse_terminal_id": terminal_id,
            # False is the honest value here, not just the safe one:
            # run_agent_step's own teardown call is gated on
            # `teardown and created_here`, and created_here is False for ANY
            # reuse_terminal_id call -- the server literally cannot act on
            # this field once we're reusing, regardless of what we send.
            # Tearing down is therefore this function's own job on success,
            # below (review: socrates on commit 3952889 -- the prior version
            # sent True here and never tore anything down, leaking a
            # terminal on every successful wait=True handoff).
            "teardown": False,
            "timeout": float(timeout),
        }
        if engine is not None:
            payload["engine"] = engine
        result = await _run_step_and_build_result(
            payload, agent_profile, provider, timeout, start_time
        )
        if result.success:
            # Best-effort, mirroring run_agent_step's own teardown philosophy
            # (services/agent_step.py: "never let cleanup mask" a settled
            # step): a cleanup failure must not turn this already-successful
            # handoff into a reported failure. Only on success -- a failed,
            # errored, or timed-out wait leaves the terminal alive on purpose,
            # so the operator can inspect/recover it via status/result/cancel,
            # which is the entire point of surfacing terminal_id early.
            cleanup = _delete_terminal_impl(terminal_id)
            if not cleanup.get("success"):
                logger.warning(
                    "handoff: post-success teardown of terminal %s failed: %s",
                    terminal_id,
                    cleanup.get("message"),
                )
        return result

    except Exception as e:
        # Surface terminal_id when known. With the single-call design the server
        # owns the terminal lifecycle, so on a client-side failure (e.g. the
        # provider resolution) there is usually no terminal to surface.
        return HandoffResult(
            success=False, message=f"Handoff failed: {str(e)}", output=None, terminal_id=terminal_id
        )


# Implementation function for assign
def _assign_impl(
    agent_profile: str,
    message: str,
    working_directory: Optional[str] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    use_worktree: bool = False,
) -> Dict[str, Any]:
    """Implementation of assign logic.

    Uses the server-side deferred-init path: cao-server creates the tmux
    window and DB record synchronously (fast, <2s), then runs
    ``provider.initialize()`` and delivers the initial message as a
    background task. This keeps the assign() call's round-trip well
    under kiro-cli 2.11's ~60s per-tool client timeout, and lets multiple
    concurrent assigns from the same LLM turn run their init phases in
    parallel instead of blocking one behind the other.
    """
    terminal_id: Optional[str] = None
    try:
        # Fail fast before creating the worker terminal when CAO_TERMINAL_ID is
        # unset — REGARDLESS of the sender-ID-injection flag. The deferred-init
        # path only forwards the initial message on the existing-session branch
        # of _create_terminal (an existing session requires a current terminal).
        # Without CAO_TERMINAL_ID, _create_terminal takes the new-session branch
        # which cannot honor defer_init/initial_message — assign would create a
        # worker, never deliver the task, and still return success. Guarding
        # here also avoids leaving an orphan window behind (issue #284).
        current_terminal_id = _current_terminal_id()
        if not current_terminal_id:
            return {
                "success": False,
                "terminal_id": None,
                "message": (
                    "Assignment failed: CAO_TERMINAL_ID not set — assign must run "
                    "from inside a CAO terminal so the worker joins the caller's "
                    "session and its results can route back."
                ),
            }

        # Compose the message the worker will see once it is ready. We do
        # this here (not on the server) because the callback-instructions
        # suffix depends on ``CAO_TERMINAL_ID``, which lives in the calling
        # process's env (the supervisor-owned MCP subprocess, or a ``cao
        # agent assign`` shell), not on the cao-server side.
        if ENABLE_SENDER_ID_INJECTION:
            worker_message = (
                message
                + f"\n\n[Assigned by terminal {current_terminal_id}. "
                + f"When done, send results back to terminal {current_terminal_id} using send_message]"
            )
        else:
            worker_message = message

        # Create terminal in DEFERRED-INIT mode: cao-server returns as soon
        # as the tmux window is up and the DB row is written; the actual
        # provider.initialize() and initial-message delivery run as a
        # background task on the server. The call typically returns
        # in under 2 seconds regardless of how long init takes.
        terminal_id, _ = _create_terminal(
            agent_profile,
            working_directory,
            engine=engine,
            defer_init=True,
            initial_message=worker_message,
            initial_message_orchestration_type=OrchestrationType.ASSIGN,
            model=model,
            use_worktree=use_worktree,
        )

        return {
            "success": True,
            "terminal_id": terminal_id,
            "message": (
                f"Task assigned to {agent_profile} (terminal: {terminal_id}). "
                f"Worker is initializing in the background; your task will be "
                f"delivered once it is ready. "
                f"Call delete_terminal('{terminal_id}') when you no longer need this terminal."
                + _get_cleanup_nudge()
            ),
        }

    except Exception as e:
        # Surface the terminal_id when creation succeeded before the failure
        # (e.g. the send POST failed) so the orphaned terminal can be
        # inspected or deleted — matching the ready-timeout path above.
        return {
            "success": False,
            "terminal_id": terminal_id,
            "message": f"Assignment failed: {str(e)}",
        }


# Implementation function for send_message
def _send_message_impl(receiver_id: Optional[str], message: str) -> Dict[str, Any]:
    """Implementation of send_message logic."""
    try:
        own_terminal_id = _current_terminal_id()

        # Default the receiver to the recorded caller (issue #284): handoff/
        # assign persist the creating terminal's ID on the worker's row, so a
        # worker can reply without parsing an ID out of the task message text.
        if not receiver_id:
            if not own_terminal_id:
                return {
                    "success": False,
                    "error": (
                        "receiver_id not provided and CAO_TERMINAL_ID not set - cannot "
                        "look up the recorded caller. Pass receiver_id explicitly."
                    ),
                }
            response = requests.get(
                f"{API_BASE_URL}/terminals/{own_terminal_id}",
                headers=_auth_headers() or None,
                timeout=_mcp_timeout(),
            )
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                detail = _extract_error_detail(response, str(exc))
                return {
                    "success": False,
                    "error": (
                        f"receiver_id not provided and the caller lookup for this "
                        f"terminal ({own_terminal_id}) failed: {detail}. Pass "
                        "receiver_id explicitly."
                    ),
                }
            receiver_id = response.json().get("caller_id")
            if not receiver_id:
                return {
                    "success": False,
                    "error": (
                        "receiver_id not provided and this terminal has no recorded "
                        "caller (it was not created via handoff/assign). Pass "
                        "receiver_id explicitly."
                    ),
                }

        # Guard against the worker sending a message to itself (issue #24).
        # Worker agents sometimes confuse their own CAO_TERMINAL_ID with the
        # supervisor's and end up queueing a message into their own inbox,
        # which never reaches the supervisor. Reject that here so the worker
        # gets a clear error and can pick the correct receiver_id instead.
        if own_terminal_id and receiver_id == own_terminal_id:
            return {
                "success": False,
                "error": (
                    f"receiver_id ({receiver_id}) is this terminal's own CAO_TERMINAL_ID. "
                    "send_message cannot deliver to the sender. Omit receiver_id to reply "
                    "to the terminal that assigned this task (the recorded caller), or "
                    "use the supervisor's terminal ID from the task message."
                ),
            }

        # Auto-inject sender terminal ID suffix when enabled. Skipped when
        # CAO_TERMINAL_ID is unset — never inject 'unknown' as a routable
        # address (issue #284); _send_to_inbox raises a clear error for that
        # case anyway.
        if ENABLE_SENDER_ID_INJECTION and own_terminal_id:
            message += (
                f"\n\n[Message from terminal {own_terminal_id}. "
                "Use send_message MCP tool for any follow-up work.]"
            )

        return _send_to_inbox(receiver_id, message)
    except requests.HTTPError as exc:
        # e.g. the receiver terminal (a recorded caller included) was deleted
        # before this reply — surface the API detail instead of a raw
        # requests error string so the agent knows the address is gone.
        detail = str(exc)
        if exc.response is not None:
            detail = _extract_error_detail(exc.response, detail)
        return {
            "success": False,
            "error": f"Failed to deliver to terminal {receiver_id}: {detail}",
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _delete_terminal_impl(terminal_id: str) -> Dict[str, Any]:
    """Implementation of delete_terminal logic.

    Kills the tmux window and removes the terminal record. Used both by the
    ``delete_terminal`` MCP tool and by ``cao agent cancel --delete``.
    """
    try:
        response = requests.delete(
            f"{API_BASE_URL}/terminals/{terminal_id}",
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        if response.status_code == 409:
            return {
                "success": False,
                "message": (
                    f"Terminal {terminal_id} cleanup is pending; retry delete_terminal "
                    "after the Grok process exits."
                ),
            }
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", False):
            return {
                "success": False,
                "message": (
                    f"Terminal {terminal_id} cleanup is pending; retry delete_terminal "
                    "after the Grok process exits."
                ),
            }
        return {"success": True, "message": f"Terminal {terminal_id} deleted successfully"}
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return {"success": False, "message": f"Terminal {terminal_id} not found"}
        if e.response is not None and e.response.status_code == 409:
            return {
                "success": False,
                "message": (
                    f"Terminal {terminal_id} cleanup is pending; retry delete_terminal "
                    "after the Grok process exits."
                ),
            }
        return {"success": False, "message": f"Failed to delete terminal: {str(e)}"}
    except Exception as e:
        return {"success": False, "message": f"Failed to delete terminal: {str(e)}"}


def _status_impl(terminal_id: str) -> Dict[str, Any]:
    """Fetch a terminal's current status and identifying metadata.

    Backs ``cao agent status`` (issue #616) -- no MCP tool exposes this today
    (an LLM caller of assign/handoff already gets a terminal_id back and
    learns completion via handoff's own return, or via send_message from the
    worker); the CLI path needs an explicit poll-style check since there is
    no one to call it back.
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/terminals/{terminal_id}",
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        if response.status_code == 404:
            return {
                "success": False,
                "terminal_id": terminal_id,
                "error": f"Terminal {terminal_id} not found",
            }
        response.raise_for_status()
        terminal = response.json()
        return {
            "success": True,
            "terminal_id": terminal.get("id", terminal_id),
            "status": terminal.get("status"),
            "agent_profile": terminal.get("agent_profile"),
            "provider": terminal.get("provider"),
            "session_name": terminal.get("session_name"),
        }
    except requests.HTTPError as exc:
        detail = (
            _extract_error_detail(exc.response, str(exc)) if exc.response is not None else str(exc)
        )
        return {"success": False, "terminal_id": terminal_id, "error": detail}
    except requests.ConnectionError:
        return {
            "success": False,
            "terminal_id": terminal_id,
            "error": "Failed to connect to cao-server. The server may not be running.",
        }
    except Exception as e:
        return {"success": False, "terminal_id": terminal_id, "error": str(e)}


def _result_impl(terminal_id: str) -> Dict[str, Any]:
    """Fetch a terminal's last response (the tail of its most recent turn).

    Backs ``cao agent result`` (issue #616): the CLI counterpart of what a
    supervisor would otherwise learn from a worker's own send_message
    callback -- for when that callback never arrives (MCP down on the worker
    side too, or the worker was created via assign and hasn't been told to
    call back yet).
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/terminals/{terminal_id}/output",
            params={"mode": "last"},
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        if response.status_code == 404:
            return {
                "success": False,
                "terminal_id": terminal_id,
                "error": f"Terminal {terminal_id} not found",
            }
        response.raise_for_status()
        return {
            "success": True,
            "terminal_id": terminal_id,
            "output": response.json().get("output"),
        }
    except requests.HTTPError as exc:
        detail = (
            _extract_error_detail(exc.response, str(exc)) if exc.response is not None else str(exc)
        )
        return {"success": False, "terminal_id": terminal_id, "error": detail}
    except requests.ConnectionError:
        return {
            "success": False,
            "terminal_id": terminal_id,
            "error": "Failed to connect to cao-server. The server may not be running.",
        }
    except Exception as e:
        return {"success": False, "terminal_id": terminal_id, "error": str(e)}


def _cancel_impl(terminal_id: str, delete: bool = False) -> Dict[str, Any]:
    """Stop a worker terminal: interrupt its current turn, or free it entirely.

    Backs ``cao agent cancel`` (issue #616). Default (``delete=False``) sends
    a tmux interrupt (C-c) -- cooperative, matching this codebase's other
    "cancel" verb (``cao workflow cancel``): the terminal survives so it can
    be reassigned. ``delete=True`` instead frees the terminal via the same
    path as the ``delete_terminal`` MCP tool -- for "I'm done with this
    worker", the cleanup verb assign's own success message already points
    callers at.
    """
    if delete:
        return _delete_terminal_impl(terminal_id)

    try:
        response = requests.post(
            f"{API_BASE_URL}/terminals/{terminal_id}/key",
            params={"key": "C-c"},
            headers=_auth_headers() or None,
            timeout=_mcp_timeout(),
        )
        if response.status_code == 404:
            return {
                "success": False,
                "terminal_id": terminal_id,
                "error": f"Terminal {terminal_id} not found",
            }
        response.raise_for_status()
        return {
            "success": True,
            "terminal_id": terminal_id,
            "message": f"Sent interrupt (C-c) to terminal {terminal_id}",
        }
    except requests.HTTPError as exc:
        detail = (
            _extract_error_detail(exc.response, str(exc)) if exc.response is not None else str(exc)
        )
        return {"success": False, "terminal_id": terminal_id, "error": detail}
    except requests.ConnectionError:
        return {
            "success": False,
            "terminal_id": terminal_id,
            "error": "Failed to connect to cao-server. The server may not be running.",
        }
    except Exception as e:
        return {"success": False, "terminal_id": terminal_id, "error": str(e)}
