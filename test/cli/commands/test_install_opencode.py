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
        "cli_agent_orchestrator.services.install_service.LOCAL_AGENT_STORE_DIR", local_store
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
# Scenario: slash-safe agent ID parity (filename === opencode.json key)
# ---------------------------------------------------------------------------


class TestSlashSafeAgentId:
    """The sanitized agent ID must be used for both the .md filename and the
    ``agent.<id>.tools`` key in opencode.json, so the value passed to
    ``opencode --agent <id>`` at runtime lines up with its MCP grants."""

    def _write_slash_profile(self, install_workspace: Dict[str, Any]) -> None:
        _write_profile(
            install_workspace["local_store"] / "my__agent.md",
            name="my/agent",
            mcp_servers="  cao-mcp-server:\n    command: cao-mcp-server\n",
        )
        # profile.name "my/agent" → context path would be context_dir/my/agent.md;
        # pre-create the intermediate dir so the context write doesn't fail before
        # reaching the agent-file step that we want to assert on.
        (install_workspace["context_dir"] / "my").mkdir(parents=True, exist_ok=True)

    def test_slash_replaced_in_agent_filename(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        self._write_slash_profile(install_workspace)

        runner.invoke(install, ["my__agent", "--provider", "opencode_cli"])

        agent_file = install_workspace["agents_dir"] / "my__agent.md"
        assert agent_file.exists()

    def test_opencode_json_uses_sanitized_agent_id(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """The agent.<id>.tools key must use the sanitized filename, not the
        frontmatter ``name`` with ``/`` in it."""
        self._write_slash_profile(install_workspace)

        runner.invoke(install, ["my__agent", "--provider", "opencode_cli"])

        data = json.loads(install_workspace["config_file"].read_text())
        assert "my__agent" in data["agent"], "sanitized agent ID must be the key"
        assert data["agent"]["my__agent"]["tools"]["cao-mcp-server*"] is True
        assert (
            "my/agent" not in data["agent"]
        ), "unsanitized profile.name must not be written as an agent key"


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
# Agent-id collision guard: '/' -> '__' derivation is not injective
# ---------------------------------------------------------------------------


class TestAgentIdCollisionGuard:
    """Installing a profile whose id collides with another must fail loud.

    The id derivation replaces '/' with '__', so a profile named ``a/b`` and a
    literal profile named ``a__b`` both map to the ``a__b`` id — the second
    install would silently overwrite the first's ``a__b.md`` file and
    ``agent.a__b`` config. The guard turns that into a clean CLI error.
    """

    def test_real_collision_fails_and_names_both_profiles(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        store = install_workspace["local_store"]
        # A sibling profile that literally occupies the "a__b" id.
        _write_profile(store / "a__b.md", name="a__b")
        # The profile we install has frontmatter name "a/b" -> id "a__b".
        # File stem must be a legal source name; the '/' lives in frontmatter.
        _write_profile(store / "slash-named.md", name="a/b")
        # profile.name "a/b" → context path context_dir/a/b.md; pre-create the
        # intermediate dir so the context write (which runs before the provider
        # branch) doesn't fail before the collision guard is reached.
        (install_workspace["context_dir"] / "a").mkdir(parents=True, exist_ok=True)

        result = runner.invoke(install, ["slash-named", "--provider", "opencode_cli"])

        assert result.exit_code == 0  # install_agent returns a failure result, not a crash
        assert "Error:" in result.output
        assert "a/b" in result.output and "a__b" in result.output
        # The colliding profile must NOT have been written under the shared id.
        # Only the pre-existing sibling (if installed) could own a__b.md; the
        # slash-named install must be refused before writing.
        assert not (install_workspace["agents_dir"] / "a__b.md").exists()

    def test_same_resolved_name_different_stem_fails_and_preserves_first(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """Two distinct files with the IDENTICAL frontmatter name collide.

        Both ``profile-one.md`` and ``profile-two.md`` carry ``name: shared-alias``,
        so both resolve to the id ``shared-alias`` — the same ``shared-alias.md``
        file and ``agent.shared-alias`` config section. The second install must
        fail (naming both files/name) rather than silently overwrite the first.
        This is the same-resolved-name-different-stem gap: the id derivation is
        many-to-one on the *name* even without any '/' rewrite.
        """
        store = install_workspace["local_store"]
        # Install the first profile while it is the only one on disk.
        _write_profile(store / "profile-one.md", name="shared-alias", body="First agent body.")
        r1 = runner.invoke(install, ["profile-one", "--provider", "opencode_cli"])
        assert r1.exit_code == 0 and "Error:" not in r1.output
        agent_file = install_workspace["agents_dir"] / "shared-alias.md"
        assert agent_file.exists()
        first_contents = agent_file.read_text()
        assert "First agent body." in first_contents

        # Also capture the shared context file from the first install.
        context_file = install_workspace["context_dir"] / "shared-alias.md"
        assert context_file.exists()
        first_context = context_file.read_text()

        # A second, DIFFERENT file later appears with the same resolved name.
        _write_profile(store / "profile-two.md", name="shared-alias", body="Second agent body.")

        # Second install (different file, same resolved name) must be refused.
        r2 = runner.invoke(install, ["profile-two", "--provider", "opencode_cli"])
        assert r2.exit_code == 0  # returns a failure result, not a crash
        assert "Error:" in r2.output
        # The error must name both offending profiles and the shared name.
        assert "profile-one" in r2.output
        assert "profile-two" in r2.output
        assert "shared-alias" in r2.output
        # The first install's file must be intact — NOT overwritten by the second.
        assert agent_file.read_text() == first_contents
        assert "Second agent body." not in agent_file.read_text()
        # Regression check for caom-934: the shared context file must ALSO be
        # preserved. Before the fix, the guard ran AFTER _write_context_file(),
        # so the rejected second install would corrupt AGENT_CONTEXT_DIR/<id>.md
        # even though opencode_cli/agents/<id>.md was protected.
        assert context_file.read_text() == first_context
        assert "Second agent body." not in context_file.read_text()

    def test_reinstall_same_profile_stays_idempotent_despite_guard(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """Reinstalling the SAME profile (same stem) must not trip the guard.

        The guard excludes candidates by stem, so a profile never collides with
        itself even though discovery lists it with its own resolved name.
        """
        _write_profile(install_workspace["local_store"] / "test-agent.md", name="test-agent")

        r1 = runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])
        r2 = runner.invoke(install, ["test-agent", "--provider", "opencode_cli"])

        assert r1.exit_code == 0 and "Error:" not in r1.output
        assert r2.exit_code == 0 and "Error:" not in r2.output
        assert (install_workspace["agents_dir"] / "test-agent.md").exists()

    def test_non_colliding_spaces_vs_dash_both_install(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """ "foo bar" and "foo-bar" do NOT collide (only '/' is rewritten)."""
        store = install_workspace["local_store"]
        _write_profile(store / "foo-space.md", name="foo bar")
        _write_profile(store / "foo-dash.md", name="foo-bar")

        r1 = runner.invoke(install, ["foo-space", "--provider", "opencode_cli"])
        r2 = runner.invoke(install, ["foo-dash", "--provider", "opencode_cli"])

        assert r1.exit_code == 0 and "Error:" not in r1.output
        assert r2.exit_code == 0 and "Error:" not in r2.output
        # Distinct ids => distinct files, both present.
        assert (install_workspace["agents_dir"] / "foo bar.md").exists()
        assert (install_workspace["agents_dir"] / "foo-bar.md").exists()

    def test_normal_single_profile_install_unaffected(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """The guard is a no-op for an ordinary, non-colliding profile."""
        _write_profile(install_workspace["local_store"] / "solo-agent.md", name="solo-agent")

        result = runner.invoke(install, ["solo-agent", "--provider", "opencode_cli"])

        assert result.exit_code == 0
        assert "Error:" not in result.output
        assert (install_workspace["agents_dir"] / "solo-agent.md").exists()


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
            "cli_agent_orchestrator.services.install_service.LOCAL_AGENT_STORE_DIR", local_store
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
