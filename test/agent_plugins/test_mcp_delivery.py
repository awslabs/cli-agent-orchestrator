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
        assert servers["demo-tools"]["command"] == "demo-server"

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
        assert args == ["--root", expected]

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
        assert args == ["${NOT_OURS}"]

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
        opencode_workspace["write_profile"]("worker")

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
