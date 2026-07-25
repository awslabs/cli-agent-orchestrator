"""U9/FR-107: KAS install driven end-to-end through the `cao install` CLI.

Complements the service-level cases in ``test/services/test_install_service.py``
by exercising the real CLI entry point — no ``install_agent`` mock — so the whole
chain (CLI -> install_service -> profile facade -> policy compiler -> atomic
write) is verified against the files on disk.

Traces to FR-107, FR-105, FR-106, NFR-104, BR-U9-1/5/6.
"""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import install
from cli_agent_orchestrator.utils.kiro_policy import KNOWN_KAS_ACTIONS

_SUMMARY_KEYS = {
    "policy_source",
    "unrestricted",
    "visible_tools",
    "excluded_tools",
    "allow_rule_count",
    "deny_rule_count",
}

_SENTINEL_PROMPT = "SENTINEL-E2E-PROMPT-5d19"
_SENTINEL_TOKEN = "SENTINEL-E2E-CREDENTIAL-b83a"


@pytest.fixture
def kas_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Path]:
    """Redirect every install destination into a temp workspace."""
    store = tmp_path / "agent-store"
    context = tmp_path / "agent-context"
    kiro = tmp_path / "kiro"
    for directory in (store, context, kiro):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.LOCAL_AGENT_STORE_DIR", store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context
    )
    monkeypatch.setattr("cli_agent_orchestrator.services.install_service.KIRO_AGENTS_DIR", kiro)
    monkeypatch.setattr("cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", store)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", tmp_path / ".env")
    return {"store": store, "kiro": kiro, "context": context}


def test_deny_only_narrows_profile_installs_through_the_cli(
    kas_workspace: dict[str, Path],
) -> None:
    """FR-107: deny narrows the effective grant end-to-end through the CLI.

    Asserts three things at once, on the bytes that reached disk:
    the effective set is `allowed - denied`; the deny surface is the grant's
    complement (FR-106); and the sidecar agrees with the artifact (FR-105).
    """
    (kas_workspace["store"] / "deny-cli.md").write_text(
        "---\n"
        "name: deny-cli\n"
        "description: Deny narrows through the CLI\n"
        "engine: kas\n"
        "allowedTools: [fs_*, web_fetch]\n"
        "deniedTools: [fs_write]\n"
        "mcpServers:\n"
        "  service:\n"
        "    command: service-mcp\n"
        f"    env:\n      API_TOKEN: {_SENTINEL_TOKEN}\n"
        "---\n"
        f"{_SENTINEL_PROMPT}\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(install, ["deny-cli", "--provider", "kiro_cli"])

    assert result.exit_code == 0, result.output

    artifact_path = kas_workspace["kiro"] / "deny-cli.kas.json"
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))

    effective = {"fs_read", "glob", "grep", "web_fetch"}
    assert set(artifact["tools"]) == effective
    assert "fs_write" not in artifact["tools"]

    permitted = {
        rule.split('Action::"')[1].split('"')[0]
        for rule in artifact["permissions"]["rules"]
        if rule.startswith("permit(")
    }
    forbidden = {
        rule.split('Action::"')[1].split('"')[0]
        for rule in artifact["permissions"]["rules"]
        if rule.startswith("forbid(")
    }
    assert permitted == effective
    assert forbidden == set(KNOWN_KAS_ACTIONS) - effective, "deny surface is the grant's complement"

    # No v2 artifact is produced for a KAS profile.
    assert not (kas_workspace["kiro"] / "deny-cli.json").exists()

    # FR-105: the sidecar records the same compiled policy, redacted (NFR-104).
    summary_raw = (kas_workspace["kiro"] / "deny-cli.kas.summary.json").read_text(encoding="utf-8")
    summary = json.loads(summary_raw)
    assert set(summary) == _SUMMARY_KEYS
    assert summary["visible_tools"] == sorted(effective)
    assert set(summary["excluded_tools"]) == forbidden
    assert summary["allow_rule_count"] == len(permitted)
    assert summary["deny_rule_count"] == len(forbidden)
    assert summary["policy_source"] == "allowedTools"
    assert summary["unrestricted"] is False
    assert _SENTINEL_PROMPT not in summary_raw
    assert _SENTINEL_TOKEN not in summary_raw
    assert "permit(" not in summary_raw and "forbid(" not in summary_raw


def test_role_sourced_profile_installs_through_the_cli(
    kas_workspace: dict[str, Path],
) -> None:
    """FR-107: a role-sourced render (no explicit allowedTools) golden."""
    (kas_workspace["store"] / "role-cli.md").write_text(
        "---\n"
        "name: role-cli\n"
        "description: Role sourced through the CLI\n"
        "engine: kas\n"
        "role: supervisor\n"
        "mcpServers:\n"
        "  cao-mcp-server:\n"
        "    command: fixture-cao-mcp\n"
        "---\n"
        "Role sourced.\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(install, ["role-cli", "--provider", "kiro_cli"])

    assert result.exit_code == 0, result.output

    artifact = json.loads((kas_workspace["kiro"] / "role-cli.kas.json").read_text(encoding="utf-8"))
    # role "supervisor" -> ["@cao-mcp-server", "fs_read", "fs_list"]
    assert artifact["tools"] == [
        "fs_read",
        "glob",
        "grep",
        "mcp::cao-mcp-server::*",
    ]
    assert artifact["permissions"]["includePowers"] is False
    assert "allowedTools" not in artifact
    assert "toolsSettings" not in artifact

    summary = json.loads(
        (kas_workspace["kiro"] / "role-cli.kas.summary.json").read_text(encoding="utf-8")
    )
    assert summary["policy_source"] == "role"
    assert summary["allow_rule_count"] == 4
    assert summary["visible_tools"] == artifact["tools"]


def test_untranslatable_profile_fails_the_cli_and_writes_nothing(
    kas_workspace: dict[str, Path],
) -> None:
    """FR-106/NFR-103 through the CLI: exact-or-refuse, no partial artifact."""
    (kas_workspace["store"] / "bad-cli.md").write_text(
        "---\n"
        "name: bad-cli\n"
        "description: Untranslatable\n"
        "engine: kas\n"
        "allowedTools: [fs_read]\n"
        "toolAliases:\n"
        "  ls: fs_list\n"
        "---\n"
        "Body\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(install, ["bad-cli", "--provider", "kiro_cli"])

    # The install command reports failure on stderr and returns 0 (pre-existing
    # CLI behaviour, unchanged by Phase 1). What matters here is that the
    # diagnostic reaches the operator and nothing was written.
    assert "Error:" in result.output
    assert "unsafe-aliases" in result.output
    assert "installed successfully" not in result.output
    assert list(kas_workspace["kiro"].glob("bad-cli*")) == []
    assert list(kas_workspace["kiro"].glob("*.tmp")) == []
