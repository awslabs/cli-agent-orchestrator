"""The ops control plane is gated on the caller's tools allowlist (#1062).

#972 gated ``cao-mcp-server`` and recorded THIS surface as knowingly ungated, in
``UNGATED_SURFACES``, for its own issue. The gap it recorded: ``CAO_TERMINAL_ID``
is forwarded into every server in a profile's ``mcpServers`` map, so a bound
terminal whose profile listed ``cao-ops-mcp-server`` reached all 18 ops tools --
``launch_session``, ``install_profile``, ``shutdown_session``,
``send_session_message`` and the seven ``decisions_*`` tools -- with its
``tools:`` allowlist unconsulted.

Every test here goes through REAL FastMCP dispatch (``fastmcp.Client`` against the
module-level ``ops_server.mcp``), so what is under test is the gate as an agent
meets it, not a stand-in. Two things are faked, and neither is the gate:

* the terminal row, at the HTTP boundary ``mcp_utils.get_json`` -- a real
  ``allowed_tools`` list, resolved by the real ``caller_terminal_context``;
* each tool's effect, so an ALLOWED call neither reaches cao-server nor rewrites
  the operator's decision settings. Recording those boundaries is also how an
  allowed call is proved allowed: dispatch reached the body.

The tool list is INTROSPECTED from the live registry rather than written out, so
an ops tool a later PR adds is covered here by default -- the half of #671 that
per-registration guards cannot deliver.
"""

import asyncio
import os
from pathlib import Path
from typing import Any, Dict, List, Mapping
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastmcp import Client
from fastmcp.exceptions import ToolError

from cli_agent_orchestrator.decisions import egress
from cli_agent_orchestrator.decisions import operator as decision_operator
from cli_agent_orchestrator.decisions import settings as decision_settings
from cli_agent_orchestrator.mcp_server import utils as mcp_utils
from cli_agent_orchestrator.ops_mcp_server import gate as ops_gate
from cli_agent_orchestrator.ops_mcp_server import server as ops_server

GRANT = "@cao-ops-mcp-server"
BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}

#: Minimal valid arguments per ops tool. Hand-written -- there is no introspecting
#: a meaningful ``point`` -- but pinned against the live registry by
#: ``test_the_argument_table_covers_every_ops_tool``, so a new ops tool fails here
#: instead of quietly dropping out of the parametrization below.
#:
#: ``launch_session`` carries an ``initial_message`` so the tool uses the ordinary
#: HTTP budget rather than deriving one from ``provider_init_timeout`` (#710);
#: either path is gated identically, and this one touches no profile on disk.
ARGS: Dict[str, Dict[str, Any]] = {
    "decisions_list": {},
    "decisions_off": {},
    "decisions_purge": {"all_records": True},
    "decisions_set_exclusions": {"add": "reviewer"},
    "decisions_set_point": {"point": "model.route", "state": "off"},
    "decisions_set_tier": {"provider": "codex", "tier": "small"},
    "decisions_status": {},
    "get_profile_details": {"name": "worker"},
    "get_session_info": {"session_name": "cao-session"},
    "get_terminal_output": {"terminal_id": "a1b2c3d4"},
    "get_terminal_status": {"terminal_id": "a1b2c3d4"},
    "install_profile": {"source": "worker"},
    "launch_session": {"agent_profile": "worker", "initial_message": "go"},
    "list_profiles": {},
    "list_sessions": {},
    "read_session_output": {"terminal_id": "a1b2c3d4"},
    "send_session_message": {"terminal_id": "a1b2c3d4", "message": "hi"},
    "shutdown_session": {"session_name": "cao-session"},
}

OPS_TOOLS = sorted(tool.name for tool in asyncio.run(ops_server.mcp._list_tools()))


