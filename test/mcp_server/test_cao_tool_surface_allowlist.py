"""The whole CAO MCP tool surface is gated on the caller's allowlist (#671).

#769 and #811 guarded the tools that mint a new agent identity — ``assign``,
``handoff``, ``assign_elastic``, the ``workflow_run`` family,
``create_ephemeral_agent``. That closed the escalation path and left the other
~26 ``@mcp.tool`` registrations open: a bound terminal whose effective allowlist
omits ``@cao-mcp-server`` could still call ``memory_store``, ``memory_forget``,
``delete_terminal``, ``update_metadata`` and the rest.

Nothing else could have caught them. ``get_disallowed_tools`` skips every
``@``-prefixed entry, so the provider-native deny flags never name an MCP tool on
any provider — that is the mechanism this issue reported and it is unchanged.

Two properties under test, and they pull against each other:

* Every non-exempt CAO tool denies a bound caller without the grant, and the
  gate is installed once rather than per call site so a tool added later is
  gated by default.
* The callback protocol stays reachable. ``skills/cao-worker-protocols`` has an
  ASSIGNED worker report its result through ``send_message`` with no
  ``receiver_id``, and ``_resolve_child_allowed_tools`` gives a child whose
  profile declares ``tools: [fs_read]`` exactly ``["fs_read"]``. Gating
  ``send_message`` would strand that worker and hang its supervisor, so the
  exemption is a correctness requirement, not a loophole.

The seam is FastMCP's dispatch, which is what an agent goes through and what the
CLI does not: ``cao assign`` / ``cao handoff`` call the shared impls directly and
must stay unrestricted, so a middleware gate cannot reach them.
"""

import asyncio
import os
from unittest.mock import patch

import pytest
from fastmcp import Client, FastMCP

from cli_agent_orchestrator.mcp_server import server

BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}

# One representative per kind of authority left ungated after #769/#811.
UNGATED_BEFORE = (
    "memory_store",  # durable write
    "memory_forget",  # durable delete
    "memory_recall",  # durable read
    "delete_terminal",  # another terminal's lifecycle
    "update_metadata",  # another terminal's record
    "store_lesson",  # cross-agent learning write
    "report_outcome",
    "workflow_cancel",  # another run's lifecycle
    "find_profiles",  # reconnaissance
    "list_siblings",
    "load_skill",
    "workflow_status",
)


def _ctx(allowed_tools, profile_name="worker"):
    return {
        "terminal_id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "codex",
        "agent_profile": profile_name,
        "allowed_tools": allowed_tools,
    }


def _server(tool_names):
    """A FastMCP server exposing ``tool_names`` as trivial tools, gated."""
    mcp = FastMCP("cao-mcp-server-under-test")
    for name in tool_names:

        def _impl(_name=name) -> dict:
            return {"reached": _name}

        mcp.add_tool(server.Tool.from_function(_impl, name=name))
    server._install_allowlist_middleware(mcp)
    return mcp


def _call(mcp, name):
    async def go():
        async with Client(mcp) as client:
            result = await client.call_tool(name, {})
            return result.data

    return asyncio.run(go())


def _unbound_env():
    return {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}


class TestUngatedSurfaceNowDenies:
    """The tools #769 and #811 did not reach."""

    @pytest.mark.parametrize("tool", UNGATED_BEFORE)
    def test_bound_caller_without_the_grant_is_denied(self, tool):
        mcp = _server([tool])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
            ):
                with pytest.raises(Exception) as excinfo:
                    _call(mcp, tool)
        assert "@cao-mcp-server" in str(excinfo.value)
        assert tool in str(excinfo.value)

    @pytest.mark.parametrize("tool", UNGATED_BEFORE)
    def test_the_grant_allows(self, tool):
        mcp = _server([tool])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server,
                "_get_terminal_context_from_env",
                return_value=_ctx(["fs_read", "@cao-mcp-server"]),
            ):
                assert _call(mcp, tool) == {"reached": tool}

    @pytest.mark.parametrize("tool", UNGATED_BEFORE)
    def test_unrestricted_allows(self, tool):
        mcp = _server([tool])
        with patch.dict(os.environ, BOUND):
            with patch.object(server, "_get_terminal_context_from_env", return_value=_ctx(["*"])):
                assert _call(mcp, tool) == {"reached": tool}

    def test_a_tool_nobody_thought_to_list_is_gated_by_default(self):
        """The regression that per-site guards cannot prevent.

        A tool registered after this change — by a later PR or by a plugin's
        ``on_mcp_server`` hook — is covered without anyone remembering to add a
        guard. That default is the point of moving the check to one place.
        """
        mcp = _server(["some_tool_added_next_quarter"])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
            ):
                with pytest.raises(Exception) as excinfo:
                    _call(mcp, "some_tool_added_next_quarter")
        assert "@cao-mcp-server" in str(excinfo.value)


