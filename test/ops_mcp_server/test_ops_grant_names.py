"""Who actually holds ``@cao-ops-mcp-server`` (#1062, review of PR #1069).

``test_ops_tool_surface_allowlist.py`` proves the gate refuses a list without the
grant and allows a list with it. It hands the gate a list it wrote itself, so it
cannot say which list a REAL profile ends up with. Two findings lived in that gap:

* The gate reads the fixed selector, never the key a profile declares the server
  under. CAO's own operator agent-plugin declares it as ``cao-ops``, and the docs
  told an operator to grant ``@cao-ops`` -- a list the gate refuses.
* ``resolve_allowed_tools`` appends ``@<server>`` for every server a profile
  declares itself unless the profile pins an explicit list (#772). A role-default
  profile that declares ``cao-ops-mcp-server`` therefore HOLDS the grant, while
  the docs said that listing the server does not grant it.

So every test here starts from a profile, resolves its list with the functions
launch uses (``grantable_server_names`` and ``resolve_allowed_tools``), and drives
that list through the real gate. The documented lists are read out of the docs and
the dog-food script rather than retyped, so a doc that names a grant the gate
refuses fails here.
"""

import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest
import yaml
from fastmcp import Client
from fastmcp.exceptions import ToolError

from cli_agent_orchestrator.agent_plugins.mcp_delivery import grantable_server_names
from cli_agent_orchestrator.decisions import operator as decision_operator
from cli_agent_orchestrator.mcp_server import utils as mcp_utils
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.ops_mcp_server import gate as ops_gate
from cli_agent_orchestrator.ops_mcp_server import server as ops_server
from cli_agent_orchestrator.security.mcp_tool_gate import CAO_OPS_MCP_SERVER_SELECTOR
from cli_agent_orchestrator.utils import agent_profiles
from cli_agent_orchestrator.utils.tool_mapping import (
    granted_mcp_servers,
    resolve_allowed_tools,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_MCP_JSON = REPO_ROOT / "agent-plugin" / "cao" / "mcp.json"
AGENT_PLUGINS_DOC = REPO_ROOT / "docs" / "agent-plugins.md"
DOGFOOD_DIR = REPO_ROOT / "examples" / "agent-plugins" / "agent-plugins-dogfood"

GRANT = CAO_OPS_MCP_SERVER_SELECTOR
OPS_SCRIPT = "cao-ops-mcp-server"
BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}

#: A tool that mints a session, and the fail-safe that needs no grant.
GATED_TOOL = "launch_session"
FAIL_SAFE_TOOL = "decisions_off"


def _plugin_server_key() -> str:
    """The key CAO's own operator plugin declares the ops server under."""
    servers = json.loads(PLUGIN_MCP_JSON.read_text())["mcpServers"]
    keys = [name for name, entry in servers.items() if OPS_SCRIPT in entry.get("args", [])]
    assert len(keys) == 1, f"expected one entry launching {OPS_SCRIPT}, found {keys}"
    return keys[0]


PLUGIN_KEY = _plugin_server_key()


def _ops_entry() -> Dict[str, Any]:
    return {"command": "uvx", "args": ["--from", "cli-agent-orchestrator", OPS_SCRIPT]}


def _profile(
    *,
    key: Optional[str] = OPS_SCRIPT,
    role: Optional[str] = None,
    allowed: Optional[List[str]] = None,
) -> AgentProfile:
    """A profile declaring the ops server under ``key`` (``None`` declares none)."""
    servers: Dict[str, Any] = {"cao-mcp-server": {"command": "cao-mcp-server"}}
    if key is not None:
        servers[key] = _ops_entry()
    return AgentProfile(
        name="probe",
        description="probe",
        role=role,
        allowedTools=allowed,
        mcpServers=servers,
    )


def _recorded(profile: AgentProfile) -> List[str]:
    """The list launch records for ``profile``: the calls ``create_terminal`` makes."""
    return resolve_allowed_tools(
        profile.allowedTools, profile.role, grantable_server_names(profile)
    )


def _row(allowed_tools: Optional[List[str]]) -> Dict[str, Any]:
    return {
        "id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "kiro_cli",
        "agent_profile": "probe",
        "allowed_tools": allowed_tools,
        "ephemeral": False,
    }


def _denial(allowed_tools: Optional[List[str]], tool: str = GATED_TOOL) -> Optional[str]:
    """The real gate's verdict for a bound terminal whose row carries this list."""
    with (
        patch.dict(os.environ, BOUND),
        patch.object(mcp_utils, "get_json", return_value=_row(allowed_tools)),
    ):
        return ops_gate.ops_tool_denied_reason(tool, {})


