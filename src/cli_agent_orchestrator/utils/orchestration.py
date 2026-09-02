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
exclusively through cao-server's FastAPI surface over ``API_BASE_URL`` -- or,
for a cross-node placement, over the target node's own base URL resolved by
``_resolve_target_base_url`` (``requests``) -- never through ``clients.tmux`` /
``clients.database``.
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

from cli_agent_orchestrator.constants import (
    ADVERTISED_URL_ENV,
    API_BASE_URL,
    CALLBACK_TERMINAL_ID_ENV,
    CALLBACK_URL_ENV,
    DEFAULT_PROVIDER,
)
from cli_agent_orchestrator.mcp_server.models import HandoffResult
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.security.auth import get_local_bearer
from cli_agent_orchestrator.services.elastic_worker_gateway import (
    elastic_worker_gateway_headers,
)
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import resolve_provider
from cli_agent_orchestrator.utils.terminal import (
    generate_session_name,
    generate_session_name_for_key,
)

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


# --- Cross-node placement + callback routing (one-agent-per-pod topology) ---
# A supervisor may delegate to a REMOTE CAO node by passing ``target_host`` to
# assign/handoff. The worker terminal is then created on that node via its REST
# API instead of the caller's local cao-server. For replies to route back
# cross-node, two env vars are involved:
#
#   CAO_ADVERTISED_URL        set on the SUPERVISOR's node: the base URL at
#                             which peers (worker pods) can reach THIS node's
#                             cao-server (e.g. http://cao-supervisor:9889).
#                             Required for remote assign — without it the
#                             remote worker would have no reachable address to
#                             send results back to.
#   CAO_ELASTIC_CALLBACK_URL  optional narrow broker gateway used only by
#                             assign_elastic workers, so they never receive the
#                             supervisor control API URL.
#   CAO_CALLBACK_URL /        injected by the supervisor into the REMOTE worker
#   CAO_CALLBACK_TERMINAL_ID  terminal's environment at creation time: the
#                             supervisor cao-server's advertised URL and the
#                             supervisor's terminal ID. send_message on the
#                             worker uses them to deliver replies to the
#                             supervisor's node (its own local DB has no row
#                             for the supervisor's terminal).
#
# All three unset = single-node behavior, byte-for-byte unchanged. The env-var
# NAMES live in constants.py (imported above) because terminal_service also
# reads them server-side to notify a cross-node supervisor of deferred-init
# failures.

# Default port assumed for a bare ``target_host`` DNS name (every CAO node in
# the k8s manifests listens on 9889; override by passing host:port or a URL).
DEFAULT_TARGET_PORT = 9889

# Connect-leg timeout (seconds) for HTTP calls to a REMOTE node. Remote calls
# use a (connect, read) tuple so a black-holed/unreachable node fails in
# seconds instead of consuming the full read budget (which for handoff is
# timeout+180s).
REMOTE_CONNECT_TIMEOUT = 10.0


def _callback_route() -> Tuple[Optional[str], Optional[str]]:
    """Return ``(callback_base_url, callback_terminal_id)`` for a remote worker.

    Both come from the env vars the supervisor injected at remote-creation time
    (see the CALLBACK_* constants above). ``(None, None)``-ish values mean this
    terminal was created locally — callers must leave behavior unchanged then.
    """
    url = os.environ.get(CALLBACK_URL_ENV)
    terminal_id = os.environ.get(CALLBACK_TERMINAL_ID_ENV)
    return (url.rstrip("/") if url else None, terminal_id or None)


