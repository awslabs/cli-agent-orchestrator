"""W11 delivery seam — installed plugins' MCP servers reach provider configs.

**Validates: Requirements 18.1, 18.2, 18.3, 18.4; Property P9**

``test_mcp_mapping.py`` covers the mapper in isolation: given a document, does it
produce the right entry. This module covers the question the adoption audit found
unanswered (R1): does that entry ever reach a provider? The distinction matters
because every one of the mapper's guarantees — single-pass expansion, the literal
``${...}`` left alone, the pre-expanded marker — is only worth anything if
something downstream consumes the output, and for a while nothing did.

So the load-bearing test here is
``TestTheRealSeam::test_a_plugins_server_reaches_the_kiro_agent_json``: it installs
a real plugin, runs the real ``install_agent``, and reads the JSON Kiro will load.
No hand-built dict, no re-implemented comprehension.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import mcp_delivery
from cli_agent_orchestrator.agent_plugins.installer import install, uninstall
from cli_agent_orchestrator.agent_plugins.mcp_mapping import PRE_EXPANDED_KEY
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

from .conftest import MCP_SCHEMA_ID, build_plugin

STDIO = "stdio"


def mcp_doc(**servers) -> str:
    """An ``mcp.json`` document declaring ``servers``."""
    return json.dumps({"$schema": MCP_SCHEMA_ID, "mcpServers": servers}, indent=2)


def stdio(command: str = "demo-server", **extra) -> dict:
    return {"type": STDIO, "command": command, **extra}


@pytest.fixture
def agent_workspace(tmp_path, monkeypatch):
    """A temp CAO home wired into ``install_service``, plus a plugin store.

    Mirrors ``test/services/test_install_service.py::install_workspace`` — the
    established way to drive ``install_agent`` without touching a real home — and
    adds the agent-plugin store so both halves of the seam are isolated.
    """
    local_store_dir = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    kiro_dir = tmp_path / "kiro"
    copilot_dir = tmp_path / "copilot"
    skills_dir = tmp_path / "skills"
    for directory in (local_store_dir, context_dir, kiro_dir, copilot_dir, skills_dir):
        directory.mkdir()

    for target, value in (
        ("cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store_dir),
        ("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store_dir),
        ("cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir),
        ("cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro_dir),
        ("cli_agent_orchestrator.services.install_service.COPILOT_AGENTS_DIR", copilot_dir),
        ("cli_agent_orchestrator.services.install_service.SKILLS_DIR", skills_dir),
        ("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir),
    ):
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )

    store = InstalledPluginStore(
        plugins_dir=tmp_path / "agent-plugins",
        data_dir=tmp_path / "agent-plugin-data",
    )
    # `install_agent` constructs its own store from the real constants, so the
    # module-level default has to point at the temp one for the seam to be
    # exercised rather than bypassed.
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", store.plugins_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", store.data_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.validation.AGENT_PLUGIN_DATA_DIR",
        store.data_dir,
        raising=False,
    )

    def write_profile(name: str, *, frontmatter: str = "", prompt: str = "Prompt.") -> Path:
        path = local_store_dir / f"{name}.md"
        body = f"name: {name}\ndescription: Test agent\n{frontmatter}"
        path.write_text(f"---\n{body}---\n{prompt}\n", encoding="utf-8")
        return path

    return {
        "tmp_path": tmp_path,
        "store": store,
        "skills_dir": skills_dir,
        "kiro_dir": kiro_dir,
        "context_dir": context_dir,
        "write_profile": write_profile,
    }


#: Frontmatter that NAMES the plugin server in ``allowedTools``.
#:
#: Required by every test below whose subject is the per-agent GRANT — OpenCode's
#: ``agent.<id>.tools`` map is that provider's ``@<server>`` grant, and a
#: plugin-provided server is no longer auto-granted to a profile that did not ask
#: for it (issue #573 AC7, R1.1/R1.2). These tests are about whether CAO's grant
#: is merged rather than clobbering, and whether it is withdrawn again; naming the
#: server is how they keep a grant to observe. The absence case is owned by
#: ``test_no_auto_grant.py``.
NAMES_DEMO_TOOLS = 'allowedTools:\n  - "@demo-tools"\n'


def install_plugin(workspace, name: str, *, mcp: str | None = None, skills=("alpha",)) -> None:
    """Install a plugin into the workspace's store."""
    source = build_plugin(
        workspace["tmp_path"] / "src" / name, name, skills=list(skills), mcp_text=mcp
    )
    outcome = install(
        PluginSource(kind="path", location=str(source)),
        store=workspace["store"],
        skills_dir=workspace["skills_dir"],
        refresh_agents=False,
    )
    assert outcome.installed, [f.message for f in outcome.report.findings]


def kiro_agent_json(workspace, agent: str) -> dict:
    return json.loads((workspace["kiro_dir"] / f"{agent}.json").read_text(encoding="utf-8"))


