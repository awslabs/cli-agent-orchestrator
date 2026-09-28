"""R9 — a plugin add or remove must not change an agent's recorded provider.

**Validates: Requirements R9.1, R9.2, R9.3 (spec ``pr584-review-fable``)**

``refresh_installed_agents_for_plugin_mcp`` re-materialises provider configs by
replaying the real ``install_agent`` for every provider artifact that already
exists. That replay is the right mechanism (see the function's own docstring:
editing each provider's config in place would mean a second implementation of
every provider's MCP shape). But ``install_agent`` also *records* the provider it
installed for into the local store copy's frontmatter, and that record is
provenance, not configuration.

The asymmetry that this module pins down: the rewrite is **pre-existing**
(``fb90d894:434``) while the refresh loop is **new in #584**. Before the loop the
rewrite was only ever reached because an operator typed
``cao install <agent> --provider X``. The loop turns that deliberate act into an
automatic side effect of an unrelated plugin operation — so an agent installed
for two providers ends up recorded as whichever leg the loop happened to visit
last. The loop's own comment states the principle it breaks: *"Installing an
agent for a provider the operator never chose would be a side effect, not a
refresh."* It guards against **creating** an artifact for an unchosen provider,
then rewrites the **recorded** provider anyway.

Hence the pair of tests here, which have to be read together:

* ``TestPluginChangePreservesRecordedProvider`` — the automatic caller must not
  re-record (R9.1).
* ``TestExplicitInstallStillRecordsTheProvider`` — the deliberate caller still
  must (R9.2). Without this second test the first one is satisfiable by simply
  deleting the rewrite, which would reintroduce the cross-node placement bug the
  rewrite exists to fix (see the long comment at its site in
  ``install_service``).

Both drive the real ``installer.install`` / ``installer.uninstall`` with
``refresh_agents=True`` — the actual production path from ``installer.py`` — not
``refresh_installed_agents_for_plugin_mcp`` called directly. The defect is that
an unrelated plugin operation has this effect, so the plugin operation is what
must be exercised.
"""

from __future__ import annotations

import json
from pathlib import Path

import frontmatter
import pytest

from cli_agent_orchestrator.agent_plugins.installer import install, uninstall
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.services.install_service import install_agent

from .conftest import MCP_SCHEMA_ID, build_plugin
from .test_mcp_delivery import agent_workspace  # noqa: F401  (fixture import)
from .test_mcp_delivery import opencode_workspace  # noqa: F401  (fixture import)


def _mcp_doc() -> str:
    """A minimal ``mcp.json`` — one stdio server, enough to make the refresh act."""
    return json.dumps(
        {
            "$schema": MCP_SCHEMA_ID,
            "mcpServers": {"demo-tools": {"type": "stdio", "command": "demo-server"}},
        },
        indent=2,
    )