def _resolve_target_base_url(target_host: str) -> str:
    """Normalize a ``target_host`` value into a cao-server base URL.

    Accepts a full URL (``http://host:port``), a ``host:port`` pair, or a bare
    DNS name / hostname (port defaults to DEFAULT_TARGET_PORT, the port every
    CAO node in the k8s manifests listens on).

    Note: a BARE IPv6 literal (e.g. ``::1`` or ``fd00::2``) contains ``:`` and
    would be misparsed by the host:port branch below — pass IPv6 targets as a
    full bracketed URL instead (``http://[fd00::2]:9889``), which the ``://``
    branch handles verbatim.
    """
    host = target_host.strip()
    if not host:
        raise ValueError("target_host must not be empty")
    if "://" in host:
        return host.rstrip("/")
    if ":" in host:
        return f"http://{host}"
    return f"http://{host}:{DEFAULT_TARGET_PORT}"


def _wait_remote_ready(base_url: str, timeout: float) -> None:
    """Poll a remote node's ``/health`` until it answers, or raise.

    Exists because "the pod is Ready" and "the Service in front of the pod is
    routable" are different claims, and only the second one is what a caller
    needs. A broker that leases a worker the moment its Job and Service objects
    exist is handing back an address that becomes usable shortly afterwards -
    endpoint published, kube-proxy rules programmed on this node - and the
    difference is a second or two that no readiness probe on the pod can observe.
    Waiting here, on the address actually about to be used, is the only check
    that covers both.

    Polls rather than retrying the real request, and polls a GET, because that is
    what makes this safe: ``POST /sessions`` is not idempotent, so retrying it
    through a connection error risks two terminals on a node that allows one.
    ``GET /health`` can be retried as often as we like.

    A short per-attempt connect timeout on purpose: the expected failure while a
    Service converges is a fast refusal or a DNS miss, and spending 10s on each
    would turn a 2s wait into one attempt.

    Raises:
        ValueError: the node did not answer within ``timeout``.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    last_error = "no attempt made"
    while True:
        attempt += 1
        try:
            response = requests.get(f"{base_url}/health", timeout=(2.0, 5.0))
            if response.status_code < 400:
                if attempt > 1:
                    logger.info("Remote node %s answered /health on attempt %d", base_url, attempt)
                return
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as exc:
            last_error = str(exc)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(
                f"remote CAO node at {base_url} did not become reachable within "
                f"{timeout:.0f}s ({attempt} attempts, last error: {last_error}); "
                f"check the pod's status, CoreDNS, and any NetworkPolicy on "
                f"worker ingress"
            )
        time.sleep(min(0.5, remaining))


def _resolve_remote_provider(base_url: str, agent_profile: str) -> str:
    """Resolve a worker's provider from the REMOTE node's own profile store.

    Mirrors ``utils.agent_profiles.resolve_provider`` but over HTTP: profiles
    are installed per node, so the caller's local store is the wrong place to
    look for a profile that will run remotely (the supervisor node typically
    only installs supervisor profiles). Falls back to DEFAULT_PROVIDER only when
    the remote profile is missing (404) or a successful response does not pin a
    provider — the remote node's provider init will surface a clear error if that
    guess is wrong. Other HTTP failures are raised rather than silently changing
    providers.

    A CONNECTION-level failure raises instead of falling back: it doubles as
    the reachability probe for the whole remote call, and guessing a provider
    only to post the real work to the same dead node would waste the caller's
    full timeout budget on a node we already know is unreachable.

    Raises:
        ValueError: the remote node could not be reached at all, or returned an
            unexpected non-error status.
        requests.HTTPError: the profile lookup returned an HTTP error other than
            404.
    """
    try:
        response = requests.get(
            f"{base_url}/agents/profiles/{agent_profile}",
            timeout=(REMOTE_CONNECT_TIMEOUT, _mcp_timeout()),
        )
    except requests.RequestException as exc:
        raise ValueError(
            f"cannot reach remote CAO node at {base_url} ({exc}); check "
            f"target_host and that the node's cao-server is up"
        )
    if response.status_code == 404:
        return DEFAULT_PROVIDER
    if response.status_code != 200:
        response.raise_for_status()
        raise ValueError(
            f"remote profile lookup at {base_url} returned unexpected "
            f"HTTP {response.status_code}"
        )
    provider = response.json().get("provider")
    if provider:
        return str(provider)
    return DEFAULT_PROVIDER


def _cleanup_remote_terminal(base_url: str, terminal_id: str) -> bool:
    """Best-effort DELETE of a terminal on a REMOTE node.

    Used when a remote handoff step fails/times out: ``run_agent_step`` only
    tears the worker terminal down on SUCCESS, and on a CAO_MAX_TERMINALS=1
    worker pod a leftover terminal occupies the pod's only slot — permanently,
    since the supervisor's local delete cannot reach it. A 404 counts as
    cleaned (already gone). Never raises; returns False so the caller can put
    the manual cleanup route in its failure message.
    """
    try:
        response = requests.delete(
            f"{base_url}/terminals/{terminal_id}",
            timeout=(REMOTE_CONNECT_TIMEOUT, _mcp_timeout()),
        )
        if response.status_code == 404:
            return True
        return response.status_code < 400
    except requests.RequestException as exc:
        logger.warning("Cleanup of remote terminal %s at %s failed: %s", terminal_id, base_url, exc)
        return False


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
        # A KEYED request derives its session name FROM the key instead of a
        # fresh uuid4, and without this a keyed retry on this branch could never
        # match (review on PR #634, issue #616). The server fingerprints
        # session_name, and this branch mints a new one per invocation, so two
        # keyed retries from outside a CAO terminal produced different
        # fingerprints for the same logical request: the retry 409'd against its
        # own first attempt instead of reattaching to it. Deriving the name makes
        # the request byte-identical across attempts, which is the precondition
        # the fingerprint check has always assumed.
        #
        # sha256 of the key, truncated to the same 8 hex chars
        # `generate_session_name` uses, so the result is indistinguishable in
        # shape from a generated name and satisfies the same tmux-name rules.
        # Two DIFFERENT requests sharing a key collide on this name, which is
        # correct: that is the conflict case, and it surfaces as the 409 the key
        # is supposed to produce rather than as two sessions.
        session_name = (
            generate_session_name_for_key(idempotency_key)
            if idempotency_key
            else generate_session_name()
        )
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

    Cross-node routing (one-agent-per-pod topology): when this terminal was
    created remotely, the supervisor's terminal lives on ANOTHER node — its row
    does not exist in this node's DB, so a local POST would 404. If the
    receiver is the recorded cross-node supervisor (CAO_CALLBACK_TERMINAL_ID),
    deliver through CAO_CALLBACK_URL. For ordinary remote workers that is the
    supervisor's cao-server; elastic workers use the authenticated broker
    gateway. A local 404 for any other receiver is also retried against the
    callback URL once, so an explicitly quoted supervisor ID still routes.
    Single-node behavior (no callback env) is unchanged.

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

    callback_url, callback_terminal_id = _callback_route()
    base_url = API_BASE_URL
    if callback_url and receiver_id == callback_terminal_id:
        base_url = callback_url

    params = {"sender_id": sender_id, "message": message}
    # BOTH header sets, and the union is not a compromise between two merge
    # sides -- they are disjoint and independently load-bearing.
    # `_auth_headers()` carries the local `Authorization: Bearer` an
    # auth-enabled cao-server rejects every call without (haofeif's P2 on PR
    # #634); `elastic_worker_gateway_headers()` carries the broker's
    # worker-id/release-token pair an elastic worker's callback hop needs. They
    # share no key, so neither can shadow the other, and an auth-enabled
    # elastic deployment genuinely needs both on the same request. Each is
    # empty when its own feature is off, so the default-off posture is still
    # byte-for-byte `None`.
    request_headers = {**_auth_headers(), **elastic_worker_gateway_headers()} or None
    response = requests.post(
        f"{base_url}/terminals/{receiver_id}/inbox/messages",
        params=params,
        headers=request_headers,
        timeout=_mcp_timeout(),
    )
    if response.status_code == 404 and callback_url and base_url != callback_url:
        # Receiver unknown on this node but a cross-node supervisor is
        # recorded — the caller likely quoted a terminal ID that lives on the
        # supervisor's node. One retry against that node before failing.
        response = requests.post(
            f"{callback_url}/terminals/{receiver_id}/inbox/messages",
            params=params,
            headers=request_headers,
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
    base_url: str = API_BASE_URL,
    target_host: Optional[str] = None,
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

    ``base_url``/``target_host`` carry ``_handoff_impl``'s cross-node placement
    down to the one POST that this helper owns. They default to the local node,
    so the two callers that never place remotely pass neither and behave exactly
    as before. ``target_host`` is needed here in ADDITION to ``base_url``
    because it is not merely a URL: it selects the connect-timeout shape and it
    is the name quoted back to the operator in the remote-cleanup hints below.
    """
    known_terminal_id: Optional[str] = payload.get("reuse_terminal_id")
    # Allow the full step time plus the server-side ready-wait (up to 120s)
    # plus headroom; the server enforces the per-step timeout internally.
    #
    # Remote calls use a (connect, read) tuple: without it, a black-holed
    # node would consume the FULL read budget (~timeout+180s) just failing
    # to connect. Local calls keep the plain timeout (localhost connect
    # cannot black-hole meaningfully) so their behavior is unchanged.
    client_timeout = float(timeout) + 180.0
    request_timeout: Any = (
        (REMOTE_CONNECT_TIMEOUT, client_timeout) if target_host else client_timeout
    )
    try:
        response = requests.post(
            f"{base_url}/terminals/run-step",
            json=payload,
            headers=_auth_headers() or None,
            timeout=request_timeout,
        )
    except requests.Timeout:
        timeout_msg = f"Handoff timed out after {timeout} seconds"
        if target_host and not known_terminal_id:
            # Client-side timeout on a fresh remote create: the step may still
            # be running and its terminal id is unknown here, so it cannot be
            # auto-cleaned. On a CAO_MAX_TERMINALS=1 worker that terminal
            # occupies the pod's only slot — hand the operator the manual route.
            timeout_msg += (
                f". A worker terminal may remain on {target_host}; inspect "
                f"GET {base_url}/sessions and free the slot with "
                f"delete_terminal(<id>, target_host='{target_host}')."
            )
        return HandoffResult(
            success=False,
            message=timeout_msg,
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
        resolved_tid = tid or known_terminal_id
        if target_host and resolved_tid:
            # A failed/timed-out step leaves its terminal ALIVE server-side
            # (run_agent_step only tears down on success). Locally the
            # supervisor can delete_terminal it; remotely that terminal
            # occupies a max=1 worker pod's ONLY slot and the local delete
            # cannot reach it — so clean it up here, best-effort.
            if _cleanup_remote_terminal(base_url, resolved_tid):
                msg += f" (remote terminal {resolved_tid} on {target_host} cleaned up)"
            else:
                msg += (
                    f". Cleanup of remote terminal {resolved_tid} failed — it still "
                    f"occupies a slot on {target_host}; terminal {resolved_tid} lives "
                    f"on {target_host}; DELETE {base_url}/terminals/{resolved_tid} "
                    f"(or delete_terminal('{resolved_tid}', target_host='{target_host}'))."
                )
        return HandoffResult(success=False, message=msg, output=None, terminal_id=resolved_tid)

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
    placement = f" on node {target_host}" if target_host else ""
    return HandoffResult(
        success=True,
        message=f"Successfully handed off to {agent_profile} ({provider})"
        f"{placement} in {execution_time:.2f}s" + _get_cleanup_nudge(),
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
    target_host: Optional[str] = None,
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

    ``idempotency_key``: NO CALLER PASSES THIS TODAY, deliberately. It is
    forwarded to ``_create_terminal``'s early-terminal-id create call, which
    forwards it to the server; the server persists
    ``(idempotency_key -> terminal_id)`` atomically with the terminal row (see
    ``terminal_service.create_terminal``'s docstring), so a caller supplying the
    SAME key on a retry gets back the terminal the first, already-committed
    attempt created rather than a second worker.

    That deduplicates terminal CREATION only. It does NOT deduplicate the
    submission: the message is delivered after the worker exists, and nothing
    records whether that delivery happened, so a retry can neither safely skip
    the send (which would silently drop the task) nor safely repeat it (which
    would run it twice). A ``cao agent handoff --idempotency-key`` flag briefly
    existed here and was REMOVED for exactly that reason (review on PR #634):
    a user-facing retry-safety contract that only half holds is worse than none,
    and #616's "killing the CLI process does not lose the job or its result"
    needs the durable run record, not this key alone.

    The parameter and its tests stay because #715 builds the run record on top
    of this substrate and will re-expose the key once a retry can resolve to an
    existing RUN. Unreachable, not dead -- do not delete it as unused.

    ``target_host`` (one-agent-per-pod topology): when set, the single
    run-step call goes to THAT node's cao-server instead of the local one, so
    the worker terminal (and its fresh session) is created on the remote node.
    The provider is resolved from the remote node's own profile store —
    failing FAST if the node is unreachable (never guess a provider and then
    post work to a dead node) — no ``session_name``/``caller_id`` is sent
    (the supervisor's session and terminal row exist only on the supervisor's
    node, and handoff is blocking — the result returns in this HTTP response,
    no callback needed), and ``working_directory`` is interpreted on the
    remote filesystem. ``use_worktree`` is rejected together with
    ``target_host`` (same rule as assign — see the target_host field
    description). A failed/timed-out remote step leaves its terminal alive
    server-side, which on a CAO_MAX_TERMINALS=1 worker pod occupies the pod's
    only slot — so remote failures trigger a best-effort DELETE of that
    terminal here, and the failure message carries the manual cleanup route
    when even that fails. Omitting ``target_host`` preserves local behavior
    byte-for-byte.

    ``target_host`` is ALSO rejected together with the three CLI-only inputs
    above (``on_terminal_id``, ``wait=False``, ``idempotency_key``), and that
    combination is refused rather than made to work because the two features
    were built against different code paths and silently mixing them targets
    the WRONG NODE. Those inputs route through ``_create_terminal`` and
    ``_send_direct_input_handoff``, both of which post to ``API_BASE_URL``
    unconditionally: a remote handoff carrying them would create the worker
    locally and then drive it remotely (or the reverse), which no caller could
    detect from a success return. Rejecting is the same call upstream already
    made for ``use_worktree`` for the same reason. Separately, the idempotency
    mapping is per-node local state (``idempotency_keys`` is a node-local
    table), so a keyed retry that lands on a different ``target_host`` could
    not dedupe even if the plumbing were threaded.
    """
    start_time = time.time()
    terminal_id: Optional[str] = None

    # Same rule as assign (kept symmetric on purpose): remote worktree
    # provisioning is not supported — the remote pod's default workspace is
    # not a git checkout, so use_worktree would only fail later and wedge the
    # worker's slot. Reject the combination up front.
    if target_host and use_worktree:
        return HandoffResult(
            success=False,
            message=(
                "Handoff failed: use_worktree is not supported together with "
                "target_host (remote nodes have no shared git checkout to "
                "provision a worktree from). Omit one of the two."
            ),
            output=None,
            terminal_id=None,
        )

    # See the docstring: these three drive the pre-create path, which is
    # API_BASE_URL-only. Refuse rather than half-honor the placement.
    #
    # `idempotency_key` is included even though no CLI flag reaches it today
    # (that flag was removed -- see the parameter's own docstring note): the
    # parameter is still live for #715 to re-expose, and the guard has to be
    # correct when it is, not retrofitted then.
    if target_host and (on_terminal_id is not None or not wait or idempotency_key):
        return HandoffResult(
            success=False,
            message=(
                "Handoff failed: target_host is not supported together with "
                "--no-wait or a request idempotency key (those paths create and "
                "drive the worker through this node's own API, so a remote "
                "placement would silently target the wrong node; the idempotency "
                "mapping is node-local too). Omit target_host, or drop the other "
                "option and use the default blocking handoff."
            ),
            output=None,
            terminal_id=None,
        )

    try:
        if target_host:
            # Remote placement: the worker runs on target_host's node in a
            # fresh session there. The supervisor's session/caller_id/allowed
            # -tools context is local-node state and is deliberately NOT sent
            # (the remote DB has no row for the supervisor's terminal); the
            # provider comes from the remote node's own profile store.
            base_url = _resolve_target_base_url(target_host)
            ctx = HandoffContext(
                provider=_resolve_remote_provider(base_url, agent_profile),
                session_name=None,
                caller_id=None,
                allowed_tools=None,
            )
        else:
            # Resolve the supervisor context WITHOUT creating a terminal, so the
            # codex fast-fail (which needs CAO_TERMINAL_ID) can run before any
            # terminal exists, on every path below.
            base_url = API_BASE_URL
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
                payload,
                agent_profile,
                provider,
                timeout,
                start_time,
                base_url=base_url,
                target_host=target_host,
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
        # still forwarded here to match. Forwarded under the same Kiro-only
        # guard as the create path below, for exactly that reason: engine is a
        # Kiro-only concept and the create path only ever persists it for
        # kiro_cli, so sending it for any other provider could only ever
        # mismatch what is stored.
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
            # below (review on commit 3952889 -- the prior version
            # sent True here and never tore anything down, leaking a
            # terminal on every successful wait=True handoff).
            "teardown": False,
            "timeout": float(timeout),
        }
        if provider == ProviderType.KIRO_CLI.value and engine is not None:
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


def _assign_remote(
    *,
    agent_profile: str,
    worker_message: str,
    current_terminal_id: str,
    target_host: str,
    working_directory: Optional[str],
    engine: Optional[str],
    model: Optional[str],
    use_worktree: bool,
    ready_wait_seconds: float = 0.0,
    callback_url: Optional[str] = None,
    remote_session_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create an assign worker on a REMOTE CAO node (one-agent-per-pod topology).

    Uses the remote node's ``POST /sessions`` deferred-init path: a fresh
    session is created there (the supervisor's session exists only on this
    node) and the task is delivered once the remote provider initializes. The
    remote node resolves the provider from its OWN installed profile store
    (``provider`` is deliberately omitted from the request).

    Callback routing: assign is non-blocking, so results come back via the
    worker's ``send_message``. The worker's node has no DB row for this
    supervisor terminal, so we inject ``CAO_CALLBACK_URL`` and
    ``CAO_CALLBACK_TERMINAL_ID`` into the remote terminal's environment. Plain
    remote assign uses this node's ``CAO_ADVERTISED_URL``; elastic assign passes
    its authenticated broker gateway as ``callback_url`` so workers never learn
    the supervisor control API URL.

    Elastic assign also passes the broker-issued ``remote_session_name``. The
    broker binds memory authorization to that immutable session identity before
    the worker exists, avoiding a registration race with deferred initialization.

    ``ready_wait_seconds`` > 0 waits for the target's ``/health`` before posting
    (see ``_wait_remote_ready``). It defaults to 0, which keeps every existing
    caller byte-identical: a ``target_host`` naming a long-running pod is either
    up or genuinely broken, and a node that is down should still fail in seconds
    rather than after a wait. Only a caller that just CREATED the target - the
    elastic path - has reason to expect it to arrive shortly.
    """
    advertised_url = callback_url or os.environ.get(ADVERTISED_URL_ENV)
    if not advertised_url:
        return {
            "success": False,
            "terminal_id": None,
            "message": (
                f"Assignment failed: target_host={target_host!r} requires "
                f"{ADVERTISED_URL_ENV} to be set on this node (the base URL at "
                f"which the remote worker can reach this cao-server, e.g. "
                f"http://cao-supervisor:9889) — without it the worker's results "
                f"cannot route back."
            ),
        }
    if use_worktree:
        return {
            "success": False,
            "terminal_id": None,
            "message": (
                "Assignment failed: use_worktree is not supported together with "
                "target_host (the remote session-creation API has no worktree "
                "provisioning). Omit one of the two."
            ),
        }

    base_url = _resolve_target_base_url(target_host)
    if ready_wait_seconds > 0:
        _wait_remote_ready(base_url, ready_wait_seconds)
    params: Dict[str, Any] = {"agent_profile": agent_profile}
    if remote_session_name:
        params["session_name"] = remote_session_name
    if working_directory:
        # Interpreted on the REMOTE node's filesystem; the supervisor's own
        # cwd is deliberately NOT inherited cross-node (it is meaningless
        # on another pod's filesystem).
        params["working_directory"] = working_directory
    if engine is not None:
        params["engine"] = engine
    if model is not None:
        params["model"] = model

    response = requests.post(
        f"{base_url}/sessions",
        params=params,
        json={
            "initial_message": worker_message,
            "initial_message_orchestration_type": OrchestrationType.ASSIGN.value,
            "env_vars": {
                CALLBACK_URL_ENV: advertised_url.rstrip("/"),
                CALLBACK_TERMINAL_ID_ENV: current_terminal_id,
            },
        },
        timeout=(REMOTE_CONNECT_TIMEOUT, _mcp_timeout()),
    )
    if response.status_code >= 400:
        # Surface the remote node's JSON detail (e.g. a 429 "Terminal limit
        # reached ... target a different node" from a full max=1 worker)
        # instead of a bare status line the supervisor cannot act on.
        detail = _extract_error_detail(response, f"status {response.status_code}")
        return {
            "success": False,
            "terminal_id": None,
            "target_host": target_host,
            "message": f"Assignment failed on node {target_host}: {detail}",
        }
    data = response.json()
    terminal_id = data["id"]
    session_name = data.get("session_name")
    if remote_session_name and session_name != remote_session_name:
        return {
            "success": False,
            "terminal_id": terminal_id,
            "target_host": target_host,
            "message": (
                f"Assignment failed on node {target_host}: requested bound session "
                f"{remote_session_name!r}, but remote node returned {session_name!r}"
            ),
        }
    # Ready-made cleanup route. NOTE: DELETE /sessions/{name} requires the
    # admin scope (SCOPE_ADMIN) when the node's OAuth layer is enabled;
    # DELETE /terminals/{id} (write scope) is the lighter alternative.
    delete_url = f"{base_url}/sessions/{session_name}" if session_name else None

    message_text = (
        f"Task assigned to {agent_profile} on node {target_host} "
        f"(remote terminal: {terminal_id}"
        + (f", session: {session_name}" if session_name else "")
        + f"). The worker is initializing in the background; your task will "
        f"be delivered once it is ready and results will arrive via "
        f"send_message. Cleanup when finished: "
        f"delete_terminal('{terminal_id}', target_host='{target_host}')"
        + (
            f", or drop the whole remote session with DELETE {delete_url} "
            f"(requires admin scope when auth is enabled)."
            if delete_url
            else "."
        )
    )
    result = {
        "success": True,
        "terminal_id": terminal_id,
        "target_host": target_host,
        "message": message_text,
    }
    if session_name:
        result["session_name"] = session_name
        result["delete_url"] = delete_url
    return result


# Implementation function for assign
def _assign_impl(
    agent_profile: str,
    message: str,
    working_directory: Optional[str] = None,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    use_worktree: bool = False,
    target_host: Optional[str] = None,
    ready_wait_seconds: float = 0.0,
    callback_url: Optional[str] = None,
    remote_session_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Implementation of assign logic.

    Uses the server-side deferred-init path: cao-server creates the tmux
    window and DB record synchronously (fast, <2s), then runs
    ``provider.initialize()`` and delivers the initial message as a
    background task. This keeps the assign() call's round-trip well
    under kiro-cli 2.11's ~60s per-tool client timeout, and lets multiple
    concurrent assigns from the same LLM turn run their init phases in
    parallel instead of blocking one behind the other.

    ``target_host`` (one-agent-per-pod topology): when set, the worker is
    created on THAT node via its ``POST /sessions`` deferred-init path (fresh
    session, provider resolved from the remote node's own profile store). For
    the worker's results to route back cross-node, this supervisor node must
    advertise a peer-reachable base URL in ``CAO_ADVERTISED_URL``; it is
    injected into the remote worker's env as ``CAO_CALLBACK_URL`` together
    with ``CAO_CALLBACK_TERMINAL_ID`` (this supervisor's terminal), which the
    worker-side ``send_message`` uses to deliver replies to this node.
    Omitting ``target_host`` preserves local behavior byte-for-byte.
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

        if target_host:
            return _assign_remote(
                agent_profile=agent_profile,
                worker_message=worker_message,
                current_terminal_id=current_terminal_id,
                target_host=target_host,
                working_directory=working_directory,
                engine=engine,
                model=model,
                use_worktree=use_worktree,
                ready_wait_seconds=ready_wait_seconds,
                callback_url=callback_url,
                remote_session_name=remote_session_name,
            )

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

        # A REMOTE worker has no local caller row; its cross-node supervisor is
        # recorded in CAO_CALLBACK_TERMINAL_ID instead (injected at creation) —
        # _send_to_inbox routes that ID to the supervisor's node. Checked BEFORE
        # the caller lookup below, which would otherwise GET a caller_id that
        # this node's DB cannot have.
        if not receiver_id:
            _, callback_terminal_id = _callback_route()
            if callback_terminal_id:
                receiver_id = callback_terminal_id

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


def _delete_terminal_impl(terminal_id: str, target_host: Optional[str] = None) -> Dict[str, Any]:
    """Implementation of delete_terminal logic.

    Kills the tmux window and removes the terminal record. Used both by the
    ``delete_terminal`` MCP tool and by ``cao agent cancel --delete``.

    ``target_host``: delete a terminal that lives on ANOTHER node (the same
    one-agent-per-pod placement assign/handoff accept). Omitted, this targets
    the local cao-server exactly as before. The MCP tool wrapper launders
    pydantic's ``FieldInfo`` default into ``None`` before calling; the CLI
    passes a real ``Optional[str]``, so no laundering happens here.
    """
    # Hoisted above the try because the HTTPError arm below interpolates it: if
    # it were assigned inside, a raise before that line would turn a reportable
    # failure into an UnboundLocalError.
    location = f" on node {target_host}" if target_host else ""
    try:
        base_url = _resolve_target_base_url(target_host) if target_host else API_BASE_URL
        response = requests.delete(
            f"{base_url}/terminals/{terminal_id}",
            headers=_auth_headers() or None,
            # A remote node that is unreachable must fail on CONNECT rather than
            # hang for the full read timeout; a local delete keeps its single
            # scalar timeout so default-path behavior is unchanged.
            timeout=(REMOTE_CONNECT_TIMEOUT, _mcp_timeout()) if target_host else _mcp_timeout(),
        )
        if response.status_code == 409:
            return {
                "success": False,
                "message": (
                    f"Terminal {terminal_id}{location} cleanup is pending; retry "
                    "delete_terminal after the Grok process exits."
                ),
            }
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success", False):
            return {
                "success": False,
                "message": (
                    f"Terminal {terminal_id}{location} cleanup is pending; retry "
                    "delete_terminal after the Grok process exits."
                ),
            }
        return {
            "success": True,
            "message": f"Terminal {terminal_id}{location} deleted successfully",
        }
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return {"success": False, "message": f"Terminal {terminal_id}{location} not found"}
        if e.response is not None and e.response.status_code == 409:
            return {
                "success": False,
                "message": (
                    f"Terminal {terminal_id}{location} cleanup is pending; retry "
                    "delete_terminal after the Grok process exits."
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