def _terminal_row(allowed_tools: List[str]) -> Dict[str, Any]:
    """A terminal row as ``GET /terminals/{id}`` publishes it, with a real allowlist.

    A recorded ``allowed_tools`` IS the effective list
    (``caller_effective_allowed_tools``, mirroring ``create_terminal``), so the
    gate resolves exactly this list and no profile is read from disk.
    """
    return {
        "id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "codex",
        "agent_profile": "worker",
        "allowed_tools": allowed_tools,
        "ephemeral": False,
    }


def _as_bound(allowed_tools: List[str]):
    """Answer the caller's terminal lookup with a row carrying ``allowed_tools``."""
    return patch.object(mcp_utils, "get_json", return_value=_terminal_row(allowed_tools))


def _unbound_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}


def _not_found() -> requests.HTTPError:
    """A 404 from the terminal lookup: "no such terminal", not "no identity"."""
    response = MagicMock()
    response.status_code = 404
    return requests.HTTPError("404", response=response)


def _dispatch(tool: str, arguments: Mapping[str, Any]):
    """Call ``tool`` through real FastMCP dispatch; return the error text or None."""

    async def go():
        async with Client(ops_server.mcp) as client:
            try:
                await client.call_tool(tool, dict(arguments))
            except ToolError as exc:
                return str(exc)
            return None

    return asyncio.run(go())


@pytest.fixture
def reached(monkeypatch) -> List[str]:
    """Neutralize every ops tool's effect; return the boundaries dispatch reached.

    An allowed call must not reach cao-server or rewrite the operator's decision
    settings, and "did the gate allow it?" is answered by whether dispatch got
    this far -- not by a return value, which differs per tool.
    """
    calls: List[str] = []

    def _request(method, url, **kwargs):
        calls.append(f"http:{method}")
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "success": True,
            "message": "stub",
            "id": "a1b2c3d4",
            "output": "",
            "terminals": [],
        }
        return response

    monkeypatch.setattr(ops_server.requests, "request", _request)

    def _recorder(name, result):
        def _stub(*args, **kwargs):
            calls.append(f"decision:{name}")
            return result

        return _stub

    # ``decision_tools`` resolves these off the module at call time, so patching
    # the module is enough and the REGISTERED tools stay the real ones.
    monkeypatch.setattr(decision_operator, "status", _recorder("status", {}))
    monkeypatch.setattr(decision_operator, "off", _recorder("off", {"success": True}))
    monkeypatch.setattr(decision_operator, "set_point", _recorder("set_point", {}))
    monkeypatch.setattr(decision_operator, "records", _recorder("records", []))
    monkeypatch.setattr(decision_operator, "purge", _recorder("purge", 0))
    monkeypatch.setattr(decision_settings, "set_tier", _recorder("set_tier", None))
    monkeypatch.setattr(decision_settings, "set_exclusions", _recorder("set_exclusions", None))
    return calls


class TestBoundCallerWithoutTheGrant:
    """AC1: refused on every ops tool except the fail-safe."""

    @pytest.mark.parametrize("tool", OPS_TOOLS)
    def test_every_ops_tool_is_refused(self, tool, reached):
        with patch.dict(os.environ, BOUND), _as_bound(["fs_read", "@cao-mcp-server"]):
            error = _dispatch(tool, ARGS[tool])
        if tool in ops_gate.FAIL_SAFE_EXEMPT_TOOLS:
            assert error is None, f"{tool} is the fail-safe but the gate refused it"
            assert reached == ["decision:off"]
            return
        assert error is not None, (
            f"{tool} is registered on cao-ops-mcp-server and reachable by a bound "
            f"terminal whose allowlist omits '{GRANT}'. Gate it, or add it to "
            f"ops_gate.FAIL_SAFE_EXEMPT_TOOLS with a reason."
        )
        assert GRANT in error
        assert tool in error
        assert reached == [], f"{tool} ran its body despite being refused"

    def test_the_agent_facing_grant_does_not_carry_the_control_plane(self, reached):
        """``@cao-mcp-server`` is delegation; this surface acts on the node."""
        with patch.dict(os.environ, BOUND), _as_bound(["@cao-mcp-server", "discovery"]):
            error = _dispatch("launch_session", ARGS["launch_session"])
        assert error is not None and GRANT in error
        assert reached == []

    def test_a_tool_a_later_pr_adds_is_refused_without_anyone_listing_it(self):
        """The inverted default, asked of the real policy on the real surface."""
        with patch.dict(os.environ, BOUND), _as_bound(["fs_read"]):
            denial = ops_gate.ops_tool_denied_reason("ops_tool_from_a_later_pr", {})
        assert denial is not None and GRANT in denial


