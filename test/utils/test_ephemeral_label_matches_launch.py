"""Tie ephemeral enforcement labels to the real launch builders (#1099)."""

import shlex
from test.utils.test_agent_profiles_ephemeral import DOCUMENT, NAME, stores  # noqa: F401

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.providers import claude_code, codex
from cli_agent_orchestrator.providers.kiro_capabilities import build_kiro_command
from cli_agent_orchestrator.utils.ephemeral_capabilities import (
    ADVISORY,
    CAPABILITIES,
    enforcement_label,
)
from cli_agent_orchestrator.utils.tool_mapping import get_launch_disallowed_tools


@pytest.mark.parametrize("provider_name", CAPABILITIES)
def test_non_advisory_label_matches_ephemeral_launch(provider_name, stores, monkeypatch, tmp_path):
    tools = ["fs_read", "fs_list", "@cao-mcp-server"]
    document = DOCUMENT.replace("provider: claude_code", f"provider: {provider_name}")
    document = document.replace("---\nTask.", "allowedTools: " + str(tools) + "\n---\nTask.")
    (stores[1] / f"{NAME}.md").write_text(document)

    if provider_name == "claude_code":
        monkeypatch.setattr(claude_code, "CAO_HOME_DIR", tmp_path)
        provider = claude_code.ClaudeCodeProvider(
            "eeeeeeee", "session", "child", NAME, allowed_tools=tools, ephemeral=True
        )
        argv = shlex.split(provider._build_claude_command().split("; ")[-1])
        blocked = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--disallowedTools"]
        assert blocked == get_launch_disallowed_tools("claude_code", tools, ephemeral=True)
        assert blocked
    elif provider_name == "codex":
        monkeypatch.setattr(codex, "CAO_HOME_DIR", tmp_path)
        provider = codex.CodexProvider("eeeeeeee", "session", "child", NAME, allowed_tools=tools)
        argv = shlex.split(provider._build_codex_command())
    elif provider_name == "kiro_cli":
        # The helper path in KiroCliProvider.initialize uses this real builder
        # on v3, with an absolute executable and per-launch home, and yolo=True.
        argv = build_kiro_command(
            KiroEngine.V3,
            NAME,
            yolo=True,
            executable=str(tmp_path / "bin" / "kiro-cli"),
            kiro_home=str(tmp_path / "kiro-home"),
        )
    else:
        pytest.fail(f"Add an ephemeral launch builder for {provider_name}")

    if enforcement_label(provider_name) != ADVISORY:
        # Claude's --dangerously-skip-permissions and Kiro's --trust-all-tools
        # skip prompts but preserve their tool cuts; they are not sandbox bypasses.
        bypasses = {"--yolo", "--dangerously-bypass-approvals-and-sandbox"}
        found = [arg for arg in argv if arg.split("=", 1)[0] in bypasses]
        assert (
            not found
        ), f"{provider_name} ({enforcement_label(provider_name)}) launches with {found}"


def test_codex_remains_advisory_until_its_launch_is_contained():
    assert enforcement_label("codex") == ADVISORY
