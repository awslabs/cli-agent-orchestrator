"""Tie ephemeral enforcement labels to the real launch builders (#1099)."""

import shlex
import tomllib
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


def _sandbox_bypasses(argv, config_text):
    bypasses = {"--yolo", "--dangerously-bypass-approvals-and-sandbox"}
    found = [arg for arg in argv if arg.split("=", 1)[0] in bypasses]
    for i, arg in enumerate(argv):
        flag, _, value = arg.partition("=")
        if flag in {"--sandbox", "-s", "-c", "--config"}:
            if "=" not in arg and i + 1 < len(argv):
                value = argv[i + 1]
            if flag in {"-c", "--config"}:
                key, _, value = value.partition("=")
                if key.strip() != "sandbox_mode":
                    continue
            if value.strip().strip("\"'").casefold() == "danger-full-access":
                found.append(f"{flag} {value}")
    if config_text:
        value = tomllib.loads(config_text).get("sandbox_mode", "")
        if isinstance(value, str) and value.casefold() == "danger-full-access":
            found.append("config: sandbox_mode=danger-full-access")
    return found


@pytest.mark.parametrize(
    "argv, config_text, expected",
    [
        (["codex", "--yolo"], "", True),
        (["codex", "--dangerously-bypass-approvals-and-sandbox"], "", True),
        (["codex", "--sandbox", "danger-full-access", "--ask-for-approval", "never"], "", True),
        (["codex", "-s", "danger-full-access"], "", True),
        (["codex", "--sandbox=danger-full-access"], "", True),
        (["codex", "-c", 'sandbox_mode="danger-full-access"'], "", True),
        (["codex", "-c", "sandbox_mode=danger-full-access"], "", True),
        (["codex", "-c", "sandbox_mode='DANGER-FULL-ACCESS'"], "", True),
        (["codex", "--config=sandbox_mode=danger-full-access"], "", True),
        (["codex"], 'sandbox_mode = "danger-full-access"', True),
        (["codex", "--profile", "helper"], 'sandbox_mode = "DANGER-FULL-ACCESS"', True),
        (["claude", "--dangerously-skip-permissions"], "", False),
        (["kiro-cli", "chat", "--trust-all-tools"], "", False),
        (["codex", "--sandbox", "workspace-write"], "", False),
        (["codex", "-c", 'sandbox_mode="workspace-write"'], "", False),
        (["codex"], 'sandbox_mode = "workspace-write"', False),
        (["codex", "-c", 'developer_instructions="danger-full-access"'], "", False),
        (["codex"], '# sandbox_mode = "danger-full-access"', False),
    ],
)
def test_sandbox_bypasses(argv, config_text, expected):
    assert bool(_sandbox_bypasses(argv, config_text)) is expected


@pytest.mark.parametrize("provider_name", CAPABILITIES)
def test_non_advisory_label_matches_ephemeral_launch(provider_name, stores, monkeypatch, tmp_path):
    tools = ["fs_read", "fs_list", "@cao-mcp-server"]
    document = DOCUMENT.replace("provider: claude_code", f"provider: {provider_name}")
    document = document.replace("---\nTask.", "allowedTools: " + str(tools) + "\n---\nTask.")
    (stores[1] / f"{NAME}.md").write_text(document)

    config_bypasses = []
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
        # The label check below binds as soon as Codex stops being advisory.
        monkeypatch.setattr(codex, "CAO_HOME_DIR", tmp_path)
        codex_home = tmp_path / "codex-home"
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
        provider = codex.CodexProvider("eeeeeeee", "session", "child", NAME, allowed_tools=tools)
        argv = shlex.split(provider._build_codex_command())
        # The builder currently emits -c overrides, not config files. A named
        # --profile/-p resolves its own config under CODEX_HOME; argv alone misses it.
        config_paths = [codex_home / "config.toml"]
        for i, arg in enumerate(argv[:-1]):
            if arg in {"--profile", "-p"}:
                config_paths.append(codex_home / f"{argv[i + 1]}.config.toml")
        for path in config_paths:
            if path.is_file():
                config_bypasses.extend(_sandbox_bypasses([], path.read_text()))
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
        found = _sandbox_bypasses(argv, "") + config_bypasses
        assert (
            not found
        ), f"{provider_name} ({enforcement_label(provider_name)}) launches with {found}"


def test_codex_remains_advisory_until_its_launch_is_contained():
    assert enforcement_label("codex") == ADVISORY