class TestTheRealSeam:
    """Requirement 18.4 — driven through ``install_agent``, not around it."""

    def test_a_plugins_server_reaches_the_kiro_agent_json(self, agent_workspace):
        """The whole point: a plugin's declared server is configured for a provider.

        Before this seam existed the mapping was computed, reported, and dropped —
        so this assertion is the one that distinguishes "MCP mapping implemented"
        from "plugin MCP servers work".
        """
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(
            agent_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        agent_workspace["write_profile"]("worker")

        result = install_agent("worker", "kiro_cli")
        assert result.success, result.message

        servers = kiro_agent_json(agent_workspace, "worker")["mcpServers"]
        assert "demo-tools" in servers, f"plugin server not delivered: {sorted(servers)}"
        # CONTRACT UPDATED by review 5222539218 (item 4): Kiro's MCP format has no
        # working-directory field, so the plugin's `cwd` is carried by a /bin/sh
        # wrapper and the real command moves into the shim's argv. Still delivered,
        # still the same executable -- just no longer at args[0]'s position.
        entry = servers["demo-tools"]
        assert entry["command"] == "/bin/sh"
        assert "demo-server" in entry["args"]
        assert "cwd" not in entry

    def test_the_plugin_root_placeholder_arrives_expanded(self, agent_workspace):
        """§9.2 expansion survives into the provider's own config file."""
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(
            agent_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")

        args = kiro_agent_json(agent_workspace, "worker")["mcpServers"]["demo-tools"]["args"]
        expected = str(agent_workspace["store"].plugin_root("demo"))
        # Kiro is a shim provider (item 4), so the server's own argv is the TAIL of
        # the shim's. What this test is about -- that §9.2 expansion survives all
        # the way into the provider's config file -- is asserted on that tail.
        assert args[-2:] == ["--root", expected], args
        assert args[0] == "-c" and args[3] == expected, args

    def test_an_unrelated_placeholder_stays_literal_in_the_provider_config(self, agent_workspace):
        """The reason the marker exists, asserted at the far end of the pipeline.

        CAO's ``resolve_env_vars`` pass would happily expand ``${NOT_OURS}``; §9.2
        forbids a client performing any expansion beyond the two placeholders. The
        marker is what makes that pass skip the entry, so this test failing means
        either the marker was dropped or the merge happened after the pass.
        """
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(
            agent_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["${NOT_OURS}"])}),
        )
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")

        args = kiro_agent_json(agent_workspace, "worker")["mcpServers"]["demo-tools"]["args"]
        # Shimmed (item 4), so the literal must survive as the shim's last argument.
        assert args[-1] == "${NOT_OURS}", args

    def test_the_internal_marker_never_reaches_the_provider_config(self, agent_workspace):
        """``x-cao-pre-expanded`` is CAO bookkeeping and not part of any format."""
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")

        raw = (agent_workspace["kiro_dir"] / "worker.json").read_text(encoding="utf-8")
        assert PRE_EXPANDED_KEY not in raw

    def test_cao_supplies_both_env_paths(self, agent_workspace):
        """§9.1 — the plugin gets PLUGIN_ROOT and PLUGIN_DATA in its environment."""
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")

        env = kiro_agent_json(agent_workspace, "worker")["mcpServers"]["demo-tools"]["env"]
        assert env["PLUGIN_ROOT"] == str(agent_workspace["store"].plugin_root("demo"))
        assert env["PLUGIN_DATA"] == str(agent_workspace["store"].plugin_data_dir("demo"))

    def test_a_plugin_without_mcp_json_adds_nothing(self, agent_workspace):
        """§6.2 — an absent ``mcp.json`` is not an error and not a server."""
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(agent_workspace, "demo", mcp=None)
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")

        agent = kiro_agent_json(agent_workspace, "worker")
        assert not agent.get("mcpServers")

    def test_an_unusable_mcp_json_delivers_nothing_and_still_installs_the_agent(
        self, agent_workspace
    ):
        """§7.2.2.2 — MCP off for that plugin; everything else unaffected."""
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(agent_workspace, "demo", mcp="{not json")
        agent_workspace["write_profile"]("worker")

        result = install_agent("worker", "kiro_cli")

        assert result.success, result.message
        assert not kiro_agent_json(agent_workspace, "worker").get("mcpServers")
        # The skill still arrived: an unusable mcp.json is not a plugin failure.
        assert (agent_workspace["skills_dir"] / "alpha").exists()


class TestProfileServersAlwaysWin:
    """Collision rule 1 — the operator's own declaration is never replaced."""

    def test_a_profile_server_of_the_same_name_is_untouched(self, agent_workspace):
        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio("theirs")}))

        merged, delivery = mcp_delivery.merge_plugin_mcp_servers(
            {"demo-tools": {"type": STDIO, "command": "mine"}},
            store=agent_workspace["store"],
        )

        assert merged["demo-tools"]["command"] == "mine"
        assert [f.code for f in delivery.findings] == ["mcp_delivery.profile_collision"]

    def test_the_loser_is_reported_by_plugin_and_server_name(self, agent_workspace):
        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"shared": stdio()}))

        _, delivery = mcp_delivery.merge_plugin_mcp_servers(
            {"shared": {"type": STDIO, "command": "mine"}},
            store=agent_workspace["store"],
        )

        message = delivery.findings[0].message
        assert "shared" in message and "demo" in message

    def test_a_non_colliding_plugin_server_is_still_delivered(self, agent_workspace):
        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"theirs": stdio()}))

        merged, _ = mcp_delivery.merge_plugin_mcp_servers(
            {"mine": {"type": STDIO, "command": "mine"}},
            store=agent_workspace["store"],
        )

        assert sorted(merged) == ["mine", "theirs"]