def _assert_refused(denial: Optional[str]) -> None:
    assert denial is not None, "the gate allowed a list that does not hold the grant"
    assert GRANT in denial


# ── the documented lists, read from where an operator reads them ─────────────


def _yaml_block_lists(text: str) -> List[List[str]]:
    """``allowedTools`` of every fenced YAML block that names the plugin's server."""
    found = []
    for block in re.findall(r"```yaml\n(.*?)```", text, flags=re.DOTALL):
        if f"@{PLUGIN_KEY}" not in block or "allowedTools" not in block:
            continue
        found.append(list(yaml.safe_load(block)["allowedTools"]))
    return found


def _inline_lists(text: str) -> List[List[str]]:
    """Every inline ``allowedTools: [...]`` that names the plugin's server."""
    found = []
    for body in re.findall(r"allowedTools: (\[[^\]\n]*\])", text):
        entries = [str(entry) for entry in yaml.safe_load(body.replace('\\"', '"'))]
        if f"@{PLUGIN_KEY}" in entries:
            found.append(entries)
    return found


def _heredoc_profile_lists(script: str) -> List[List[str]]:
    """``allowedTools`` of every profile ``run.sh`` writes that names the server."""
    found = []
    for document in re.findall(r"<<'MD'\n---\n(.*?)\n---\n", script, flags=re.DOTALL):
        allowed = yaml.safe_load(document).get("allowedTools")
        if allowed and f"@{PLUGIN_KEY}" in allowed:
            found.append(list(allowed))
    return found


def _documented_opt_in_lists() -> Dict[str, List[str]]:
    doc = AGENT_PLUGINS_DOC.read_text()
    readme = (DOGFOOD_DIR / "README.md").read_text()
    script = (DOGFOOD_DIR / "run.sh").read_text()
    sources = {
        "docs/agent-plugins.md yaml": _yaml_block_lists(doc),
        "docs/agent-plugins.md inline": _inline_lists(doc),
        "dogfood README.md inline": _inline_lists(readme),
        "dogfood run.sh profile": _heredoc_profile_lists(script),
        "dogfood run.sh inline": _inline_lists(script),
    }
    return {
        f"{source} #{index}": entries
        for source, lists in sources.items()
        for index, entries in enumerate(lists)
    }


DOCUMENTED = _documented_opt_in_lists()