class TestBoundCallerWithTheGrant:
    """AC2: allowed on every ops tool, except lifting a halt."""

    @pytest.mark.parametrize("tool", OPS_TOOLS)
    def test_every_ops_tool_is_allowed(self, tool, reached):
        with patch.dict(os.environ, BOUND), _as_bound(["fs_read", GRANT]):
            error = _dispatch(tool, ARGS[tool])
        assert error is None or GRANT not in error, f"{tool} refused a caller holding {GRANT}"
        assert reached, f"{tool} never reached its body, so the gate did not allow it"

    def test_unrestricted_allows(self, reached):
        with patch.dict(os.environ, BOUND), _as_bound(["*"]):
            assert _dispatch("launch_session", ARGS["launch_session"]) is None
        assert reached

    @pytest.mark.parametrize("entry", ["@cao-*", "@*"])
    def test_a_documented_glob_grant_is_honoured(self, entry, reached):
        """The spelling rule is ``tool_mapping``'s, not a second exact-match one.

        ``docs/agent-plugins.md`` documents ``@plugin-*``-style globs as a way to
        name a server, and ``granted_mcp_servers`` honours them when it builds
        provider MCP policy. A gate comparing by exact membership would refuse a
        profile the provider layer had already granted.
        """
        with patch.dict(os.environ, BOUND), _as_bound(["fs_read", entry]):
            assert _dispatch("launch_session", ARGS["launch_session"]) is None
        assert reached

    def test_an_unrelated_glob_does_not_reach_this_surface(self, reached):
        with patch.dict(os.environ, BOUND), _as_bound(["fs_read", "@plugin-*"]):
            error = _dispatch("launch_session", ARGS["launch_session"])
        assert error is not None and GRANT in error
        assert reached == []