class TestPluginVersusPluginCollision:
    """Collision rule 2 — lexicographically smallest plugin name, as with skills."""

    def test_the_smallest_plugin_name_wins(self, agent_workspace):
        install_plugin(agent_workspace, "zeta", mcp=mcp_doc(**{"shared": stdio("from-zeta")}))
        install_plugin(agent_workspace, "alpha", mcp=mcp_doc(**{"shared": stdio("from-alpha")}))

        delivery = mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"])

        assert delivery.servers["shared"]["command"] == "from-alpha"
        assert delivery.owners["shared"] == "alpha"

    def test_the_winner_does_not_depend_on_install_order(self, agent_workspace):
        """P8's argument, applied to MCP: order in, same set out."""
        install_plugin(agent_workspace, "alpha", mcp=mcp_doc(**{"shared": stdio("from-alpha")}))
        install_plugin(agent_workspace, "zeta", mcp=mcp_doc(**{"shared": stdio("from-zeta")}))

        delivery = mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"])

        assert delivery.owners["shared"] == "alpha"

    def test_the_loser_is_reported_never_silently_dropped(self, agent_workspace):
        install_plugin(agent_workspace, "alpha", mcp=mcp_doc(**{"shared": stdio()}))
        install_plugin(agent_workspace, "zeta", mcp=mcp_doc(**{"shared": stdio()}))

        delivery = mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"])

        collisions = [f for f in delivery.findings if f.code == "mcp_delivery.plugin_collision"]
        assert len(collisions) == 1
        assert "zeta" in collisions[0].message and "alpha" in collisions[0].message


class TestProviderTransportNarrowing:
    """Requirement 18.7 at the delivery layer, not just the mapper."""

    def test_opencode_does_not_receive_an_http_server(self, agent_workspace):
        """``translate_mcp_server_config`` would flatten a url entry to an empty
        command, so OpenCode must never be handed one."""
        install_plugin(
            agent_workspace,
            "demo",
            mcp=mcp_doc(remote={"type": "streamable-http", "url": "https://example.test/mcp"}),
        )

        for_opencode = mcp_delivery.collect_plugin_mcp_servers(
            provider="opencode_cli", store=agent_workspace["store"]
        )
        for_kiro = mcp_delivery.collect_plugin_mcp_servers(
            provider="kiro_cli", store=agent_workspace["store"]
        )

        assert for_opencode.servers == {}
        assert "remote" in for_kiro.servers

    def test_the_skip_is_reported_for_the_narrowed_provider(self, agent_workspace):
        install_plugin(
            agent_workspace,
            "demo",
            mcp=mcp_doc(remote={"type": "sse", "url": "https://example.test/mcp"}),
        )

        delivery = mcp_delivery.collect_plugin_mcp_servers(
            provider="opencode_cli", store=agent_workspace["store"]
        )

        assert [f.code for f in delivery.findings] == ["mcp.transport_unsupported"]


class TestRemovalWithdrawsTheServer:
    """The uninstall half — a removed plugin's servers must not linger."""

    def test_uninstall_removes_the_server_from_delivery(self, agent_workspace):
        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        assert (
            "demo-tools"
            in mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"]).servers
        )

        uninstall(
            "demo",
            store=agent_workspace["store"],
            skills_dir=agent_workspace["skills_dir"],
            refresh_agents=False,
        )

        assert mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"]).servers == {}

    def test_the_refresh_rewrites_an_existing_agent_after_uninstall(self, agent_workspace):
        """Provider MCP config is baked at install time, so removal must rewrite it.

        Without ``refresh_installed_agents_for_plugin_mcp`` the agent JSON would
        keep a server pointing at a ``PLUGIN_ROOT`` that no longer exists — the
        failure mode is a provider that starts, tries to spawn a missing binary,
        and reports a tool error the operator cannot trace back to the plugin.
        """
        from cli_agent_orchestrator.services.install_service import (
            install_agent,
            refresh_installed_agents_for_plugin_mcp,
        )

        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")
        assert "demo-tools" in kiro_agent_json(agent_workspace, "worker")["mcpServers"]

        uninstall(
            "demo",
            store=agent_workspace["store"],
            skills_dir=agent_workspace["skills_dir"],
            refresh_agents=False,
        )
        refreshed = refresh_installed_agents_for_plugin_mcp()

        assert "worker" in refreshed
        assert not kiro_agent_json(agent_workspace, "worker").get("mcpServers")

    def test_the_refresh_adds_a_server_to_an_agent_installed_earlier(self, agent_workspace):
        """The symmetric case: plugin installed *after* the agent."""
        from cli_agent_orchestrator.services.install_service import (
            install_agent,
            refresh_installed_agents_for_plugin_mcp,
        )

        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")
        assert not kiro_agent_json(agent_workspace, "worker").get("mcpServers")

        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        refresh_installed_agents_for_plugin_mcp()

        assert "demo-tools" in kiro_agent_json(agent_workspace, "worker")["mcpServers"]

    def test_the_refresh_only_touches_providers_that_are_installed(self, agent_workspace):
        """Refreshing must not install an agent for a provider never chosen."""
        from cli_agent_orchestrator.services.install_service import (
            install_agent,
            refresh_installed_agents_for_plugin_mcp,
        )

        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")
        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))

        refresh_installed_agents_for_plugin_mcp()

        copilot_dir = agent_workspace["tmp_path"] / "copilot"
        assert list(copilot_dir.iterdir()) == []


class TestTotality:
    """Delivery is on the agent-install path, so it may never raise."""

    def test_an_empty_store_delivers_nothing_without_error(self, agent_workspace):
        delivery = mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"])
        assert delivery.servers == {} and delivery.findings == ()

    def test_a_missing_store_directory_is_not_an_error(self, tmp_path):
        store = InstalledPluginStore(plugins_dir=tmp_path / "nope", data_dir=tmp_path / "nodata")
        assert mcp_delivery.collect_plugin_mcp_servers(store=store).servers == {}

    def test_a_plugin_whose_root_vanished_is_reported_not_raised(self, agent_workspace):
        """Record present, tree gone — the operator deleted it by hand."""
        import shutil

        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        shutil.rmtree(agent_workspace["store"].plugin_root("demo"))

        delivery = mcp_delivery.collect_plugin_mcp_servers(store=agent_workspace["store"])

        assert delivery.servers == {}

    def test_no_profile_servers_and_no_plugins_stays_none(self, agent_workspace):
        """The "no MCP at all" shape providers already handle is preserved."""
        merged, _ = mcp_delivery.merge_plugin_mcp_servers(None, store=agent_workspace["store"])
        assert merged is None


