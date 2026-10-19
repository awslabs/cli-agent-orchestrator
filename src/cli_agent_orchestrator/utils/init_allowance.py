"""Client-side allowance for a request the server answers only after init.

Issue #710. A handful of client call sites POST a create and then wait for
cao-server to finish ``provider.initialize()`` before it answers. The budget
such a call needs is a function of the SAME value the server will use --
``server.provider_init_timeout``, or the profile's own override of it
(``BaseProvider.get_init_timeout`` prefers the profile) -- not of
``server.mcp_request_timeout``, which sizes ordinary reads and sends. Pairing
them by hand is how a perfectly successful init came to be reported as a
failure: the client gave up at 30s while the server went on to create the
worker, so the caller retried against a worker that already existed.

Every client-side allowance for server-side init resolves through here, so
raising ``provider_init_timeout`` (or a profile's override) moves all of them
at once instead of leaving each site's literal behind.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

# Provider init is not one wait of ``provider_init_timeout`` (T). A successful
# path legitimately spans several T-bounded phases: Claude's shell wait +
# startup-prompt handler + readiness wait is 3T (+ settle), Kimi's is up to
# T + 2*max(120, T), Kiro's legacy fallback 4T. An allowance has to cover the
# longest of those or a valid init blows the client deadline while the server
# goes on to finish the work.
_INIT_PHASES = 4

# Antigravity's successful path (``antigravity_cli.initialize``) does not scale
# with T at the stock T=60: a 10s shell wait, then a startup-dialog phase
# capped at ``max(180, T)``, then a separate readiness phase capped at
# ``max(180, T)``. At defaults that is 10 + 2*180 = 370s, which is LONGER than
# ``_INIT_PHASES * T`` (240s) -- a plain ``_INIT_PHASES * T`` allowance would
# make a successful init look like a failure at the exact 300s a prior version
# of this budget granted. ``init_allowance`` takes the max of the two shapes so
# neither a large-T provider's linear cost nor Antigravity's fixed floor is
# ever underpriced.
_ANTIGRAVITY_SHELL_WAIT_S = 10.0
_ANTIGRAVITY_PHASE_FLOOR_S = 180.0

# Worst case of the server's confirm-and-resubmit loop after a send
# (``_DEFERRED_SUBMIT_CONFIRM_TIMEOUT`` x (1 + ``_DEFERRED_SUBMIT_MAX_RESUBMITS``)
# = 32s) with headroom for the pre-dispatch work (memory injection) and polling.
_DELIVERY_CONFIRM_ALLOWANCE = 60

# Connect budget for a synchronous create. Kept small and SEPARATE from the read
# budget: ``timeout=`` as a single scalar covers both, so widening the wait for
# init would also mean a dead server took minutes to report (the objection
# recorded in ``test_launch_create_call_keeps_mcp_request_timeout``). cao-server
# is localhost, so a slow connect means something is wrong, not far away --
# the same reasoning as ``ops_mcp_server.server._HTTP_TIMEOUT``.
_CONNECT_TIMEOUT_S = 5.0


def resolve_init_timeout(
    agent_profile: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Return the ``provider_init_timeout`` (seconds) the SERVER will apply.

    Mirrors ``BaseProvider.get_init_timeout``: the profile's own
    ``provider_init_timeout`` override wins over the server setting. Resolved
    through ``load_launch_profile``, the same loader the server uses
    (``terminal_service.py``, ``claude_code.py``, ``codex.py``) -- a reserved
    ephemeral name is served exclusively from the server-owned live store, and
    ``load_agent_profile`` alone cannot see it. A profile that cannot be loaded
    here falls back to the server default rather than failing the caller -- the
    server resolves the real profile itself; this only sizes a wait.
    """

    from cli_agent_orchestrator.services.settings_service import get_server_settings

    if settings is None:
        settings = get_server_settings()
    base = int(settings["provider_init_timeout"])

    if not agent_profile:
        return base

    from cli_agent_orchestrator.utils.agent_profiles import load_launch_profile

    try:
        profile, _source = load_launch_profile(agent_profile)
    except Exception:  # noqa: BLE001 -- sizing a wait must never fail the call
        return base
    override = getattr(profile, "provider_init_timeout", None)
    return int(override) if override is not None else base


def init_allowance(
    agent_profile: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Seconds a client should allow for server-side init plus delivery confirmation.

    Takes the larger of the linear ``_INIT_PHASES * T`` bound and Antigravity's
    fixed-floor shape (``_ANTIGRAVITY_SHELL_WAIT_S + 2 * max(_ANTIGRAVITY_PHASE_FLOOR_S, T)``)
    so a successful init on either shape of provider stays inside the budget.
    """

    timeout = resolve_init_timeout(agent_profile, settings)
    linear_bound = _INIT_PHASES * timeout
    fixed_floor_bound = _ANTIGRAVITY_SHELL_WAIT_S + 2 * max(_ANTIGRAVITY_PHASE_FLOOR_S, timeout)
    return int(max(linear_bound, fixed_floor_bound)) + _DELIVERY_CONFIRM_ALLOWANCE


def synchronous_create_timeout(
    agent_profile: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Tuple[float, float]:
    """``requests`` ``(connect, read)`` timeout for a create the server answers after init.

    The read half is :func:`init_allowance`; the connect half stays at
    ``_CONNECT_TIMEOUT_S`` so an unreachable cao-server still reports promptly.
    Use this -- never ``mcp_request_timeout`` -- for any POST that provokes a
    synchronous ``provider.initialize()`` (``POST /sessions`` without
    ``initial_message``, ``POST /sessions/{name}/terminals`` without
    ``defer_init``).
    """

    return (_CONNECT_TIMEOUT_S, float(init_allowance(agent_profile, settings)))