class TestLiftingAHaltIsOperatorOnly:
    """AC2/AC3, #1062 item 3.

    #955 made ``decisions_off`` the operator's kill switch, and
    ``settings.set_point(point, "shadow"|"on")`` unlinks the halt marker. Without
    this rule a caller holding the grant could undo an operator's
    ``cao decisions off`` over the same surface it was halted from.
    """

    @pytest.fixture
    def halted(self, monkeypatch, tmp_path):
        """A real halt marker, so the refusal is driven by real state."""
        marker = tmp_path / "decisions.halt"
        marker.touch()
        monkeypatch.setattr(egress, "HALT_FILE", marker)
        assert egress.is_halted()
        return marker

    @pytest.fixture
    def not_halted(self, monkeypatch, tmp_path):
        marker = tmp_path / "absent.halt"
        monkeypatch.setattr(egress, "HALT_FILE", marker)
        assert not egress.is_halted()
        return marker

    @pytest.mark.parametrize("state", ["shadow", "on"])
    def test_a_bound_caller_with_the_grant_cannot_lift_a_halt(self, state, halted, reached):
        with patch.dict(os.environ, BOUND), _as_bound(["*", GRANT]):
            error = _dispatch("decisions_set_point", {"point": "model.route", "state": state})
        assert error == ops_gate.HALT_LIFT_DENIAL
        assert reached == [], "set_point ran despite the refusal"
        assert halted.exists(), "the halt marker was removed by a refused call"

    def test_turning_a_point_off_under_a_halt_still_works(self, halted, reached):
        """Only the LIFTING states are refused; off reduces what the node does."""
        with patch.dict(os.environ, BOUND), _as_bound(["*", GRANT]):
            error = _dispatch("decisions_set_point", {"point": "model.route", "state": "off"})
        assert error is None and reached == ["decision:set_point"]

    @pytest.mark.parametrize("state", ["shadow", "on"])
    def test_with_no_halt_in_place_there_is_nothing_to_lift(self, state, not_halted, reached):
        with patch.dict(os.environ, BOUND), _as_bound(["*", GRANT]):
            error = _dispatch("decisions_set_point", {"point": "model.route", "state": state})
        assert error is None and reached == ["decision:set_point"]

    def test_a_bound_caller_without_the_grant_hears_about_the_grant(self, halted, reached):
        """Order: the missing grant is the one thing such a caller needs to hear."""
        with patch.dict(os.environ, BOUND), _as_bound(["fs_read"]):
            error = _dispatch("decisions_set_point", {"point": "model.route", "state": "on"})
        assert error is not None and GRANT in error
        assert reached == []

    @pytest.mark.parametrize("state", ["shadow", "on"])
    def test_an_operator_context_may_still_lift_a_halt(self, state, halted, reached):
        """AC3: the operator's own MCP host is unchanged, halt or no halt."""
        with patch.dict(os.environ, _unbound_env(), clear=True):
            error = _dispatch("decisions_set_point", {"point": "model.route", "state": state})
        assert error is None and reached == ["decision:set_point"]

    def test_the_gate_has_no_private_copy_of_the_lifting_states(self, halted, reached, monkeypatch):
        """Widening the shared set moves the gate, because the gate asks for it.

        ``settings.set_point`` unlinks the marker for exactly
        ``HALT_LIFTING_STATES``; a guard with its own ``("shadow", "on")`` stops
        guarding the day the mutation gains a state. The mutation's own half of
        this is
        ``test/decisions/test_external_controls.py::test_the_mutation_lifts_the_halt_for_exactly_the_shared_states``.
        """
        monkeypatch.setattr(decision_settings, "HALT_LIFTING_STATES", frozenset({"off"}))
        with patch.dict(os.environ, BOUND), _as_bound(["*", GRANT]):
            refused = _dispatch("decisions_set_point", {"point": "model.route", "state": "off"})
            allowed = _dispatch("decisions_set_point", {"point": "model.route", "state": "on"})
        assert refused == ops_gate.HALT_LIFT_DENIAL
        assert allowed is None


class TestOperatorContextIsUnchanged:
    """AC3: no ``CAO_TERMINAL_ID`` is the supported operator context."""

    @pytest.mark.parametrize("tool", OPS_TOOLS)
    def test_every_ops_tool_is_allowed_and_no_identity_is_looked_up(self, tool, reached):
        with patch.dict(os.environ, _unbound_env(), clear=True):
            with patch.object(mcp_utils, "get_json", side_effect=AssertionError) as lookup:
                error = _dispatch(tool, ARGS[tool])
        assert error is None or GRANT not in error
        assert reached, f"{tool} never reached its body for an operator context"
        lookup.assert_not_called()