# ---------------------------------------------------------------------------
# design.md §10a — OpenCode's shared opencode.json is edited in place
# ---------------------------------------------------------------------------
#
# The tests above drive Kiro, whose per-agent JSON is rewritten wholesale. The
# OpenCode branch is the one that carries the two §10a obligations Kiro/Copilot
# do not: it must not clobber a user's shared-config entry (Finding 2), and it
# must disable — not orphan — a withdrawn plugin server (Finding 1). Both act
# ONLY on plugin-derived servers, so both need a real installed plugin, which is
# why they live here beside the delivery seam rather than in
# ``test/cli/commands/test_install_opencode.py`` (whose fixtures never install a
# plugin).


@pytest.fixture
def opencode_workspace(agent_workspace, monkeypatch):
    """``agent_workspace`` plus OpenCode's shared install target.

    The base fixture wires Kiro/Copilot and the plugin store; the OpenCode agents
    dir and shared ``opencode.json`` are layered on here so one test can exercise
    the in-place-edit provider against the same installed plugin set.
    ``install_agent`` reads these module globals, so they must point at the temp
    tree, and the skills-symlink side effect is stubbed (its own behaviour is
    covered by ``test/utils/test_opencode_config.py``).
    """
    opencode_root = agent_workspace["tmp_path"] / "opencode"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR",
        opencode_root / "agents",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE",
        opencode_root / "opencode.json",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.ensure_skills_symlink", lambda: None
    )
    agent_workspace["opencode_config"] = opencode_root / "opencode.json"
    return agent_workspace


def opencode_json(workspace) -> dict:
    return json.loads(workspace["opencode_config"].read_text(encoding="utf-8"))


def _finding_spy(monkeypatch):
    """Collect every ``Finding`` ``install_service`` logs, and return the list.

    ``install_agent`` logs the merge-level delivery result, and
    ``_materialize_opencode_mcp`` logs its own collision findings, both through
    the same module-global ``log_delivery_findings``. Spying on that name is the
    only way to assert a finding's *code* — the collisions are consumed by
    logging, not returned — while still calling through so nothing is suppressed.
    """
    from cli_agent_orchestrator.services import install_service

    captured: list = []
    real = install_service.log_delivery_findings

    def spy(delivery, *, agent_name):
        captured.extend(delivery.findings)
        return real(delivery, agent_name=agent_name)

    monkeypatch.setattr(install_service, "log_delivery_findings", spy)
    return captured