@pytest.fixture
def two_provider_workspace(opencode_workspace, monkeypatch):  # noqa: F811
    """``opencode_workspace`` with the prompt-refresh half of the plugin hook isolated.

    ``installer._refresh_agent_artifacts`` runs two independent best-effort
    refreshes, and only the MCP one reads ``install_service``'s module globals.
    The other, ``skill_injection.refresh_all_cao_managed_agents``, imported
    ``COPILOT_AGENTS_DIR`` and ``AGENT_CONTEXT_DIR`` into its own namespace at
    import time, so the base fixture's patches do not reach it and it would read
    the real home. Point them at the temp tree: these tests must not depend on
    what is installed on the machine running them, in either direction.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.skill_injection.COPILOT_AGENTS_DIR",
        opencode_workspace["tmp_path"] / "copilot",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.skill_injection.AGENT_CONTEXT_DIR",
        opencode_workspace["context_dir"],
    )
    opencode_workspace["store_dir"] = opencode_workspace["tmp_path"] / "agent-store"
    return opencode_workspace


def recorded_provider(workspace, agent: str) -> str | None:
    """The ``provider:`` key as it stands in the local store copy on disk.

    Read from the file rather than from a parsed ``AgentProfile`` so the assertion
    is about the durable record — the thing a later ``resolve_provider()`` on this
    node, or a remote ``_assign_remote``, will actually find.
    """
    path: Path = workspace["store_dir"] / f"{agent}.md"
    return frontmatter.loads(path.read_text(encoding="utf-8")).get("provider")


def _install_plugin(workspace, name: str = "demo") -> None:
    """Install a plugin through the real path, refresh hook and all."""
    source = build_plugin(
        workspace["tmp_path"] / "src" / name, name, skills=["alpha"], mcp_text=_mcp_doc()
    )
    outcome = install(
        PluginSource(kind="path", location=str(source)),
        store=workspace["store"],
        skills_dir=workspace["skills_dir"],
        refresh_agents=True,
    )
    assert outcome.installed, [f.message for f in outcome.report.findings]


def _uninstall_plugin(workspace, name: str = "demo") -> None:
    outcome = uninstall(
        name,
        store=workspace["store"],
        skills_dir=workspace["skills_dir"],
        refresh_agents=True,
    )
    assert outcome.removed


def _install_for_both_providers(workspace, agent: str = "worker") -> None:
    """Install ``agent`` under OpenCode and then Kiro, leaving Kiro recorded.

    The order is load-bearing and is the whole reason this test can see the bug.
    The refresh loop walks ``(kiro_cli, copilot_cli, opencode_cli)`` and the last
    leg it visits wins, so an agent recorded as ``opencode_cli`` would be
    "corrected" to ``opencode_cli`` and the defect would hide. Recording
    ``kiro_cli`` last puts the operator's choice first in the loop's order, where
    a later leg overwrites it.

    Both installs are explicit ``--provider`` acts, so both rewrites here are the
    correct, deliberate behaviour R9.2 preserves. Only what happens *after* this
    setup is at issue.
    """
    workspace["write_profile"](agent)

    assert install_agent(agent, "opencode_cli").success
    assert install_agent(agent, "kiro_cli").success

    # Both artifacts exist, so the refresh loop will visit two legs.
    assert (workspace["kiro_dir"] / f"{agent}.json").is_file()
    assert (workspace["tmp_path"] / "opencode" / "agents" / f"{agent}.md").is_file()
    assert recorded_provider(workspace, agent) == "kiro_cli"


class TestPluginChangePreservesRecordedProvider:
    """R9.1 — the automatic caller re-materialises configs without re-recording."""

    def test_plugin_install_does_not_change_the_recorded_provider(
        self, two_provider_workspace
    ) -> None:
        _install_for_both_providers(two_provider_workspace)

        _install_plugin(two_provider_workspace)

        assert recorded_provider(two_provider_workspace, "worker") == "kiro_cli", (
            "adding a plugin re-recorded the agent's provider; the operator chose "
            "kiro_cli and no plugin operation may overrule that"
        )

    def test_plugin_uninstall_does_not_change_the_recorded_provider(
        self, two_provider_workspace
    ) -> None:
        _install_for_both_providers(two_provider_workspace)
        _install_plugin(two_provider_workspace)

        _uninstall_plugin(two_provider_workspace)

        assert (
            recorded_provider(two_provider_workspace, "worker") == "kiro_cli"
        ), "removing a plugin re-recorded the agent's provider"

    def test_the_refresh_still_delivers_to_both_providers(self, two_provider_workspace) -> None:
        """R9.3 — preserving provenance must not cost the refresh its coverage.

        The fix must not be "stop visiting a leg". Both artifacts have to come out
        of the plugin install carrying the plugin's server, which is the only
        reason the loop exists.
        """
        _install_for_both_providers(two_provider_workspace)

        _install_plugin(two_provider_workspace)

        kiro = json.loads(
            (two_provider_workspace["kiro_dir"] / "worker.json").read_text(encoding="utf-8")
        )
        assert "demo-tools" in kiro.get("mcpServers", {}), sorted(kiro.get("mcpServers", {}))

        opencode = json.loads(two_provider_workspace["opencode_config"].read_text(encoding="utf-8"))
        assert "demo-tools" in opencode.get("mcp", {}), sorted(opencode.get("mcp", {}))


class TestExplicitInstallStillRecordsTheProvider:
    """R9.2 — the pre-existing deliberate rewrite must survive the fix.

    This is the companion that stops R9.1 from being satisfied by deleting the
    rewrite. ``cao install <agent> --provider X`` must still record ``X``: without
    it ``resolve_provider()`` finds no frontmatter key and falls back to the
    caller's provider or ``DEFAULT_PROVIDER``, which breaks cross-node placement
    on any node whose installed provider is not the default.
    """

    def test_explicit_install_under_another_provider_rewrites_the_record(
        self, two_provider_workspace
    ) -> None:
        _install_for_both_providers(two_provider_workspace)
        assert recorded_provider(two_provider_workspace, "worker") == "kiro_cli"

        assert install_agent("worker", "opencode_cli").success

        assert recorded_provider(two_provider_workspace, "worker") == "opencode_cli"

    def test_explicit_install_records_the_provider_from_scratch(
        self, two_provider_workspace
    ) -> None:
        """The frontmatter-less profile case — the record is created, not just changed."""
        two_provider_workspace["write_profile"]("fresh")
        assert recorded_provider(two_provider_workspace, "fresh") is None

        assert install_agent("fresh", "opencode_cli").success

        assert recorded_provider(two_provider_workspace, "fresh") == "opencode_cli"