class TestCallbackProtocolStaysReachable:
    """A restricted worker must still be able to report, or CAO deadlocks."""

    @pytest.mark.parametrize("tool", sorted(server.CALLBACK_EXEMPT_TOOLS))
    def test_exempt_tool_allows_a_caller_without_the_grant(self, tool):
        mcp = _server([tool])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
            ):
                assert _call(mcp, tool) == {"reached": tool}

    def test_send_message_is_exempt(self):
        """Pinned against a future tightening: cao-worker-protocols depends on it.

        An assigned worker reports with ``send_message`` and no ``receiver_id``.
        Gating it on ``@cao-mcp-server`` makes any child with an explicit
        ``tools:`` list unable to answer, and its supervisor waits forever.
        """
        assert "send_message" in server.CALLBACK_EXEMPT_TOOLS

    def test_the_exemption_stays_small(self):
        """The exempt set is the callback protocol, not a convenience list."""
        assert server.CALLBACK_EXEMPT_TOOLS == frozenset(
            {"send_message", "complete_assignment", "workflow_return", "answer_user_prompt"}
        )

    def test_no_exempt_tool_can_mint_an_identity(self):
        """None of the delegation tools leaked into the exemption."""
        delegation = {
            "assign",
            "handoff",
            "assign_elastic",
            "create_ephemeral_agent",
            "workflow_run",
            "workflow_resume",
            "workflow_start",
        }
        assert not (server.CALLBACK_EXEMPT_TOOLS & delegation)


class TestPolicyIsNotDuplicated:
    """The gate defers to ``_tool_denied_reason``; it does not restate the policy."""

    def test_operator_context_is_not_restricted(self):
        """``cao`` run by a human has no caller identity and must not be gated."""
        mcp = _server(["memory_store"])
        with patch.dict(os.environ, _unbound_env(), clear=True):
            assert _call(mcp, "memory_store") == {"reached": "memory_store"}

    def test_unresolvable_bound_caller_fails_closed(self):
        mcp = _server(["memory_store"])
        with patch.dict(os.environ, BOUND):
            with patch.object(server, "_get_terminal_context_from_env", return_value=None):
                with pytest.raises(Exception) as excinfo:
                    _call(mcp, "memory_store")
        assert "could not be resolved" in str(excinfo.value)

    def test_lookup_failure_fails_closed(self):
        mcp = _server(["memory_store"])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server,
                "_get_terminal_context_from_env",
                side_effect=RuntimeError("cao-server unreachable"),
            ):
                with pytest.raises(Exception) as excinfo:
                    _call(mcp, "memory_store")
        assert "cannot authorize" in str(excinfo.value)

    def test_the_gate_asks_tool_denied_reason(self):
        """One source of policy, so #769's semantics cannot drift from this gate."""
        mcp = _server(["memory_store"])
        with patch.dict(os.environ, BOUND):
            with patch.object(server, "_tool_denied_reason", return_value=None) as policy:
                _call(mcp, "memory_store")
        policy.assert_called_once_with("memory_store")

    def test_an_exempt_tool_does_not_even_consult_the_policy(self):
        mcp = _server(["send_message"])
        with patch.dict(os.environ, BOUND):
            with patch.object(server, "_tool_denied_reason") as policy:
                _call(mcp, "send_message")
        policy.assert_not_called()


class TestInstallation:
    def test_the_real_server_has_the_gate_installed(self):
        """Not just installable — installed on the module-level ``mcp``."""
        middlewares = getattr(server.mcp, "middleware", None)
        assert middlewares is not None
        assert any(type(m).__name__ == "_AllowlistGate" for m in middlewares)

    def test_a_build_without_middleware_fails_closed(self):
        """A FastMCP without ``add_middleware`` must raise, not silently start ungated."""

        class _NoMiddleware:
            pass

        with pytest.raises(RuntimeError):
            server._install_allowlist_middleware(_NoMiddleware())


class TestDiscoveryCarveOut:
    """``discovery`` is a separate per-tool grant (docs/tool-restrictions.md:120-124).

    The gate must not require ``@cao-mcp-server`` for the two tools that grant
    already covers independently -- ``_require_discovery_marker`` still does the
    real, live-looked-up enforcement when the tool runs.
    """

    @pytest.mark.parametrize("tool", sorted(server.DISCOVERY_GATED_TOOLS))
    def test_the_discovery_grant_alone_passes_the_gate(self, tool):
        mcp = _server([tool])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["discovery"])
            ):
                assert _call(mcp, tool) == {"reached": tool}

    @pytest.mark.parametrize("tool", sorted(server.DISCOVERY_GATED_TOOLS))
    def test_neither_discovery_nor_cao_mcp_server_is_still_denied(self, tool):
        mcp = _server([tool])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
            ):
                with pytest.raises(Exception) as excinfo:
                    _call(mcp, tool)
        assert "@cao-mcp-server" in str(excinfo.value)

    def test_the_carve_out_does_not_widen_to_other_tools(self):
        """``discovery`` grants only the two tools in ``DISCOVERY_GATED_TOOLS``."""
        mcp = _server(["memory_store"])
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["discovery"])
            ):
                with pytest.raises(Exception) as excinfo:
                    _call(mcp, "memory_store")
        assert "@cao-mcp-server" in str(excinfo.value)

    def test_the_set_is_exactly_the_two_documented_tools(self):
        assert server.DISCOVERY_GATED_TOOLS == frozenset({"list_siblings", "update_metadata"})
