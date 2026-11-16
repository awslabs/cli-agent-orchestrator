"""``params`` is CAO-native: no provider's native agent file or agent JSON carries it.

Contract section 1. Each writer below builds its file from named profile fields, so a
declared param never reaches a provider. One test per writer, each with a profile whose
params use marker names and descriptions that appear nowhere else, and each asserting
the markers are absent from everything the writer produced.

Not covered here, by design: the context copy under ``agent-context``. It is CAO's own
copy of the profile file, stored as written, and every frontmatter key is in it.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from unittest.mock import patch

import frontmatter
import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.install_service import install_agent

MARKERS = ("orders_marker_table", "orders-marker-description", "orders_marker_limit")

PARAMS_BLOCK = (
    "params:\n"
    "  orders_marker_table:\n"
    "    description: orders-marker-description\n"
    "    required: true\n"
    "  orders_marker_limit:\n"
    "    type: int\n"
    "    default: 7\n"
)

PARAMS = {
    "orders_marker_table": {"description": "orders-marker-description", "required": True},
    "orders_marker_limit": {"type": "int", "default": 7},
}


def _assert_no_params(text: str, where: str) -> None:
    for marker in MARKERS:
        assert marker not in text, f"{where} carries {marker!r}"


def _assert_tree_has_no_params(root: Path) -> None:
    files = [path for path in root.rglob("*") if path.is_file()]
    assert files, f"nothing was written under {root}"
    for path in files:
        _assert_no_params(path.read_text(encoding="utf-8", errors="replace"), str(path))


def _profile_text(name: str, extra: str = "") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Reads the orders table\n"
        "role: developer\n"
        f"{extra}"
        "mcpServers:\n"
        "  orders-mcp:\n"
        "    command: orders-mcp\n"
        f"{PARAMS_BLOCK}"
        "---\n\nRead the newest order.\n"
    )


@pytest.fixture
def install_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Every install destination under tmp_path, as in ``test_install_service.py``."""
    paths = {
        name: tmp_path / name
        for name in ("agent-store", "agent-context", "kiro", "copilot", "opencode-agents")
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    opencode_config = tmp_path / "opencode" / "opencode.json"
    paths["opencode-config"] = opencode_config

    svc = "cli_agent_orchestrator.services.install_service"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", paths["agent-store"]
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", paths["agent-store"]
    )
    monkeypatch.setattr(f"{svc}.AGENT_CONTEXT_DIR", paths["agent-context"])
    monkeypatch.setattr(f"{svc}.KIRO_AGENTS_DIR", paths["kiro"])
    monkeypatch.setattr(f"{svc}.COPILOT_AGENTS_DIR", paths["copilot"])
    monkeypatch.setattr(f"{svc}.OPENCODE_AGENTS_DIR", paths["opencode-agents"])
    monkeypatch.setattr(f"{svc}.ensure_skills_symlink", lambda: None)
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE", opencode_config
    )
    monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
        lambda: {"cao_installed": str(paths["agent-context"])},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    return paths


def _install(install_paths: dict, name: str, provider: str, extra: str = ""):
    (install_paths["agent-store"] / f"{name}.md").write_text(
        _profile_text(name, extra), encoding="utf-8"
    )
    result = install_agent(name, provider)
    assert result.success is True, result.message
    return result


class TestInstallWriters:
    def test_kiro_cli_agent_json(self, install_paths) -> None:
        _install(install_paths, "orders-kiro", "kiro_cli")

        written = (install_paths["kiro"] / "orders-kiro.json").read_text(encoding="utf-8")

        assert "params" not in json.loads(written)
        _assert_no_params(written, "the Kiro agent JSON")

    def test_kiro_cli_universal_agent_json(self, install_paths) -> None:
        _install(
            install_paths,
            "orders-kiro-v3",
            "kiro_cli",
            extra="engine: v3\nallowedTools: [fs_read]\n",
        )

        written = (install_paths["kiro"] / "orders-kiro-v3.json").read_text(encoding="utf-8")

        assert "excludedTools" in json.loads(written), "the universal file was written"
        assert "params" not in json.loads(written)
        _assert_no_params(written, "the universal Kiro agent JSON")

    def test_copilot_cli_agent_file_and_its_refresh(self, install_paths) -> None:
        from cli_agent_orchestrator.utils.skill_injection import refresh_agent_md_prompt

        _install(install_paths, "orders-copilot", "copilot_cli")
        agent_file = install_paths["copilot"] / "orders-copilot.agent.md"

        assert "params" not in frontmatter.loads(agent_file.read_text()).metadata
        _assert_no_params(agent_file.read_text(), "the Copilot agent file")

        profile = AgentProfile(
            name="orders-copilot",
            description="Reads the orders table",
            system_prompt="Read the newest order.",
            params=PARAMS,
        )
        assert refresh_agent_md_prompt(agent_file, profile) is True
        _assert_no_params(agent_file.read_text(), "the refreshed Copilot agent file")

    def test_opencode_cli_agent_file_and_config(self, install_paths) -> None:
        _install(install_paths, "orders-opencode", "opencode_cli")

        agent_files = list(install_paths["opencode-agents"].glob("*.md"))
        assert len(agent_files) == 1
        assert "params" not in frontmatter.loads(agent_files[0].read_text()).metadata
        _assert_no_params(agent_files[0].read_text(), "the OpenCode agent file")
        config = install_paths["opencode-config"]
        assert config.exists()
        _assert_no_params(config.read_text(), "opencode.json")


class TestLaunchWriters:
    def _profile(self, **fields) -> AgentProfile:
        return AgentProfile(
            name="orders-agent",
            description="Reads the orders table",
            system_prompt="Read the newest order.",
            params=PARAMS,
            **fields,
        )

    def test_kiro_helper_agent_json(self) -> None:
        from cli_agent_orchestrator.providers import kiro_launch_home

        written = kiro_launch_home.render_helper_agent(
            "orders-agent",
            self._profile(),
            kiro_launch_home.kiro_helper_grant(["fs_read"]),
            "0a1b2c3d",
        ).decode("utf-8")

        assert "params" not in json.loads(written)
        _assert_no_params(written, "the Kiro helper agent JSON")

    def test_kimi_launch_agent_file(self) -> None:
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        rendered = KimiCliProvider("term-md", "s", "w")._render_markdown_agent(
            self._profile(tools=["Read"])
        )

        assert rendered is not None
        _assert_no_params(rendered, "the Kimi launch agent file")

    def test_claude_code_full_profile_files_and_command(self, tmp_path) -> None:
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        profile = self._profile(mcpServers={"orders-mcp": {"command": "orders-mcp"}})
        provider = ClaudeCodeProvider("orders01", "sess", "win", "orders-agent")
        with patch("cli_agent_orchestrator.providers.claude_code.CAO_HOME_DIR", tmp_path):
            command = provider._build_claude_command(profile)

        args = shlex.split(command)
        assert "--append-system-prompt-file" in args and "--mcp-config" in args
        _assert_no_params(command, "the Claude Code command")
        _assert_tree_has_no_params(tmp_path)

    def test_claude_code_native_agent_mode(self, tmp_path) -> None:
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        profile = self._profile(native_agent="orders-native")
        provider = ClaudeCodeProvider("orders02", "sess", "win", "orders-agent")
        with patch("cli_agent_orchestrator.providers.claude_code.CAO_HOME_DIR", tmp_path):
            command = provider._build_claude_command(profile)

        assert "--agent orders-native" in command
        _assert_no_params(command, "the Claude Code native-agent command")
        assert not any(path.is_file() for path in tmp_path.rglob("*")), "native mode writes none"