class TestTheOperatorPluginInsideCao:
    """P2-1: the plugin's key is ``cao-ops``; the gate asks for the fixed selector."""

    def test_the_plugin_declares_the_server_under_a_key_that_is_not_the_selector(self):
        """The premise. If this ever fails the two names agree and the docs can shrink."""
        assert PLUGIN_KEY == "cao-ops"
        assert f"@{PLUGIN_KEY}" != GRANT

    def test_naming_only_the_plugin_key_is_refused(self):
        """What the docs used to tell an operator to write."""
        profile = _profile(key=None, allowed=["@builtin", "fs_read", f"@{PLUGIN_KEY}"])
        recorded = _recorded(profile)
        assert recorded == ["@builtin", "fs_read", f"@{PLUGIN_KEY}"]
        _assert_refused(_denial(recorded))

    def test_that_profile_meets_the_refusal_and_the_fail_safe_through_real_dispatch(
        self, monkeypatch
    ):
        """As the agent meets it: the documented refusal text, and the kill switch."""
        halted: List[str] = []
        monkeypatch.setattr(
            decision_operator, "off", lambda *a, **k: halted.append("off") or {"success": True}
        )
        recorded = _recorded(_profile(key=None, allowed=["@builtin", "fs_read", f"@{PLUGIN_KEY}"]))

        async def call(tool: str) -> Optional[str]:
            async with Client(ops_server.mcp) as client:
                try:
                    await client.call_tool(tool, {})
                except ToolError as exc:
                    return str(exc)
                return None

        with (
            patch.dict(os.environ, BOUND),
            patch.object(mcp_utils, "get_json", return_value=_row(recorded)),
        ):
            refused = asyncio.run(call("list_sessions"))
            fail_safe = asyncio.run(call(FAIL_SAFE_TOOL))

        assert refused == (
            "'list_sessions' is not permitted: the calling terminal's allowed tools "
            f"do not include '{GRANT}'"
        )
        assert fail_safe is None and halted == ["off"]

    @pytest.mark.parametrize(
        "allowed",
        [
            ["@builtin", "fs_read", f"@{PLUGIN_KEY}", GRANT],
            ["@builtin", "fs_read", f"@{PLUGIN_KEY}*"],
            ["*"],
        ],
        ids=["both-names", "one-glob", "star"],
    )
    def test_a_list_that_covers_both_names_is_allowed_and_still_selects_the_server(self, allowed):
        """Two checks, two names: the provider layer selects by key, the gate by selector."""
        recorded = _recorded(_profile(key=None, allowed=allowed))
        assert recorded == allowed, "an explicit list is recorded as written (#772)"
        assert _denial(recorded) is None
        assert granted_mcp_servers(recorded, [PLUGIN_KEY]) == [PLUGIN_KEY]

    def test_the_docs_name_at_least_one_opt_in_list_in_each_place(self):
        """Without this the parametrized test below could pass on an empty table."""
        sources = {name.rsplit(" #", 1)[0] for name in DOCUMENTED}
        assert {
            "docs/agent-plugins.md yaml",
            "docs/agent-plugins.md inline",
            "dogfood README.md inline",
            "dogfood run.sh profile",
            "dogfood run.sh inline",
        } <= sources, f"an opt-in example moved or changed shape; found only {sorted(sources)}"

    @pytest.mark.parametrize("source", sorted(DOCUMENTED))
    def test_every_documented_opt_in_list_is_accepted_by_the_gate(self, source):
        """A doc that names a grant the gate refuses breaks the "Through CAO" setup.

        The agent ``cao install`` produces runs in a CAO terminal, so its ops
        server is bound and gated. Each documented list must both select the
        plugin's server at the provider layer AND satisfy the gate.
        """
        allowed = DOCUMENTED[source]
        recorded = _recorded(_profile(key=None, allowed=allowed))
        assert granted_mcp_servers(recorded, [PLUGIN_KEY]) == [
            PLUGIN_KEY
        ], f"{source}: {allowed} does not select the plugin's '{PLUGIN_KEY}' server"
        denial = _denial(recorded)
        assert denial is None, (
            f"{source} documents allowedTools {allowed}, which the operations gate "
            f"refuses inside a CAO terminal: {denial}"
        )


class TestARoleDefaultProfile:
    """P2-2: with no explicit list, listing the server grants it (#772)."""

    @pytest.mark.parametrize("role", [None, "developer", "supervisor", "reviewer"])
    def test_listing_the_server_grants_the_control_plane(self, role):
        """True at this head, and what the docs must say. Not a wish: a record.

        If the auto-append ever stops covering this server, this test fails and
        ``docs/decisions.md`` / ``docs/tool-restrictions.md`` / the CHANGELOG
        migration note change with it.
        """
        recorded = _recorded(_profile(role=role))
        assert GRANT in recorded
        assert _denial(recorded) is None

    @pytest.mark.parametrize("role", [None, "supervisor", "reviewer"])
    def test_an_unrecorded_row_resolves_the_same_grant_from_the_profile(self, role):
        """A row with no recorded list is resolved from the profile, not waved through."""
        profile = _profile(role=role)
        with patch.object(agent_profiles, "load_agent_profile", return_value=profile):
            assert _denial(None) is None

    @pytest.mark.parametrize("role", [None, "supervisor", "reviewer"])
    def test_an_explicit_list_without_the_grant_withholds_it(self, role):
        """The documented way to declare the server and keep the controls out."""
        role_only = _recorded(_profile(key=None, role=role))
        pinned = [entry for entry in role_only if entry != "*"] or ["fs_read"]
        recorded = _recorded(_profile(role=role, allowed=pinned))
        assert recorded == pinned and GRANT not in recorded
        _assert_refused(_denial(recorded))

    @pytest.mark.parametrize("role", [None, "supervisor", "reviewer"])
    def test_a_profile_that_does_not_list_the_server_does_not_hold_the_grant(self, role):
        recorded = _recorded(_profile(key=None, role=role))
        assert GRANT not in recorded
        _assert_refused(_denial(recorded))

    @pytest.mark.parametrize("key", [PLUGIN_KEY, "cao-ops-mcp", "ops"])
    def test_the_auto_append_under_another_key_does_not_satisfy_the_gate(self, key):
        """The append is ``@<key>``; the gate reads the selector. Refused, fail-closed."""
        recorded = _recorded(_profile(key=key, role="supervisor"))
        assert f"@{key}" in recorded and GRANT not in recorded
        _assert_refused(_denial(recorded))
