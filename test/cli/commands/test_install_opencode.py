"""Tests for the opencode_cli branch of the install command."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict

import frontmatter
import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.install import install

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def install_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Dict[str, Any]:
    """Redirect all filesystem paths used by the install command to tmp_path."""
    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    opencode_agents = tmp_path / "opencode_cli" / "agents"
    opencode_config = tmp_path / "opencode_cli" / "opencode.json"

    local_store.mkdir(parents=True)
    context_dir.mkdir(parents=True)
    # opencode_agents intentionally NOT pre-created — install must mkdir it.

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR", opencode_agents
    )
    # Redirect the config file used by opencode_config helpers
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE", opencode_config
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
    )
    # Suppress ensure_skills_symlink filesystem side-effects in install unit tests.
    # The symlink helper's own behaviour is covered by test_opencode_config.py.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.ensure_skills_symlink", lambda: None
    )

    return {
        "local_store": local_store,
        "context_dir": context_dir,
        "agents_dir": opencode_agents,
        "config_file": opencode_config,
    }


def _write_profile(
    profile_path: Path,
    *,
    name: str = "test-agent",
    description: str = "Test agent",
    mcp_servers: str = "",
    extra_frontmatter: str = "",
    body: str = "You are a helpful agent.",
) -> None:
    """Write a minimal agent profile .md file."""
    mcp_block = f"mcpServers:\n{mcp_servers}" if mcp_servers else ""
    profile_path.write_text(
        f"---\nname: {name}\ndescription: {description}\n{extra_frontmatter}{mcp_block}\n---\n{body}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Scenario (a): fresh install creates agent .md + fresh opencode.json
# ---------------------------------------------------------------------------


class TestFreshInstall:
    def test_exit_code_zero(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(install_workspace["local_store"] / "test-agent.md")

        result = runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert result.exit_code == 0, result.output

    def test_agent_md_written(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        agent_file = install_workspace["agents_dir"] / "test-agent.md"
        assert agent_file.exists()

    def test_agent_md_has_valid_frontmatter(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            description="A developer agent",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        assert post.metadata["description"] == "A developer agent"
        assert post.metadata["mode"] == "all"
        assert "permission" in post.metadata

    def test_agent_md_has_body(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            body="You are a test sentinel agent.",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        # Body must contain the raw profile.system_prompt — NOT the baked skill catalog.
        assert "You are a test sentinel agent." in post.content
        # Skills are delivered via the native skills/ symlink; the catalog must NOT
        # be baked into the system prompt.
        assert "## Available Skills" not in post.content

    def test_ensure_skills_symlink_called(
        self, runner: CliRunner, install_workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ):
        """ensure_skills_symlink() must be called once per opencode_cli install."""
        calls: list[int] = []
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.ensure_skills_symlink",
            lambda: calls.append(1),
        )
        _write_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert calls, "ensure_skills_symlink() was not called during opencode_cli install"

    def test_no_model_in_frontmatter(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        """model goes via --model at launch time, never in frontmatter."""
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            extra_frontmatter="model: anthropic/claude-sonnet-4-6\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        assert "model" not in post.metadata

    def test_agents_dir_auto_created(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(install_workspace["local_store"] / "test-agent.md")
        assert not install_workspace["agents_dir"].exists()

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert install_workspace["agents_dir"].exists()

    def test_no_opencode_json_without_mcp(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """Scenario (e): agent without MCP servers must not create opencode.json."""
        _write_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert not install_workspace["config_file"].exists()

    def test_success_message_in_output(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(install_workspace["local_store"] / "test-agent.md")

        result = runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert "installed successfully" in result.output
        assert "opencode_cli agent:" in result.output


# ---------------------------------------------------------------------------
# Scenario (b): re-install is idempotent
# ---------------------------------------------------------------------------


class TestIdempotentInstall:
    def test_two_installs_produce_identical_agent_md(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        _write_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])
        first = (install_workspace["agents_dir"] / "test-agent.md").read_bytes()

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])
        second = (install_workspace["agents_dir"] / "test-agent.md").read_bytes()

        assert first == second

    def test_two_installs_produce_identical_opencode_json(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])
        first = install_workspace["config_file"].read_bytes()

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])
        second = install_workspace["config_file"].read_bytes()

        assert first == second


# ---------------------------------------------------------------------------
# Scenario (c): permission frontmatter always emits allow/deny (no ask)
# ---------------------------------------------------------------------------


class TestPermissionTranslation:
    def test_allowed_tools_emit_allow(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            extra_frontmatter="allowedTools:\n  - fs_read\n  - execute_bash\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        perm = post.metadata["permission"]
        assert perm["read"] == "allow"
        assert perm["bash"] == "allow"

    def test_never_emits_ask(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        """CAO owns the permission decision — ``ask`` must never be written."""
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            extra_frontmatter="allowedTools:\n  - fs_read\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        perm = post.metadata["permission"]
        assert "ask" not in perm.values()

    def test_wildcard_allows_all(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            extra_frontmatter="allowedTools:\n  - '*'\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        perm = post.metadata["permission"]
        assert all(v == "allow" for v in perm.values())

    def test_hardcoded_denies_always_present(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """task/question/webfetch/websearch/codesearch are always denied (unless *)."""
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            extra_frontmatter="allowedTools:\n  - '@builtin'\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        perm = post.metadata["permission"]
        for tool in ("task", "question", "webfetch", "websearch", "codesearch"):
            assert perm[tool] == "deny", f"{tool} should always be deny"

    def test_unpermitted_cao_tools_emit_deny(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            extra_frontmatter="allowedTools:\n  - fs_read\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        post = frontmatter.loads((install_workspace["agents_dir"] / "test-agent.md").read_text())
        perm = post.metadata["permission"]
        assert perm["bash"] == "deny"
        assert perm["write"] == "deny"
        assert perm["edit"] == "deny"


# ---------------------------------------------------------------------------
# Scenario (d): MCP servers produce correct opencode.json blocks
# ---------------------------------------------------------------------------


class TestMcpWiring:
    def _mcp_profile(self, profile_path: Path) -> None:
        _write_profile(
            profile_path,
            name="test-agent",
            mcp_servers=("  cao-mcp-server:\n" "    command: cao-mcp-server\n" "    type: local\n"),
        )

    def test_mcp_server_added_to_top_level_mcp(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        self._mcp_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert "cao-mcp-server" in data["mcp"]

    def test_mcp_server_default_denied_in_top_level_tools(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        self._mcp_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert data["tools"]["cao-mcp-server*"] is False

    def test_mcp_server_re_enabled_per_agent(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        self._mcp_profile(install_workspace["local_store"] / "test-agent.md")

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert data["agent"]["test-agent"]["tools"]["cao-mcp-server*"] is True

    def test_multiple_mcp_servers(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            mcp_servers=("  srv-a:\n    command: srv-a\n" "  srv-b:\n    command: srv-b\n"),
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert data["tools"]["srv-a*"] is False
        assert data["tools"]["srv-b*"] is False
        agent_tools = data["agent"]["test-agent"]["tools"]
        assert agent_tools["srv-a*"] is True
        assert agent_tools["srv-b*"] is True


# ---------------------------------------------------------------------------
# Scenario (e): agent without MCP — already covered in TestFreshInstall
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Scenario (f): existing user-authored entries in opencode.json are preserved
# ---------------------------------------------------------------------------


class TestPreserveExistingConfig:
    def test_user_mcp_entry_preserved(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        # Pre-write a config with a user-owned entry
        install_workspace["config_file"].parent.mkdir(parents=True, exist_ok=True)
        install_workspace["config_file"].write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "mcp": {"user-server": {"type": "local", "command": "user-srv"}},
                    "tools": {"user-server*": False},
                }
            ),
            encoding="utf-8",
        )
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert "user-server" in data["mcp"], "user-authored MCP entry must survive"
        assert data["tools"]["user-server*"] is False, "user tools entry must survive"
        assert "cao-mcp-server" in data["mcp"], "new entry must also be present"

    def test_user_agent_entry_preserved(self, runner: CliRunner, install_workspace: Dict[str, Any]):
        install_workspace["config_file"].parent.mkdir(parents=True, exist_ok=True)
        install_workspace["config_file"].write_text(
            json.dumps(
                {
                    "mcp": {"cao-mcp-server": {"command": "cao-mcp-server"}},
                    "tools": {"cao-mcp-server*": False},
                    "agent": {
                        "other-agent": {"tools": {"cao-mcp-server*": True}},
                    },
                }
            ),
            encoding="utf-8",
        )
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert "other-agent" in data["agent"], "pre-existing agent entry must survive"
        assert "test-agent" in data["agent"], "new agent entry must also be present"


# ---------------------------------------------------------------------------
# Scenario: agent ID parity (filename === opencode.json key), and the refusal of
# a namespaced frontmatter name
# ---------------------------------------------------------------------------


class TestAgentIdParity:
    """One sanitized agent ID must be used for both the .md filename and the
    ``agent.<id>.tools`` key in opencode.json, so the value passed to
    ``opencode --agent <id>`` at runtime lines up with its MCP grants."""

    def test_filename_and_config_key_agree(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            name="test-agent",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )

        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert (install_workspace["agents_dir"] / "test-agent.md").exists()
        data = json.loads(install_workspace["config_file"].read_text())
        assert "test-agent" in data["agent"], "agent ID must be the opencode.json key"
        assert data["agent"]["test-agent"]["tools"]["cao-mcp-server*"] is True


class TestNamespacedNameIsRefused:
    """A frontmatter ``name:`` containing a path separator is no longer installable.

    Security fix for GHSA-6m35-gcf5-xm75: the resolved ``name:`` is
    attacker-controlled for URL installs and feeds the context-copy filename, so
    it is now validated as a single path segment. Namespaced names like
    ``my/agent`` are rejected as a consequence — they only ever worked when the
    intermediate context directory happened to already exist, and permitting them
    is exactly what let a hostile ``../..`` name escape.

    Replaces the former ``TestSlashSafeAgentId``, which asserted the now-removed
    behavior that such a profile installs successfully.
    """

    def _write_slash_profile(self, install_workspace: Dict[str, Any]) -> None:
        _write_profile(
            install_workspace["local_store"] / "my__agent.md",
            name="my/agent",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )
        # The pre-fix code needed context_dir/my/ to exist for the write to land.
        # Create it deliberately, so the refusal below is provably the name
        # validation and not an incidental missing-parent-directory failure.
        (install_workspace["context_dir"] / "my").mkdir(parents=True, exist_ok=True)

    def test_install_reports_the_refusal(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        self._write_slash_profile(install_workspace)

        result = runner.invoke(install, ["my__agent", "--provider", "opencode_cli"])

        # NOTE: asserted on the message, not the exit code. `cao install` echoes
        # "Error: ..." and returns, so a failed install still exits 0 — a
        # pre-existing CLI bug (see cli/commands/install.py, `if not
        # result.success: ... return`) that is out of scope for this fix.
        assert "Error" in result.output
        assert "profile name" in result.output
        assert "my/agent" in result.output

    def test_no_agent_file_or_context_copy_is_written(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        self._write_slash_profile(install_workspace)

        runner.invoke(install, ["my__agent", "--provider", "opencode_cli"])

        assert not (install_workspace["agents_dir"] / "my__agent.md").exists()
        # nothing landed in the pre-created subdirectory either
        assert list((install_workspace["context_dir"] / "my").iterdir()) == []
        assert not install_workspace["config_file"].exists()


# ---------------------------------------------------------------------------
# Scenario: reinstalling without MCP strips stale agent.<id>.tools
# ---------------------------------------------------------------------------


class TestStaleMcpGrantsRemoved:
    def test_reinstall_without_mcp_removes_agent_tools(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        # First install: agent has an MCP server → agent.<id>.tools is written.
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )
        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert "test-agent" in data.get("agent", {}), "precondition: agent entry present"

        # Second install: same agent, MCP servers removed from the profile.
        _write_profile(
            install_workspace["local_store"] / "test-agent.md",
            mcp_servers="",
        )
        runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert "test-agent" not in data.get(
            "agent", {}
        ), "stale agent.<id>.tools entry must be removed on reinstall without MCP"


# ---------------------------------------------------------------------------
# Optional live smoke test: opencode agent list shows the installed agent
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    shutil.which("opencode") is None,
    reason="opencode binary not on PATH",
)
class TestOpencodeAgentListIntegration:
    """Verify that the installed agent appears in `opencode agent list`."""

    def test_installed_agent_visible_in_opencode_list(
        self, runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        local_store = tmp_path / "agent-store"
        context_dir = tmp_path / "agent-context"
        agents_dir = tmp_path / "opencode_cli" / "agents"
        config_file = tmp_path / "opencode_cli" / "opencode.json"

        local_store.mkdir(parents=True)
        context_dir.mkdir(parents=True)

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR", agents_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE", config_file
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        _write_profile(local_store / "smoke-test-agent.md", name="smoke-test-agent")

        result = runner.invoke(install, ["smoke-test-agent", "--provider", "opencode_cli"])
        assert result.exit_code == 0

        env = {
            "OPENCODE_CONFIG": str(config_file),
            "OPENCODE_CONFIG_DIR": str(tmp_path / "opencode_cli"),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
        }
        proc = subprocess.run(
            ["opencode", "agent", "list"],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            timeout=60,
        )
        assert "smoke-test-agent" in proc.stdout or "smoke-test-agent" in proc.stderr
