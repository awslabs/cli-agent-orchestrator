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
from cli_agent_orchestrator.cli.commands.profile import profile as profile_cmd

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
    # ``cao_installed`` must point at this workspace's context dir, not be absent.
    # Its real default IS ``AGENT_CONTEXT_DIR`` (see settings_service._DEFAULTS), so
    # an empty dict is not a neutral stub -- it is a configuration production never
    # has, in which discovery surfaces no ``source == "installed"`` profiles at all.
    # The collision guard keys on exactly those (only an installed profile owns an
    # agent id), so leaving this empty silently disables the guard and every
    # collision test passes for the wrong reason.
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
        lambda: {"cao_installed": str(context_dir)},
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


@pytest.fixture()
def install_workspace_with_installed_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Dict[str, Any]:
    """Redirect install paths while keeping the real default agent-dir discovery shape."""
    local_store = tmp_path / "agent-store"
    context_dir = tmp_path / "agent-context"
    opencode_agents = tmp_path / "opencode_cli" / "agents"
    opencode_config = tmp_path / "opencode_cli" / "opencode.json"

    local_store.mkdir(parents=True)
    context_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.profile.LOCAL_AGENT_STORE_DIR", local_store
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.AGENT_CONTEXT_DIR", context_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.install_service.OPENCODE_AGENTS_DIR", opencode_agents
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.opencode_config.OPENCODE_CONFIG_FILE", opencode_config
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.SETTINGS_FILE",
        tmp_path / "settings.json",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service._DEFAULTS",
        {
            "kiro_cli": str(tmp_path / "kiro" / "agents"),
            "claude_code": str(local_store),
            "codex": str(local_store),
            "cao_installed": str(context_dir),
        },
    )
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
# Agent-id collision guard: '/' -> '__' derivation is not injective
# ---------------------------------------------------------------------------