class TestFailsClosed:
    """A claimed identity that cannot be resolved denies, as on the CAO surface."""

    def test_an_unregistered_bound_caller_is_denied(self, reached):
        with patch.dict(os.environ, BOUND):
            with patch.object(mcp_utils, "get_json", side_effect=_not_found()):
                error = _dispatch("launch_session", ARGS["launch_session"])
        assert error is not None and "could not be resolved" in error
        assert reached == []

    def test_a_lookup_failure_is_denied(self, reached):
        with patch.dict(os.environ, BOUND):
            with patch.object(
                mcp_utils, "get_json", side_effect=RuntimeError("cao-server unreachable")
            ):
                error = _dispatch("launch_session", ARGS["launch_session"])
        assert error is not None and "cannot authorize" in error
        assert reached == []

    def test_a_malformed_terminal_id_is_denied(self, reached):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "not-a-terminal"}):
            error = _dispatch("launch_session", ARGS["launch_session"])
        assert error is not None and "could not be resolved" in error
        assert reached == []

    def test_the_fail_safe_is_reachable_even_when_identity_cannot_be_resolved(self, reached):
        """The point of a fail-safe: it must not need a working lookup either."""
        with patch.dict(os.environ, BOUND):
            with patch.object(
                mcp_utils, "get_json", side_effect=RuntimeError("cao-server unreachable")
            ):
                assert _dispatch("decisions_off", {}) is None
        assert reached == ["decision:off"]


class TestTheHaltLiftRuleIsEnforcedInsideTheMutation:
    """The reviewer's deterministic probe, as a test (#1062, independent review).

    Every other test in this file fakes each tool's effect; this one must not,
    because what is under test is precisely the gap between the middleware's
    decision and the real write. The probe: the middleware authorizes
    ``decisions_set_point(on)`` while no marker exists, THEN the real
    ``operator.off()`` publishes one, THEN the authorized mutation runs. The
    first implementation reported ``marker_after_off=True marker_after_set=False
    final_state=on`` -- an operator halt erased by a call that was allowed
    before the halt existed.

    The mutation-level matrix is
    ``test/decisions/test_external_controls.py::TestTheHaltLiftRuleLivesInTheMutation``.
    """

    @staticmethod
    def _call(tool, arguments):
        """Dispatch and return the tool's RESULT: a refusal here is not a ToolError."""

        async def go():
            async with Client(ops_server.mcp) as client:
                return (await client.call_tool(tool, dict(arguments))).data

        return asyncio.run(go())

    @pytest.mark.parametrize("state", ["shadow", "on"])
    def test_an_off_published_after_authorization_is_not_erased(self, state, monkeypatch):
        real_set_point = decision_settings.set_point
        halts = []

        def off_then_set(point, state, decider=None, *, posture):
            halts.append(decision_operator.off()["halted"])
            return real_set_point(point, state, decider, posture=posture)

        monkeypatch.setattr(decision_settings, "set_point", off_then_set)
        monkeypatch.setattr(egress, "drain", lambda timeout: True)
        with patch.dict(os.environ, BOUND), _as_bound(["*", GRANT]):
            result = self._call("decisions_set_point", {"point": "model.route", "state": state})
        assert halts == [True], "operator.off() did not publish a halt mid-call"
        assert result["success"] is False
        assert result["message"] == ops_gate.HALT_LIFT_DENIAL
        assert egress.is_halted(), "the operator's halt was erased by an authorized call"
        assert decision_operator.status()["points"]["model.route"]["state"] == "off"

    @pytest.mark.parametrize("state", ["shadow", "on"])
    def test_an_operator_context_is_not_refused_by_the_mutation(self, state, monkeypatch):
        """The same race, from an operator MCP host: the halt is theirs to lift."""
        marker = Path(str(egress.HALT_FILE))
        marker.touch()
        monkeypatch.setattr(egress, "drain", lambda timeout: True)
        with patch.dict(os.environ, _unbound_env(), clear=True):
            result = self._call("decisions_set_point", {"point": "model.route", "state": state})
        assert result["success"] is True
        assert not egress.is_halted()


