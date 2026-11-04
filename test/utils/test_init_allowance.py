"""Issue #710: client-side allowances for server-side provider init.

The defect these guard: a client deadline sized from
``server.mcp_request_timeout`` (an ordinary-request budget) while the server is
waiting out ``provider.initialize()`` (a ``provider_init_timeout`` budget).
Every allowance has to move when ``provider_init_timeout`` moves, which is only
true if it is DERIVED from it rather than written down a second time.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

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
    from cli_agent_orchestrator.utils.agent_profiles import ProfileSource

    profile = SimpleNamespace(provider_init_timeout=300)
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_launch_profile",
        return_value=(profile, ProfileSource.INSTALLED),
    ):
        assert resolve_init_timeout("slow-agent", _SETTINGS) == 300


def test_resolve_init_timeout_does_not_let_a_lower_override_shorten_the_wait():
    """Review on PR #1002, P3-1 (setup 2). ``BaseProvider.get_init_timeout``
    prefers the override, but six providers (``copilot_cli``, ``cursor_cli``,
    ``hermes``, ``kiro_cli``, ``omp``, ``opencode_cli``) read the global value
    directly and never see it. A client that trusted an override of 30 against
    a global 300 would give up at 430s on a Kiro init the server still allowed
    600s for. The larger of the two covers both kinds of provider."""
    from cli_agent_orchestrator.utils.agent_profiles import ProfileSource

    profile = SimpleNamespace(provider_init_timeout=30)
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_launch_profile",
        return_value=(profile, ProfileSource.INSTALLED),
    ):
        assert resolve_init_timeout("fast-agent", dict(_SETTINGS, provider_init_timeout=300)) == 300


def test_resolve_init_timeout_falls_back_when_the_profile_cannot_be_loaded():
    """Sizing a wait must never be the thing that fails the call.

    The server resolves the real profile itself; a load failure here only means
    this process cannot see it.
    """
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_launch_profile",
        side_effect=FileNotFoundError("no such profile"),
    ):
        assert resolve_init_timeout("missing", _SETTINGS) == 60


def test_resolve_init_timeout_skips_reserved_ephemeral_profile_override():
    """Generated profiles have no override; MCP sizing must not load their registry."""
    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_launch_profile",
        side_effect=AssertionError("MCP sizing must not load an ephemeral profile"),
    ) as load:
        assert resolve_init_timeout("Agent-fixup-1a2b", _SETTINGS) == 60
        load.assert_not_called()


def test_resolve_init_timeout_falls_back_when_ephemeral_profile_unavailable():
    from cli_agent_orchestrator.utils.agent_profiles import EphemeralProfileUnavailable

    with patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_launch_profile",
        side_effect=EphemeralProfileUnavailable("gone"),
    ):
        assert resolve_init_timeout("Agent-fixup-1a2b", _SETTINGS) == 60


def test_init_allowance_covers_more_than_one_init_phase():
    """A successful init legitimately spans several ``provider_init_timeout``
    waits (Claude's shell wait + startup-prompt handler + readiness wait is 3T;
    Kiro's is 2T), so an allowance of exactly T is short for a path that still
    succeeds. At the stock T=60, the Antigravity-shaped fixed floor
    (10 + 2*180 = 370) exceeds the linear 4T (240), so it is the one that sets
    the budget."""
    from cli_agent_orchestrator.providers import antigravity_cli

    assert init_allowance(None, _SETTINGS) > 60
    assert (
        init_allowance(None, _SETTINGS)
        == antigravity_cli.SHELL_WAIT_TIMEOUT_S + 2 * antigravity_cli.READY_TIMEOUT_FLOOR_S + 60
    )


def test_init_allowance_linear_bound_is_three_phases_plus_margin():
    """Review on PR #1002, P3-3: the stated basis of the linear bound. The
    longest T-scaling successful path is about 3T (Claude; Kimi once T >= 120),
    and Kiro's is 2T -- the 4T ``--legacy-ui`` retry an earlier comment cited
    was removed in #691. The bound must still cover 3T with margin, and once T
    is past every fixed floor it must be exactly linear in T."""
    from cli_agent_orchestrator.utils.init_allowance import _INIT_PHASES

    assert _INIT_PHASES >= 3 + 1
    big = dict(_SETTINGS, provider_init_timeout=1000)
    assert init_allowance(None, big) == _INIT_PHASES * 1000 + 60


def test_init_allowance_scales_with_provider_init_timeout():
    """The property that makes this a derivation and not a literal.

    Once T is large enough that the linear ``_INIT_PHASES * T`` bound exceeds
    Antigravity's fixed-floor shape, the allowance scales linearly with T."""
    settings_300 = dict(_SETTINGS, provider_init_timeout=300)
    settings_600 = dict(_SETTINGS, provider_init_timeout=600)
    assert init_allowance(None, settings_600) == 2 * init_allowance(None, settings_300) - 60


@pytest.mark.parametrize("t", [1, 60, 120, 179, 180, 181, 300, 600])
def test_init_allowance_covers_antigravity_cli_successful_init(t):
    """``antigravity_cli.initialize`` is a fixed shell wait, then a startup-dialog
    phase capped at ``max(floor, T)``, then a readiness phase capped at
    ``max(floor, T)`` -- and the handler returns rather than raising at its outer
    cap, so hitting all three caps is still a SUCCESSFUL init. The allowance
    must cover that path at every T or a successful init is reported as a
    client-side failure. The floors are the provider's own constants (review on
    PR #1002, P3-3), so raising one there fails this test until the allowance
    follows."""
    from cli_agent_orchestrator.providers import antigravity_cli

    settings = dict(_SETTINGS, provider_init_timeout=t)
    antigravity_init_cost = antigravity_cli.SHELL_WAIT_TIMEOUT_S + 2 * max(
        antigravity_cli.READY_TIMEOUT_FLOOR_S, t
    )
    assert init_allowance(None, settings) >= antigravity_init_cost


@pytest.mark.parametrize("t", [1, 60, 119, 120, 121, 300, 600])
def test_init_allowance_covers_kimi_cli_successful_init(t):
    """``kimi_cli.initialize`` is a shell wait up to T, then a startup-dialog
    phase and a readiness phase each capped at ``max(floor, T)``. The allowance
    must cover that path at every T; the floor is the provider's own constant."""
    from cli_agent_orchestrator.providers import kimi_cli

    settings = dict(_SETTINGS, provider_init_timeout=t)
    kimi_init_cost = t + 2 * max(kimi_cli.READY_TIMEOUT_FLOOR_S, t)
    assert init_allowance(None, settings) >= kimi_init_cost


def test_init_allowance_floors_are_the_provider_modules_constants():
    """Review on PR #1002, P3-3: the allowance must not carry its own copy of a
    provider floor. The provider constants are the ones ``initialize`` spends,
    so a change there must change the allowance here without a second edit."""
    from cli_agent_orchestrator.providers import antigravity_cli, kimi_cli

    with (
        patch.object(antigravity_cli, "READY_TIMEOUT_FLOOR_S", 1000.0),
        patch.object(antigravity_cli, "SHELL_WAIT_TIMEOUT_S", 7.0),
    ):
        assert init_allowance(None, _SETTINGS) == int(7.0 + 2 * 1000.0) + 60

    with patch.object(kimi_cli, "READY_TIMEOUT_FLOOR_S", 2000.0):
        assert init_allowance(None, _SETTINGS) == int(60 + 2 * 2000.0) + 60


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
        assert synchronous_create_timeout() == (_CONNECT_TIMEOUT_S, 430.0)


@pytest.mark.parametrize("set_cao_home", [True, False])
def test_mcp_ephemeral_init_allowance_never_connects_registry(tmp_path, set_cao_home):
    """Exercise the three MCP caller aliases in a fresh process with a live profile."""
    import json
    import os
    import subprocess
    import sys

    home = tmp_path / "home"
    home.mkdir()
    cao = tmp_path / "cao" if set_cao_home else home / ".aws" / "cli-agent-orchestrator"
    live = cao / "ephemeral" / "live"
    live.mkdir(parents=True)
    (live / "ACDC-reviewer-1a2b.md").write_text(
        "---\nname: ACDC-reviewer-1a2b\ndescription: test\nprovider: claude_code\n"
        "provider_init_timeout: 999\n---\nBody.\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CAO_", "HERDR_", "TMUX"))
    }
    env.update(HOME=str(home), TMUX_TMPDIR=str(tmp_path / "tmux"), CAO_API_PORT="9")
    if set_cao_home:
        env["CAO_HOME_DIR"] = str(cao)
    script = """
import json
from sqlalchemy.engine import Engine
seen = []
original = Engine.connect
def spy(self, *args, **kwargs):
    seen.append(str(self.url))
    return original(self, *args, **kwargs)
Engine.connect = spy
from cli_agent_orchestrator.mcp_server import server, app_tools
from cli_agent_orchestrator.ops_mcp_server import server as ops
from cli_agent_orchestrator.utils import orchestration
from cli_agent_orchestrator.constants import DATABASE_FILE
settings = {'provider_init_timeout': 60, 'mcp_request_timeout': 30}
results = [caller.synchronous_create_timeout('ACDC-reviewer-1a2b', settings)
           for caller in (orchestration, app_tools, ops)]
print(json.dumps({'engine_connects': seen, 'db_file_created': DATABASE_FILE.exists(),
                  'timeouts': results}))
"""
    child = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True)
    assert child.returncode == 0, child.stdout + child.stderr
    result = json.loads(child.stdout)
    assert result["engine_connects"] == [], result
    assert result["db_file_created"] is False, result
    assert result["timeouts"] == [[5.0, 430.0]] * 3, result
