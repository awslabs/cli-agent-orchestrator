"""Issue #710: client-side allowances for server-side provider init.

The defect these guard: a client deadline sized from
``server.mcp_request_timeout`` (an ordinary-request budget) while the server is
waiting out ``provider.initialize()`` (a ``provider_init_timeout`` budget).
Every allowance has to move when ``provider_init_timeout`` moves, which is only
true if it is DERIVED from it rather than written down a second time.
"""

from types import SimpleNamespace
from unittest.mock import patch

from cli_agent_orchestrator.utils.init_allowance import (
    _CONNECT_TIMEOUT_S,
    init_allowance,
    resolve_init_timeout,
    synchronous_create_timeout,
)

_SETTINGS = {"provider_init_timeout": 60, "mcp_request_timeout": 30}


def test_resolve_init_timeout_uses_server_setting_without_a_profile():
    assert resolve_init_timeout(None, _SETTINGS) == 60


def test_resolve_init_timeout_prefers_the_profile_override():
    """Mirrors ``BaseProvider.get_init_timeout``: the profile wins.

    A containerized profile declaring a several-hundred-second init is the case
    the issue called out as "strictly worse for the slower providers" -- the
    client budget has to follow the profile, not the server default.
    """
    profile = SimpleNamespace(provider_init_timeout=300)
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
        return_value=profile,
    ):
        assert resolve_init_timeout("slow-agent", _SETTINGS) == 300


def test_resolve_init_timeout_falls_back_when_the_profile_cannot_be_loaded():
    """Sizing a wait must never be the thing that fails the call.

    The server resolves the real profile itself; a load failure here only means
    this process cannot see it.
    """
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
        side_effect=FileNotFoundError("no such profile"),
    ):
        assert resolve_init_timeout("missing", _SETTINGS) == 60


def test_init_allowance_covers_more_than_one_init_phase():
    """A successful init legitimately spans several ``provider_init_timeout``
    waits (Claude 3T + settle, Kiro's legacy fallback 4T), so an allowance of
    exactly T is short for a path that still succeeds."""
    assert init_allowance(None, _SETTINGS) > 60
    assert init_allowance(None, _SETTINGS) == 4 * 60 + 60


def test_init_allowance_scales_with_provider_init_timeout():
    """The property that makes this a derivation and not a literal."""
    doubled = dict(_SETTINGS, provider_init_timeout=120)
    assert init_allowance(None, doubled) == 2 * init_allowance(None, _SETTINGS) - 60


def test_synchronous_create_timeout_widens_only_the_read_half():
    """``timeout=`` as a scalar covers connect AND read, so a widened scalar
    makes an unreachable cao-server take minutes to report -- the objection
    recorded in ``test_launch_create_call_keeps_mcp_request_timeout``. A
    ``(connect, read)`` tuple widens only the wait that needs it."""
    connect, read = synchronous_create_timeout(None, _SETTINGS)
    assert connect == _CONNECT_TIMEOUT_S
    assert read == float(init_allowance(None, _SETTINGS))


def test_synchronous_create_timeout_reads_settings_when_none_passed():
    with patch(
        "cli_agent_orchestrator.services.settings_service.get_server_settings",
        return_value=_SETTINGS,
    ):
        assert synchronous_create_timeout() == (_CONNECT_TIMEOUT_S, 300.0)