class TestAnEmptyIdentityIsNotAnOperatorContext:
    """An explicitly empty ``CAO_TERMINAL_ID`` is a bound caller, and refused.

    Found by independent review of #1062: boundness was
    ``bool(os.environ.get("CAO_TERMINAL_ID"))``, so a PRESENT-but-empty value
    took the unbound branch and every non-exempt ops tool dispatched as an
    operator call with no row lookup (probe: ``EMPTY_ID outcome=allowed
    lookup_calls=0``). Reachable from a profile: until the companion provider fix
    a profile could list this server with ``env: {CAO_TERMINAL_ID: ""}``, omit
    ``@cao-ops-mcp-server``, and hold the whole control plane.

    The truth table for ``caller_is_bound`` itself is
    ``test/security/test_mcp_tool_gate_boundness.py``.
    """

    @pytest.mark.parametrize("value", ["", " ", "\t"])
    @pytest.mark.parametrize("tool", OPS_TOOLS)
    def test_every_ops_tool_is_refused_without_a_lookup(self, tool, value, reached):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": value}):
            with patch.object(mcp_utils, "get_json", side_effect=AssertionError) as lookup:
                error = _dispatch(tool, ARGS[tool])
        lookup.assert_not_called()
        if tool in ops_gate.FAIL_SAFE_EXEMPT_TOOLS:
            assert error is None, f"{tool} is the fail-safe but the gate refused it"
            assert reached == ["decision:off"]
            return
        assert error is not None, (
            f"{tool} dispatched for a caller whose CAO_TERMINAL_ID is {value!r}. An "
            "empty or whitespace value is a caller claiming an identity that cannot "
            "be resolved, not the operator context."
        )
        assert "could not be resolved" in error
        assert reached == [], f"{tool} ran its body despite being refused"

    @pytest.mark.parametrize("state", ["shadow", "on"])
    def test_an_empty_identity_cannot_lift_a_halt(self, state, monkeypatch, tmp_path, reached):
        """The halt is the thing the bypass was worth most: pin it directly."""
        marker = tmp_path / "decisions.halt"
        marker.touch()
        monkeypatch.setattr(egress, "HALT_FILE", marker)
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": ""}):
            error = _dispatch("decisions_set_point", {"point": "model.route", "state": state})
        assert error is not None and "could not be resolved" in error
        assert reached == []
        assert marker.exists()


class TestTheSurfaceDeclarations:
    def test_the_argument_table_covers_every_ops_tool(self):
        assert set(ARGS) == set(OPS_TOOLS), (
            "the ops tool surface changed; add the new tool's minimal arguments to "
            "ARGS so the parametrized gate tests above cover it."
        )

    def test_the_fail_safe_set_stays_small(self):
        """``decisions_off`` only ever turns things off; nothing else qualifies."""
        assert ops_gate.FAIL_SAFE_EXEMPT_TOOLS == frozenset({"decisions_off"})

    def test_no_fail_safe_tool_can_start_or_mint_anything(self):
        mutators = {
            "launch_session",
            "install_profile",
            "shutdown_session",
            "send_session_message",
            "decisions_set_point",
            "decisions_set_tier",
            "decisions_set_exclusions",
            "decisions_purge",
        }
        assert not (ops_gate.FAIL_SAFE_EXEMPT_TOOLS & mutators)

    def test_the_gate_is_installed_on_the_real_server(self):
        installed = [type(m).__name__ for m in getattr(ops_server.mcp, "middleware", [])]
        assert "_AllowlistGate" in installed

    def test_the_gate_is_installed_after_every_registration_hook(self):
        """Install order, so a plugin-registered operator tool lands inside it."""
        source = Path(ops_server.__file__).read_text(encoding="utf-8")
        install = source.index("\ninstall_ops_allowlist_middleware(mcp)")
        for hook in ("\nregister_decision_tools(mcp)", "\nregister_mcp_server_surfaces(mcp)"):
            assert source.index(hook) < install, (
                f"install_ops_allowlist_middleware(mcp) must run after {hook.strip()}, "
                "or tools registered by it sit outside the gate."
            )

    def test_a_build_without_middleware_fails_closed(self):
        class _NoMiddleware:
            pass

        with pytest.raises(RuntimeError):
            ops_gate.install_ops_allowlist_middleware(_NoMiddleware())