class TestOpencodeConfigClobberGuard:
    """Finding 2 — a plugin server must never overwrite a user's opencode.json entry."""

    def _seed_user_entry(self, workspace, *, name: str) -> dict:
        """Write a user-authored ``opencode.json`` entry CAO has no basis to claim.

        Its command resolves *outside* the plugin store and it is enabled, so
        ``is_cao_owned_mcp_entry`` cannot treat it as CAO's on any of its rules.
        """
        entry = {"type": "local", "command": ["/usr/local/bin/user-thing"], "enabled": True}
        config = workspace["opencode_config"]
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "mcp": {name: entry},
                    "tools": {f"{name}*": False},
                }
            ),
            encoding="utf-8",
        )
        return entry

    def test_a_plugin_server_colliding_with_a_user_entry_is_dropped_not_written(
        self, opencode_workspace, monkeypatch
    ):
        from cli_agent_orchestrator.services import install_service

        original = self._seed_user_entry(opencode_workspace, name="shared-tools")
        # A plugin declares a server of the SAME NAME as the user's hand-written one.
        install_plugin(
            opencode_workspace,
            "demo",
            mcp=mcp_doc(**{"shared-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        opencode_workspace["write_profile"]("worker")

        captured = _finding_spy(monkeypatch)

        result = install_service.install_agent("worker", "opencode_cli")
        assert result.success, result.message

        data = opencode_json(opencode_workspace)
        # (1) The user's entry is byte-for-byte intact — not overwritten.
        assert data["mcp"]["shared-tools"] == original
        # (2) A collision finding of the specific code was emitted.
        assert any(f.code == "mcp_delivery.opencode_config_collision" for f in captured), [
            f.code for f in captured
        ]
        # (3) The agent is NOT granted a tool alias for a server CAO did not write.
        agent_tools = data.get("agent", {}).get("worker", {}).get("tools", {})
        assert "shared-tools*" not in agent_tools


class TestOpencodeConfigIdempotentReplay:
    """§10a — installing twice with the plugin still present must not self-collide."""

    def test_installing_twice_rewrites_the_cao_entry_without_a_collision(
        self, opencode_workspace, monkeypatch
    ):
        from cli_agent_orchestrator.services import install_service

        install_plugin(
            opencode_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)

        # First install writes the plugin server as CAO's own entry.
        assert install_service.install_agent("worker", "opencode_cli").success
        first = opencode_json(opencode_workspace)
        assert first["mcp"]["demo-tools"]["enabled"] is True
        assert first["agent"]["worker"]["tools"]["demo-tools*"] is True

        # Second install: the entry CAO now finds is byte-equal to what it would
        # write, so ``is_cao_owned_mcp_entry`` must treat it as owned — no collision,
        # and the server stays delivered. Were replay misreported as a collision the
        # server could never be installed a second time.
        captured = _finding_spy(monkeypatch)
        assert install_service.install_agent("worker", "opencode_cli").success

        second = opencode_json(opencode_workspace)
        assert second["mcp"]["demo-tools"]["enabled"] is True
        assert second["agent"]["worker"]["tools"]["demo-tools*"] is True
        assert not any(f.code == "mcp_delivery.opencode_config_collision" for f in captured), [
            f.code for f in captured
        ]


class TestOpencodeRemovalIsDisableNotDelete:
    """Finding 1 — the asymmetry is the point.

    OpenCode's shared config is edited in place, so a withdrawn plugin server is
    set ``enabled: false`` (deleting it risks a key CAO may not own; leaving it
    ``true`` spawns a binary whose ``PLUGIN_ROOT`` removal just deleted). Kiro and
    Copilot rewrite their per-agent files wholesale, so the same withdrawal leaves
    the server simply absent. Both sides are asserted here.
    """

    def test_withdrawn_server_disabled_in_opencode_but_absent_from_kiro_and_copilot(
        self, opencode_workspace
    ):
        from cli_agent_orchestrator.services.install_service import (
            install_agent,
            refresh_installed_agents_for_plugin_mcp,
        )

        install_plugin(
            opencode_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        opencode_workspace["write_profile"]("worker")

        # Install the SAME agent for all three providers so each artifact exists
        # and the refresh re-materializes every one.
        for provider in ("opencode_cli", "kiro_cli", "copilot_cli"):
            assert install_agent("worker", provider).success, provider

        # Precondition: the plugin server reached the two persisted-config providers.
        assert opencode_json(opencode_workspace)["mcp"]["demo-tools"]["enabled"] is True
        assert "demo-tools" in kiro_agent_json(opencode_workspace, "worker")["mcpServers"]

        # Withdraw the plugin, then run the real refresh (both install and uninstall
        # call it in production).
        uninstall(
            "demo",
            store=opencode_workspace["store"],
            skills_dir=opencode_workspace["skills_dir"],
            refresh_agents=False,
        )
        refresh_installed_agents_for_plugin_mcp()

        # OpenCode — edited in place: the entry is RETAINED but disabled, and the
        # per-agent grant is withdrawn.
        opencode_data = opencode_json(opencode_workspace)
        assert opencode_data["mcp"]["demo-tools"]["enabled"] is False
        worker_tools = opencode_data.get("agent", {}).get("worker", {}).get("tools", {})
        assert "demo-tools*" not in worker_tools

        # Kiro — rewritten wholesale: the server is simply ABSENT.
        assert "demo-tools" not in kiro_agent_json(opencode_workspace, "worker").get(
            "mcpServers", {}
        )

        # Copilot — agent.md rewritten wholesale: the server name appears nowhere.
        copilot_md = (opencode_workspace["tmp_path"] / "copilot" / "worker.agent.md").read_text(
            encoding="utf-8"
        )
        assert "demo-tools" not in copilot_md


class TestOpencodeCwdIsEmitted:
    """Reproduced by review 3 on #584 — the mapped ``cwd`` never reached OpenCode.

    The mapper always supplies an absolute, contained working directory for a
    plugin server (defaulting to its ``PLUGIN_ROOT``), and OpenCode's
    ``McpLocalConfig.cwd`` is what its spawn honours — so dropping it in
    translation silently started the server in OpenCode's workspace directory.
    Asserted on the real emitted ``opencode.json``, not on the translator alone.
    """

    def test_the_default_plugin_root_cwd_reaches_opencode_json(self, opencode_workspace):
        """An ``mcp.json`` with no ``cwd`` still lands one: the plugin root."""
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"]("worker")
        install_plugin(opencode_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        install_agent("worker", "opencode_cli")

        entry = opencode_json(opencode_workspace)["mcp"]["demo-tools"]
        expected = opencode_workspace["store"].plugin_root("demo")
        assert Path(entry["cwd"]).resolve() == expected.resolve()

    def test_an_explicit_cwd_reaches_opencode_json(self, opencode_workspace):
        """A declared ``${PLUGIN_ROOT}``-relative ``cwd`` is expanded and emitted."""
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"]("worker")
        source = build_plugin(
            opencode_workspace["tmp_path"] / "src" / "demo",
            "demo",
            skills=["alpha"],
            mcp_text=mcp_doc(**{"demo-tools": stdio(cwd="${PLUGIN_ROOT}/work")}),
        )
        (source / "work").mkdir()
        outcome = install(
            PluginSource(kind="path", location=str(source)),
            store=opencode_workspace["store"],
            skills_dir=opencode_workspace["skills_dir"],
            refresh_agents=False,
        )
        assert outcome.installed, [f.message for f in outcome.report.findings]

        install_agent("worker", "opencode_cli")

        entry = opencode_json(opencode_workspace)["mcp"]["demo-tools"]
        expected = opencode_workspace["store"].plugin_root("demo") / "work"
        # Resolved on both sides: a tmp path can itself be a symlink.
        assert Path(entry["cwd"]).resolve() == expected.resolve()

    def test_a_pre_fix_entry_without_cwd_is_upgraded_not_reported(
        self, opencode_workspace, monkeypatch
    ):
        """The upgrade path: an entry written before this fix gains its ``cwd``.

        Such an entry is not byte-equal to what CAO writes now, so ownership has to
        hold through the in-store ``environment`` clause of
        ``is_cao_owned_mcp_entry``. If it did not, every pre-fix plugin entry would
        be reported as a user collision and never gain a ``cwd``.
        """
        from cli_agent_orchestrator.services.install_service import install_agent

        captured = _finding_spy(monkeypatch)
        opencode_workspace["write_profile"]("worker")
        install_plugin(opencode_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))

        plugin_root = opencode_workspace["store"].plugin_root("demo")
        config_file = opencode_workspace["opencode_config"]
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "mcp": {
                        "demo-tools": {
                            "type": "local",
                            "command": ["demo-server"],
                            "enabled": True,
                            # In-store, which is what proves the entry is CAO's.
                            "environment": {"PLUGIN_ROOT": str(plugin_root)},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        install_agent("worker", "opencode_cli")

        entry = opencode_json(opencode_workspace)["mcp"]["demo-tools"]
        assert Path(entry["cwd"]).resolve() == plugin_root.resolve()
        assert "mcp_delivery.opencode_config_collision" not in [f.code for f in captured]


class TestAProviderWithNoMcpPathSaysSo:
    """Reproduced by review 3 on #584: "no MCP path" was indistinguishable from silence.

    Hermes and the mock provider build no MCP configuration whatsoever. Their
    ``PROVIDER_TRANSPORTS`` row used to claim stdio, so a plugin's stdio server was
    reported as *mapped* and then quietly went nowhere. An empty row plus an
    explicit finding is the honest answer, and it is loud because an operator who
    installed a plugin for its tool needs to know the tool is not coming.
    """

    def test_installing_for_hermes_reports_provider_unsupported(self, agent_workspace, monkeypatch):
        from cli_agent_orchestrator.services.install_service import install_agent

        captured = _finding_spy(monkeypatch)
        agent_workspace["write_profile"]("worker")
        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))

        assert install_agent("worker", "hermes").success

        codes = [f.code for f in captured]
        assert "mcp.provider_unsupported" in codes, codes
        # Not reported as a transport problem: no transport would help.
        assert "mcp.transport_unsupported" not in codes

    def test_the_plugins_skills_are_unaffected(self, agent_workspace, monkeypatch):
        """The finding's promise, asserted: only MCP is undeliverable, not skills."""
        from cli_agent_orchestrator.services.install_service import install_agent

        agent_workspace["write_profile"]("worker")
        install_plugin(
            agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}), skills=("alpha",)
        )
        assert install_agent("worker", "hermes").success
        assert (agent_workspace["skills_dir"] / "alpha").exists()


class TestOpencodeAgentEntryIsNeverClobbered:
    """Reproduced by review 3 on #584 — CAO destroyed user state in ``agent.<id>``.

    ``upsert_agent_tools`` replaced the whole ``tools`` map and
    ``remove_agent_tools`` popped the whole ``agent.<id>`` entry, so a user's
    ``model``, ``prompt`` or ``"bash": false`` on a CAO-installed agent was wiped
    on every install, refresh and uninstall. OpenCode's config is shared and
    hand-edited by design (unlike Kiro's and Copilot's per-agent files, which CAO
    rewrites wholesale and legitimately owns), so the same "only touch what CAO
    can prove it owns" rule that governs ``mcp`` entries has to govern the grant.

    Asserted end to end on the real ``opencode.json`` through ``install_agent``
    and the real refresh, not on the helpers alone.
    """

    def test_a_users_agent_keys_survive_an_agent_install(self, opencode_workspace):
        """The install-side clobber: a pre-existing hand-written entry is merged into."""
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)
        install_plugin(opencode_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))

        config_file = opencode_workspace["opencode_config"]
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "agent": {
                        "worker": {
                            "model": "custom/model",
                            "prompt": "stay terse",
                            "tools": {"bash": False, "user*": True},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        assert install_agent("worker", "opencode_cli").success

        entry = opencode_json(opencode_workspace)["agent"]["worker"]
        assert entry["model"] == "custom/model"
        assert entry["prompt"] == "stay terse"
        assert entry["tools"]["bash"] is False
        assert entry["tools"]["user*"] is True
        assert entry["tools"]["demo-tools*"] is True

    def test_the_full_lifecycle_preserves_user_state_and_still_withdraws_the_grant(
        self, opencode_workspace
    ):
        """The reviewer's scenario: install → user edits → reinstall → plugin removed.

        The grant must come and go across the whole lifecycle while the user's
        keys are never touched — the two halves of the finding in one flow, so a
        fix that only merges (and never withdraws) fails here too.
        """
        from cli_agent_orchestrator.services.install_service import (
            install_agent,
            refresh_installed_agents_for_plugin_mcp,
        )

        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)
        install_plugin(
            opencode_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )

        # 1. First install — CAO writes its grant.
        assert install_agent("worker", "opencode_cli").success
        assert opencode_json(opencode_workspace)["agent"]["worker"]["tools"]["demo-tools*"] is True

        # 2. The user edits the entry CAO created, then reinstalls.
        data = opencode_json(opencode_workspace)
        data["agent"]["worker"]["model"] = "custom/model"
        data["agent"]["worker"]["tools"]["bash"] = False
        data["agent"]["worker"]["tools"]["user*"] = True
        opencode_workspace["opencode_config"].write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

        assert install_agent("worker", "opencode_cli").success
        entry = opencode_json(opencode_workspace)["agent"]["worker"]
        assert entry["model"] == "custom/model"
        assert entry["tools"] == {"demo-tools*": True, "bash": False, "user*": True}

        # 3. The plugin goes away — only CAO's own key is withdrawn.
        uninstall(
            "demo",
            store=opencode_workspace["store"],
            skills_dir=opencode_workspace["skills_dir"],
            refresh_agents=False,
        )
        refresh_installed_agents_for_plugin_mcp()

        entry = opencode_json(opencode_workspace)["agent"]["worker"]
        assert "demo-tools*" not in entry["tools"]
        assert entry["model"] == "custom/model"
        assert entry["tools"]["bash"] is False
        assert entry["tools"]["user*"] is True

    def test_one_agents_install_leaves_another_agents_user_keys_alone(self, opencode_workspace):
        """Grants are per-agent; installing ``other`` must not reach into ``worker``."""
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)
        opencode_workspace["write_profile"]("other", frontmatter=NAMES_DEMO_TOOLS)
        install_plugin(opencode_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))

        assert install_agent("worker", "opencode_cli").success
        data = opencode_json(opencode_workspace)
        data["agent"]["worker"]["tools"]["bash"] = False
        opencode_workspace["opencode_config"].write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

        assert install_agent("other", "opencode_cli").success

        agents = opencode_json(opencode_workspace)["agent"]
        assert agents["worker"]["tools"] == {"demo-tools*": True, "bash": False}
        assert agents["other"]["tools"]["demo-tools*"] is True

    def test_a_server_the_profile_stopped_declaring_loses_only_its_grant(self, opencode_workspace):
        """A profile-declared server withdrawn by the user: grant goes, entry stays.

        The `mcp` entry is not CAO's to delete (it is not in the plugin store), so
        only the per-agent grant is withdrawn -- and the user's own `tools` keys
        are not collateral.
        """
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"](
            "worker",
            frontmatter='mcpServers:\n  profile-srv:\n    type: stdio\n    command: "srv"\n',
        )
        assert install_agent("worker", "opencode_cli").success
        assert opencode_json(opencode_workspace)["agent"]["worker"]["tools"]["profile-srv*"] is True

        data = opencode_json(opencode_workspace)
        data["agent"]["worker"]["tools"]["user*"] = True
        opencode_workspace["opencode_config"].write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )

        # The user drops the server from the profile and reinstalls.
        opencode_workspace["write_profile"]("worker")
        assert install_agent("worker", "opencode_cli").success

        result = opencode_json(opencode_workspace)
        assert "profile-srv*" not in result["agent"]["worker"]["tools"]
        assert result["agent"]["worker"]["tools"]["user*"] is True
        # Not a plugin-store entry, so CAO neither deletes nor disables it.
        assert result["mcp"]["profile-srv"]["enabled"] is True

    def test_a_missing_sidecar_still_withdraws_provable_plugin_grants(self, opencode_workspace):
        """The upgrade path: an install predating the sidecar stays cleanable.

        Containment is the second proof, so deleting `cao-grants.json` costs CAO
        the record but not the ability to withdraw a plugin server's grant.
        """
        from cli_agent_orchestrator.services.install_service import (
            install_agent,
            refresh_installed_agents_for_plugin_mcp,
        )

        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)
        install_plugin(
            opencode_workspace,
            "demo",
            mcp=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        assert install_agent("worker", "opencode_cli").success

        data = opencode_json(opencode_workspace)
        data["agent"]["worker"]["tools"]["user*"] = True
        opencode_workspace["opencode_config"].write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        # Simulate a pre-sidecar install.
        opencode_workspace["opencode_config"].with_name("cao-grants.json").unlink()

        uninstall(
            "demo",
            store=opencode_workspace["store"],
            skills_dir=opencode_workspace["skills_dir"],
            refresh_agents=False,
        )
        refresh_installed_agents_for_plugin_mcp()

        tools = opencode_json(opencode_workspace)["agent"]["worker"]["tools"]
        assert "demo-tools*" not in tools
        assert tools["user*"] is True

    def test_a_prepopulated_entry_survives_a_plugin_add_and_a_plugin_remove(
        self, opencode_workspace
    ):
        """Both directions through the real refresh, in one test.

        Added after independent review noted the reviewer's literal complaint --
        "installing/uninstalling a plugin triggers a refresh that clobbers my
        OpenCode agent config" -- was only met by composing two tests. This drives
        the actual production entry point (``install``/``uninstall`` with
        ``refresh_agents=True``, which is what the CLI does) against an entry the
        user wrote *before* CAO ever touched it.
        """
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)

        # The user's own entry, written before any plugin exists.
        config_file = opencode_workspace["opencode_config"]
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "agent": {
                        "worker": {
                            "model": "custom/model",
                            "prompt": "stay terse",
                            "tools": {"bash": False, "user*": True},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        assert install_agent("worker", "opencode_cli").success

        def user_state():
            entry = opencode_json(opencode_workspace)["agent"]["worker"]
            return (
                entry["model"],
                entry["prompt"],
                entry["tools"].get("bash"),
                entry["tools"].get("user*"),
            )

        assert user_state() == ("custom/model", "stay terse", False, True)

        # --- direction 1: plugin ADD triggers the real refresh -----------------
        source = build_plugin(
            opencode_workspace["tmp_path"] / "src" / "demo",
            "demo",
            skills=["alpha"],
            mcp_text=mcp_doc(**{"demo-tools": stdio(args=["--root", "${PLUGIN_ROOT}"])}),
        )
        outcome = install(
            PluginSource(kind="path", location=str(source)),
            store=opencode_workspace["store"],
            skills_dir=opencode_workspace["skills_dir"],
            refresh_agents=True,
        )
        assert outcome.installed, [f.message for f in outcome.report.findings]

        entry = opencode_json(opencode_workspace)["agent"]["worker"]
        assert entry["tools"]["demo-tools*"] is True, "the refresh did not deliver the grant"
        assert user_state() == ("custom/model", "stay terse", False, True)

        # --- direction 2: plugin REMOVE triggers the real refresh --------------
        uninstall(
            "demo",
            store=opencode_workspace["store"],
            skills_dir=opencode_workspace["skills_dir"],
            refresh_agents=True,
        )

        entry = opencode_json(opencode_workspace)["agent"]["worker"]
        assert "demo-tools*" not in entry["tools"], "the grant was not withdrawn"
        assert user_state() == ("custom/model", "stay terse", False, True)

    def test_the_grant_sidecar_is_written_beside_the_opencode_config(self, opencode_workspace):
        """Provenance is recorded, not guessed — that is what makes withdrawal safe.

        Beside ``opencode.json`` rather than in it: OpenCode owns that file's
        schema and its ``mcp`` entries forbid extra properties, so there is no
        consistent in-file place for CAO bookkeeping.
        """
        from cli_agent_orchestrator.services.install_service import install_agent

        opencode_workspace["write_profile"]("worker", frontmatter=NAMES_DEMO_TOOLS)
        install_plugin(opencode_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        assert install_agent("worker", "opencode_cli").success

        sidecar = opencode_workspace["opencode_config"].with_name("cao-grants.json")
        assert sidecar.exists()
        record = json.loads(sidecar.read_text(encoding="utf-8"))
        assert record["version"] == 1
        assert "demo-tools*" in record["agents"]["worker"]


class TestCwdSeam:
    """Reported by review 5222539218 on #584 (item 4).

    The shim is applied at ONE seam -- ``apply_plugin_mcp_servers`` -- rather than in
    each of the seven serializers whose formats have no working-directory field. One
    call site cannot drift, and being upstream of every serializer it is by
    construction ahead of MiniMax's absolute-command rewrite.
    """

    def test_a_shim_provider_receives_a_shimmed_plugin_entry(self, agent_workspace):
        from cli_agent_orchestrator.services.install_service import install_agent

        install_plugin(agent_workspace, "demo", mcp=mcp_doc(**{"demo-tools": stdio()}))
        agent_workspace["write_profile"]("worker")
        install_agent("worker", "kiro_cli")

        entry = kiro_agent_json(agent_workspace, "worker")["mcpServers"]["demo-tools"]
        assert entry["command"] == "/bin/sh"
        assert entry["args"][0] == "-c"
        assert entry["args"][2] == "cao-cwd-shim"
        # $1 is the plugin root the mapper defaulted to.
        assert entry["args"][3] == str(agent_workspace["store"].plugin_root("demo"))
        assert "cwd" not in entry

    def test_a_native_provider_is_not_shimmed(self):
        """OpenCode has ``McpLocalConfig.cwd``, so the seam leaves the entry alone.

        Asserted at the seam rather than through ``install_agent``: the end-to-end
        OpenCode ``cwd`` emission already has its own test
        (``TestOpencodeCwdIsEmitted``), and driving the real installer here would
        reach the operator's actual ``~/.aws/opencode`` through the agents dir and
        the grant helpers, which resolve their paths independently of the fixture's
        patches. It did exactly that once while this test was being written.
        """
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import apply_plugin_mcp_servers

        class _P:
            mcpServers = {
                "demo-tools": {
                    "command": "demo-server",
                    "cwd": "/plugins/demo",
                    PRE_EXPANDED_KEY: True,
                }
            }

        profile = _P()
        apply_plugin_mcp_servers(profile, provider="opencode_cli", normalize_existing=False)
        entry = profile.mcpServers["demo-tools"]
        assert entry["command"] == "demo-server"
        assert entry["cwd"] == "/plugins/demo"
        assert PRE_EXPANDED_KEY not in entry, "the marker must still be stripped"

    def test_a_shim_provider_rewrites_the_same_entry(self):
        """The other half of the table, on identical input."""
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import apply_plugin_mcp_servers

        class _P:
            mcpServers = {
                "demo-tools": {
                    "command": "demo-server",
                    "cwd": "/plugins/demo",
                    PRE_EXPANDED_KEY: True,
                }
            }

        profile = _P()
        apply_plugin_mcp_servers(profile, provider="kiro_cli", normalize_existing=False)
        entry = profile.mcpServers["demo-tools"]
        assert entry["command"] == "/bin/sh"
        assert entry["args"][3] == "/plugins/demo"
        assert entry["args"][4] == "demo-server"
        assert "cwd" not in entry
        assert PRE_EXPANDED_KEY not in entry

    def test_a_profile_authored_entry_is_never_shimmed(self, agent_workspace):
        """A profile's own ``cwd`` is the operator's instruction, not ours to rewrite.

        Rewriting it would be a behaviour change beyond this finding -- the entry
        was authored for a provider the operator chose.
        """
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import apply_plugin_mcp_servers

        class _P:
            mcpServers = {"mine": {"command": "srv", "cwd": "/somewhere"}}

        profile = _P()
        apply_plugin_mcp_servers(profile, provider="kiro_cli", persisted=True)
        assert profile.mcpServers["mine"]["command"] == "srv"
        assert profile.mcpServers["mine"]["cwd"] == "/somewhere"


class TestCwdUnsupportedIsLoud:
    """Reported by review 5222539218 on #584 (item 4)."""

    def test_cwd_unsupported_is_loud(self):
        """An undeliverable working directory is not fixable from the profile."""
        from cli_agent_orchestrator.agent_plugins.mcp_delivery import _LOUD_CODES

        assert "mcp.cwd_unsupported" in _LOUD_CODES
