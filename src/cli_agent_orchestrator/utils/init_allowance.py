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
# path legitimately spans several T-bounded phases. The longest that scales
# with T is about 3T: Claude's shell wait + startup-prompt handler outer cap +
# readiness wait (``claude_code.initialize``), and Kimi's shell wait + two
# phases of ``max(120, T)`` once T >= 120 (``kimi_cli.initialize``). Kiro's is
# 2T (``kiro_cli.initialize``; the 4T ``--legacy-ui`` retry that an earlier
# version of this comment cited was removed in #691). The fourth T is margin
# for settle time and for the event loop being busy with other creates.
_INIT_PHASES = 4

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


def _antigravity_successful_init_s(timeout: int) -> float:
    """Longest SUCCESSFUL ``antigravity_cli.initialize`` at init timeout ``timeout``.

    A fixed shell wait, then a startup-dialog phase and a readiness phase each
    capped at ``max(floor, T)`` -- and the dialog handler returns rather than
    raising at its cap, so hitting all three caps is still a successful init.
    At the stock T=60 this is 10 + 2*180 = 370s, LONGER than the linear
    ``_INIT_PHASES * T`` (240s); a plain linear allowance would make a
    successful Antigravity init look like a failure. The floors are read from
    the provider module so a change there moves this bound with it.
    """

    from cli_agent_orchestrator.providers import antigravity_cli

    return antigravity_cli.SHELL_WAIT_TIMEOUT_S + 2 * max(
        antigravity_cli.READY_TIMEOUT_FLOOR_S, timeout
    )


def _kimi_successful_init_s(timeout: int) -> float:
    """Longest SUCCESSFUL ``kimi_cli.initialize`` at init timeout ``timeout``.

    A shell wait of up to T, then a startup-dialog phase and a readiness phase
    each capped at ``max(floor, T)``. At the stock T=60 this is 60 + 2*120 =
    300s. The floor is read from the provider module.
    """

    from cli_agent_orchestrator.providers import kimi_cli

    return timeout + 2 * max(kimi_cli.READY_TIMEOUT_FLOOR_S, timeout)


def init_allowance(
    agent_profile: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> int:
    """Seconds a client should allow for server-side init plus delivery confirmation.

    Takes the largest of the linear ``_INIT_PHASES * T`` bound and the two
    fixed-floor provider shapes (Antigravity, Kimi) so a successful init on any
    known shape of provider stays inside the budget, then adds the delivery
    confirmation headroom.
    """

    timeout = resolve_init_timeout(agent_profile, settings)
    bound = max(
        _INIT_PHASES * timeout,
        _antigravity_successful_init_s(timeout),
        _kimi_successful_init_s(timeout),
    )
    return int(bound) + _DELIVERY_CONFIRM_ALLOWANCE


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