class TestAgentIdCollisionGuard:
    """Installing a profile whose id collides with another must fail loud.

    The id derivation replaces '/' with '__', so a profile named ``a/b`` and a
    literal profile named ``a__b`` both map to the ``a__b`` id — the second
    install would silently overwrite the first's ``a__b.md`` file and
    ``agent.a__b`` config. The guard turns that into a clean CLI error.
    """

    def test_slash_collapse_collision_is_unreachable_because_the_name_is_refused(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """The originally-reported ``'/'``->``'__'`` collision cannot happen here.

        ``"a/b"`` and a literal ``"a__b"`` do both map to the id ``a__b``, but a
        resolved name containing a separator no longer survives the install:
        ``_write_context_file`` validates it earlier in the same ``install_agent``
        call. So the install is refused for the *name*, not for a collision, and the
        pre-existing ``a__b`` profile is never at risk.

        Asserting the mechanism matters, because "install refused" alone would pass
        for either reason and this is the claim the guard's scope now rests on: if
        the separator rule is ever relaxed, this test fails and points at the guard
        rather than letting the dead vector quietly come back to life.
        """
        store = install_workspace["local_store"]
        # A sibling profile that literally occupies the "a__b" id, installed first.
        _write_profile(store / "a__b.md", name="a__b")
        r1 = runner.invoke(install, ["a__b", "--provider", "opencode_cli"])
        assert r1.exit_code == 0 and "Error:" not in r1.output, r1.output
        occupant = install_workspace["agents_dir"] / "a__b.md"
        assert occupant.exists()
        occupant_before = occupant.read_text()

        # The profile we install has frontmatter name "a/b" -> would be id "a__b".
        # File stem must be a legal source name; the '/' lives in frontmatter.
        _write_profile(store / "slash-named.md", name="a/b")

        result = runner.invoke(install, ["slash-named", "--provider", "opencode_cli"])

        assert result.exit_code == 0  # install_agent returns a failure result, not a crash
        assert "Error:" in result.output
        # Refused by the name validation, naming the offending value...
        assert "path separator" in result.output, result.output
        assert "a/b" in result.output
        # ...and NOT by the collision guard, which never saw it.
        assert "cannot share an OpenCode agent id" not in result.output
        # The occupant is untouched.
        assert occupant.read_text() == occupant_before

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
        # Regression check: the shared context file must ALSO be
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

    def test_non_colliding_punctuation_variants_both_install(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """Names differing only by punctuation do NOT collide.

        ``to_opencode_agent_id`` rewrites separators and nothing else, so ``foo_bar``
        and ``foo-bar`` keep distinct ids and both install. This is the guard's
        false-positive check: it must fire on a genuine id clash, not on any two
        similar-looking names.

        Both names are valid under the profile schema's own ``name`` pattern
        (``^[A-Za-z0-9_-]{1,64}$``), which is deliberate -- a test asserting that
        two profiles install cleanly should not itself use a name the schema calls
        invalid. (This previously used ``"foo bar"`` versus ``"foo-bar"``; a space
        has never been a legal profile name, see
        ``test_profile_name_with_a_space_is_rejected_before_any_id_is_derived``.)
        """
        store = install_workspace["local_store"]
        _write_profile(store / "foo-under.md", name="foo_bar")
        _write_profile(store / "foo-dash.md", name="foo-bar")

        r1 = runner.invoke(install, ["foo-under", "--provider", "opencode_cli"])
        r2 = runner.invoke(install, ["foo-dash", "--provider", "opencode_cli"])

        assert r1.exit_code == 0 and "Error:" not in r1.output, r1.output
        assert r2.exit_code == 0 and "Error:" not in r2.output, r2.output
        # Distinct ids => distinct files, both present.
        assert (install_workspace["agents_dir"] / "foo_bar.md").exists()
        assert (install_workspace["agents_dir"] / "foo-bar.md").exists()

    def test_profile_name_with_a_space_is_rejected_before_any_id_is_derived(
        self, runner: CliRunner, install_workspace: Dict[str, Any]
    ):
        """A resolved ``name:`` outside ``[A-Za-z0-9._-]`` fails the install.

        A space has never been a legal profile name: the schema's ``name`` pattern
        is ``^[A-Za-z0-9_-]{1,64}$``, described there as "filesystem-safe". What the
        path-traversal hardening changed is that the rule is now ENFORCED at install
        time -- ``_write_context_file`` runs ``validate_path_component`` on the
        resolved name before any provider sink -- rather than being documented and
        checked only by the validator endpoint. The install fails cleanly rather
        than writing a half-installed profile, and no agent file appears under
        either the raw or a flattened spelling of the name.

        This is what makes the collision guard's scope claim true -- the
        separator-collapse vector cannot be reached from here -- so it is pinned
        rather than left implicit.
        """
        _write_profile(install_workspace["local_store"] / "spacey.md", name="foo bar")

        result = runner.invoke(install, ["spacey", "--provider", "opencode_cli"])

        assert result.exit_code != 0 or "Error:" in result.output, result.output
        agents_dir = install_workspace["agents_dir"]
        assert not (agents_dir / "foo bar.md").exists()
        assert not (agents_dir / "foo__bar.md").exists()

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
# Provenance guard: installed copies with provenance markers
# ---------------------------------------------------------------------------


class TestAgentIdCollisionGuardInstalledProvenance:
    """Collision checks must distinguish an installed self-copy from another profile."""

    def test_stem_not_name_reinstall_succeeds_with_installed_copy_discovered(
        self, runner: CliRunner, install_workspace_with_installed_dir: Dict[str, Any]
    ):
        store = install_workspace_with_installed_dir["local_store"]
        _write_profile(store / "my-agent.md", name="resolved-agent", body="Resolved v1.")

        r1 = runner.invoke(install, ["my-agent", "--provider", "opencode_cli"])
        r2 = runner.invoke(install, ["my-agent", "--provider", "opencode_cli"])

        assert r1.exit_code == 0 and "Error:" not in r1.output
        assert r2.exit_code == 0 and "Error:" not in r2.output

        context_file = install_workspace_with_installed_dir["context_dir"] / "resolved-agent.md"
        post = frontmatter.loads(context_file.read_text())
        assert post.metadata["x-cao-source-stem"] == "my-agent"
        assert post.metadata["name"] == "resolved-agent"
        assert post.content.strip() == "Resolved v1."

    def test_multiple_stem_not_name_reinstall_cycles_remain_idempotent(
        self, runner: CliRunner, install_workspace_with_installed_dir: Dict[str, Any]
    ):
        store = install_workspace_with_installed_dir["local_store"]
        _write_profile(store / "my-agent.md", name="resolved-agent", body="Resolved v1.")

        agent_seq = []
        context_seq = []
        for _ in range(4):
            result = runner.invoke(install, ["my-agent", "--provider", "opencode_cli"])
            assert result.exit_code == 0 and "Error:" not in result.output, result.output

            agent_seq.append(
                (
                    install_workspace_with_installed_dir["agents_dir"] / "resolved-agent.md"
                ).read_bytes()
            )
            context_seq.append(
                (
                    install_workspace_with_installed_dir["context_dir"] / "resolved-agent.md"
                ).read_bytes()
            )

        # The generated agent file is stable from the very first install.
        assert len(set(agent_seq)) == 1, "agent file churns between reinstalls"

        # The context copy CONVERGES after one install rather than being stable
        # from the first, and that is a property of the install, not a slack
        # assertion. Installing with an explicit --provider that differs from the
        # profile's own materialises the choice into the local store
        # (``write_profile`` with the resolved ``provider:`` added), which
        # reserializes the frontmatter -- reordering keys and normalising trailing
        # whitespace. The context copy is taken from that stored source, so cycle 1
        # reads pre-stamp bytes and every later cycle reads post-stamp bytes.
        # Asserting stability from cycle 2 onward still catches the failure that
        # matters (a copy that keeps churning, e.g. a provenance marker appended
        # afresh each time), while not demanding that the first install be a no-op
        # on a store it is documented to update.
        assert len(set(context_seq[1:])) == 1, (
            "context copy never converges; it still differs after the provider stamp "
            f"has settled: {context_seq[1:]!r}"
        )
        # Pin that the ONE permitted difference is the provider stamp and nothing
        # else, so an unrelated change to cycle 1 does not hide here.
        assert b"provider: opencode_cli" not in context_seq[0]
        assert b"provider: opencode_cli" in context_seq[1]
        # The provenance marker is present throughout and never duplicated.
        for i, blob in enumerate(context_seq):
            assert blob.count(b"x-cao-source-stem:") == 1, f"cycle {i}: {blob!r}"

    def test_an_uninstalled_local_twin_does_not_block_but_owning_the_id_does(
        self, runner: CliRunner, install_workspace_with_installed_dir: Dict[str, Any]
    ):
        """The guard fires on OCCUPANCY, so it blocks one install later than before.

        ``profile-a`` and ``profile-b`` both resolve to ``shared-alias``. While
        neither is installed, nothing owns ``shared-alias.md``, so installing either
        destroys nothing and must be allowed -- the guard deliberately does not
        refuse on a twin that merely exists in the local store. (The pre-emptive
        form did, which is what made every profile named after one of CAO's six
        built-ins uninstallable.)

        Once ``profile-b`` has taken the id, installing ``profile-a`` WOULD overwrite
        it, and that is refused, naming both files. So nothing is lost by waiting:
        the guard fires exactly when there is something to lose.
        """
        store = install_workspace_with_installed_dir["local_store"]
        agent_file = install_workspace_with_installed_dir["agents_dir"] / "shared-alias.md"
        _write_profile(store / "profile-a.md", name="shared-alias", body="First profile.")
        _write_profile(store / "profile-b.md", name="shared-alias", body="Second profile.")

        # Nothing owns the id yet, so this is allowed even though a twin exists.
        r_b = runner.invoke(install, ["profile-b", "--provider", "opencode_cli"])
        assert r_b.exit_code == 0 and "Error:" not in r_b.output, r_b.output
        assert agent_file.exists()
        owned_by_b = agent_file.read_text()

        # Now the id IS owned, and the twin's install would clobber it.
        r_a = runner.invoke(install, ["profile-a", "--provider", "opencode_cli"])

        assert r_a.exit_code == 0
        assert "Error:" in r_a.output, r_a.output
        assert "profile-a" in r_a.output
        assert "profile-b" in r_a.output
        # And profile-b's artifact survived intact.
        assert agent_file.read_text() == owned_by_b

    def test_removed_local_profile_leaves_installed_copy_that_still_blocks_collision(
        self, runner: CliRunner, install_workspace_with_installed_dir: Dict[str, Any]
    ):
        store = install_workspace_with_installed_dir["local_store"]
        _write_profile(store / "profile-a.md", name="shared-alias", body="First profile.")

        r1 = runner.invoke(install, ["profile-a", "--provider", "opencode_cli"])
        assert r1.exit_code == 0 and "Error:" not in r1.output

        agent_file = install_workspace_with_installed_dir["agents_dir"] / "shared-alias.md"
        context_file = install_workspace_with_installed_dir["context_dir"] / "shared-alias.md"
        first_agent = agent_file.read_text()
        first_context = context_file.read_text()

        removed = runner.invoke(profile_cmd, ["remove", "profile-a", "-y"])
        assert removed.exit_code == 0, removed.output
        assert not (store / "profile-a.md").exists()
        assert context_file.exists()

        _write_profile(store / "profile-b.md", name="shared-alias", body="Second profile.")
        r2 = runner.invoke(install, ["profile-b", "--provider", "opencode_cli"])

        assert r2.exit_code == 0
        assert "Error:" in r2.output
        assert "profile-a" in r2.output
        assert "profile-b" in r2.output
        assert agent_file.read_text() == first_agent
        assert context_file.read_text() == first_context
        assert "Second profile." not in agent_file.read_text()
        assert "Second profile." not in context_file.read_text()

    def test_legacy_installed_copy_without_marker_blocks_same_profile_reinstall(
        self, runner: CliRunner, install_workspace_with_installed_dir: Dict[str, Any]
    ):
        store = install_workspace_with_installed_dir["local_store"]
        _write_profile(store / "legacy-source.md", name="legacy-agent", body="Legacy profile.")
        legacy_copy = install_workspace_with_installed_dir["context_dir"] / "legacy-agent.md"
        _write_profile(legacy_copy, name="legacy-agent", body="Legacy profile.")
        first_context = legacy_copy.read_text()

        result = runner.invoke(install, ["legacy-source", "--provider", "opencode_cli"])

        assert result.exit_code == 0
        assert "Error:" in result.output, result.output
        assert str(legacy_copy) in result.output
        assert (
            f"If '{legacy_copy}' is your own profile's context copy from an earlier "
            "CAO version, delete it and reinstall."
        ) in result.output
        assert legacy_copy.read_text() == first_context

    def test_legacy_installed_copy_without_marker_still_blocks_id_alias_collision(
        self, runner: CliRunner, install_workspace_with_installed_dir: Dict[str, Any]
    ):
        store = install_workspace_with_installed_dir["local_store"]
        legacy_copy = install_workspace_with_installed_dir["context_dir"] / "a__b.md"
        _write_profile(legacy_copy, name="a__b", body="Legacy profile.")
        first_context = legacy_copy.read_text()

        _write_profile(store / "slash-named.md", name="a/b", body="Second profile.")
        result = runner.invoke(install, ["slash-named", "--provider", "opencode_cli"])

        assert result.exit_code == 0
        assert "Error:" in result.output
        assert "slash-named" in result.output
        assert "a__b" in result.output
        assert legacy_copy.read_text() == first_context
        assert "Second profile." not in legacy_copy.read_text()


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
