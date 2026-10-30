"""v3-enabled Kiro installs: the universal agent file (#1167 Phase 1, gates G11 and G13).

``cao install … --engine v3`` (or a profile that says ``engine: v3``) writes one
universal file at today's path: the v2 fields plus ``excludedTools`` and
``permissions``. It is validated against every pinned engine schema before an
atomic write that keeps the file's mode; a reinstall or plugin refresh keeps the
format on disk; every other install stays byte-identical. Provisional (#1167).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.providers.kiro_v3_schema import pinned_kiro_v3_schemas
from cli_agent_orchestrator.services import install_service
from cli_agent_orchestrator.services.install_service import (
    install_agent,
    installed_artifact_paths,
    refresh_installed_agents_for_plugin_mcp,
)
from cli_agent_orchestrator.services.kiro_v3_validate import validate_kiro_v3_agent_bytes

GOLDENS = Path(__file__).parent / "fixtures" / "kiro_install_goldens"
BUILT_IN_PROFILES = (
    "code_supervisor",
    "developer",
    "memory_manager",
    "retrospector",
    "reviewer",
    "workflow_scout",
)
#: One profile per built-in role.
BUILT_IN_ROLES = {
    "supervisor": "code_supervisor",
    "developer": "developer",
    "reviewer": "reviewer",
    "workflow_scout": "workflow_scout",
}


@pytest.fixture
def paths(monkeypatch, tmp_path):
    root = tmp_path
    dirs = {
        k: root / k
        for k in ("agent-store", "agent-context", "kiro", "copilot", "provider", "extra")
    }
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.profile_store.LOCAL_AGENT_STORE_DIR", dirs["agent-store"]
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", dirs["agent-store"]
    )
    monkeypatch.setattr(install_service, "AGENT_CONTEXT_DIR", dirs["agent-context"])
    monkeypatch.setattr(install_service, "KIRO_AGENTS_DIR", dirs["kiro"])
    monkeypatch.setattr(install_service, "COPILOT_AGENTS_DIR", dirs["copilot"])
    monkeypatch.setattr(install_service, "SKILLS_DIR", root / "skills")
    monkeypatch.setattr("cli_agent_orchestrator.utils.env.CAO_ENV_FILE", root / ".env")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
        lambda: {"kiro_cli": str(dirs["provider"])},
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
        lambda: [str(dirs["extra"])],
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.mcp_resolution._sibling_script",
        lambda *a, **k: "/fixture/venv/bin/cao-mcp-server",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.mcp_resolution.shutil.which",
        lambda *a, **k: "/fixture/bin/cao-mcp-server",
    )
    dirs["root"] = root
    return dirs


def _install(name, engine=None, **kwargs):
    result = install_agent(name, "kiro_cli", engine=engine, **kwargs)
    return result


def _text(paths, name):
    text = (paths["kiro"] / f"{name}.json").read_text(encoding="utf-8")
    root = paths["root"]
    return text.replace(os.path.realpath(root), "<ROOT>").replace(str(root), "<ROOT>")


def _store(paths, name, frontmatter, body="Body.\n"):
    (paths["agent-store"] / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {name} agent\n{frontmatter}---\n{body}", encoding="utf-8"
    )


@pytest.mark.parametrize("name", BUILT_IN_PROFILES)
def test_g13_a_v2_install_is_byte_identical_to_main(paths, name):
    """Goldens generated on main before this change, for every built-in profile."""
    assert _install(name).success
    assert _text(paths, name) == (GOLDENS / "v2" / f"{name}.json").read_text(encoding="utf-8")


@pytest.mark.parametrize("role", sorted(BUILT_IN_ROLES))
def test_g13_the_universal_render_per_built_in_role_is_frozen_and_valid(paths, role):
    name = BUILT_IN_ROLES[role]
    result = _install(name, engine="v3")
    assert result.success, result.message
    raw = (paths["kiro"] / f"{name}.json").read_bytes()
    assert _text(paths, name) == (GOLDENS / "universal" / f"{name}.json").read_text(
        encoding="utf-8"
    )
    for schema in pinned_kiro_v3_schemas():
        assert validate_kiro_v3_agent_bytes(raw, schema, expected_name=name) == []
    data = json.loads(raw)
    assert "read" not in data["tools"]
    assert "kiroUpgrade" not in data and "includePowers" not in data


def _is_universal(paths, name):
    return "permissions" in json.loads((paths["kiro"] / f"{name}.json").read_text())


def test_engine_v3_writes_universal_and_a_reinstall_keeps_it(paths):
    assert _install("reviewer", engine="v3").success
    first = (paths["kiro"] / "reviewer.json").read_bytes()
    assert _install("reviewer").success  # no --engine: the format on disk is kept
    assert (paths["kiro"] / "reviewer.json").read_bytes() == first
    assert _install("reviewer", engine="kas").success  # the alias, same bytes
    assert (paths["kiro"] / "reviewer.json").read_bytes() == first


def test_engine_v2_writes_the_v2_only_file_again(paths):
    assert _install("reviewer", engine="v3").success
    assert _install("reviewer", engine="v2").success
    assert _text(paths, "reviewer") == (GOLDENS / "v2" / "reviewer.json").read_text(
        encoding="utf-8"
    )


def test_a_profile_that_says_engine_v3_installs_universal(paths):
    _store(paths, "v3agent", "engine: v3\nrole: reviewer\n")
    assert _install("v3agent").success
    assert _is_universal(paths, "v3agent")


def test_an_explicit_engine_must_agree_with_the_profile(paths):
    _store(paths, "v3agent", "engine: v3\nrole: reviewer\n")
    result = _install("v3agent", engine="v2")
    assert not result.success
    assert "conflict" in result.message
    assert not (paths["kiro"] / "v3agent.json").exists()


def test_engine_selection_needs_the_kiro_provider(paths):
    result = install_agent("reviewer", "claude_code", engine="v3")
    assert not result.success
    assert "only valid for provider 'kiro_cli'" in result.message


def test_the_plugin_refresh_keeps_the_universal_format(paths):
    assert _install("reviewer", engine="v3").success
    before = (paths["kiro"] / "reviewer.json").read_bytes()
    assert "reviewer" in refresh_installed_agents_for_plugin_mcp()
    assert (paths["kiro"] / "reviewer.json").read_bytes() == before
    assert _is_universal(paths, "reviewer")


def test_the_plugin_refresh_leaves_a_v2_file_v2(paths):
    assert _install("reviewer").success
    before = (paths["kiro"] / "reviewer.json").read_bytes()
    refresh_installed_agents_for_plugin_mcp()
    assert (paths["kiro"] / "reviewer.json").read_bytes() == before


@pytest.mark.parametrize(
    ("frontmatter", "field"),
    [
        ("role: reviewer\nhooks:\n  agentSpawn: [{command: 'echo hi'}]\n", "hooks"),
        ("role: reviewer\ntoolsSettings:\n  fs_read: {allowedPaths: ['.']}\n", "toolsSettings"),
        ("role: reviewer\ntoolAliases:\n  read: fs_read\n", "toolAliases"),
        ("role: reviewer\nuseLegacyMcpJson: true\n", "useLegacyMcpJson"),
        ("allowedTools: ['*']\n", "unrestricted"),
        ("role: reviewer\ntools: ['fs_read']\n", "tools"),
        # Set at all, not truthy: on v2 an empty ``tools`` is deny-everything.
        ("role: reviewer\ntools: []\n", "tools"),
        ("role: reviewer\nhooks: {}\n", "hooks"),
    ],
    ids=[
        "hooks",
        "toolsSettings",
        "toolAliases",
        "useLegacyMcpJson",
        "unrestricted",
        "explicit_tools",
        "explicit_empty_tools",
        "empty_hooks",
    ],
)
def test_d9_untranslatable_profiles_refuse_and_write_nothing(paths, frontmatter, field):
    _store(paths, "odd", frontmatter)
    assert _install("odd").success  # v2 still installs exactly as before
    before = (paths["kiro"] / "odd.json").read_bytes()
    result = _install("odd", engine="v3")
    assert not result.success
    assert field in result.message
    assert (paths["kiro"] / "odd.json").read_bytes() == before


def test_the_render_is_validated_before_anything_is_written(paths, monkeypatch):
    assert _install("reviewer").success
    before = (paths["kiro"] / "reviewer.json").read_bytes()
    real_rules = install_service.kiro_v3_rules

    def ask_rules(allowed, servers):
        return real_rules(allowed, servers) + [{"capability": "shell", "effect": "ask"}]

    monkeypatch.setattr(install_service, "kiro_v3_rules", ask_rules)
    result = _install("reviewer", engine="v3")
    assert not result.success
    assert "kiro_v3_invalid:rule_effect" in result.message
    assert (paths["kiro"] / "reviewer.json").read_bytes() == before


def test_the_universal_write_is_atomic_and_keeps_the_mode(paths):
    assert _install("reviewer").success
    target = paths["kiro"] / "reviewer.json"
    os.chmod(target, 0o640)
    with patch.object(
        install_service, "locked_atomic_write", wraps=install_service.locked_atomic_write
    ) as spy:
        assert _install("reviewer", engine="v3").success
    spy.assert_called_once()
    assert spy.call_args.args[0] == target
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o640


def test_a_new_universal_file_gets_the_default_mode(paths):
    assert _install("developer").success
    v2_mode = stat.S_IMODE(os.stat(paths["kiro"] / "developer.json").st_mode)
    assert _install("reviewer", engine="v3").success
    assert stat.S_IMODE(os.stat(paths["kiro"] / "reviewer.json").st_mode) == v2_mode


def test_an_ungranted_server_is_left_out_of_the_universal_file():
    """A plugin server merged into ``mcpServers`` but not granted is not configured."""
    from cli_agent_orchestrator.models.agent_profile import AgentProfile

    profile = AgentProfile(
        name="mcpagent",
        description="d",
        mcpServers={
            "cao-mcp-server": {"command": "cao-mcp-server", "args": []},
            "plugin-decoy": {"command": "decoy-server"},
        },
    )
    text = install_service._render_kiro_universal_agent(
        profile, ["fs_read", "@cao-mcp-server"], [], None, "mcpagent"
    )
    data = json.loads(text)
    assert set(data["mcpServers"]) == {"cao-mcp-server"}
    assert {
        "capability": "mcp",
        "match": ["*"],
        "exclude": ["cao-mcp-server/*"],
        "effect": "deny",
    } in (data["permissions"]["rules"])


def test_g11_plan_identity_keeps_its_label_set(paths):
    assert _install("reviewer").success
    v2_labels = set(installed_artifact_paths("reviewer"))
    v2_paths = installed_artifact_paths("reviewer")
    assert _install("reviewer", engine="v3").success
    assert set(installed_artifact_paths("reviewer")) == v2_labels
    assert installed_artifact_paths("reviewer") == v2_paths


def test_an_unreadable_existing_file_reinstalls_as_v2_as_today(paths):
    assert _install("reviewer", engine="v3").success
    (paths["kiro"] / "reviewer.json").write_text("{not json", encoding="utf-8")
    assert _install("reviewer").success
    assert _text(paths, "reviewer") == (GOLDENS / "v2" / "reviewer.json").read_text(
        encoding="utf-8"
    )


def test_cao_install_passes_the_engine_choice(paths):
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.install import install

    with patch("cli_agent_orchestrator.cli.commands.install.install_agent") as mock_install:
        mock_install.return_value = install_service.InstallResult(
            success=True, message="ok", agent_name="reviewer"
        )
        result = CliRunner().invoke(
            install, ["reviewer", "--provider", "kiro_cli", "--engine", "kas"]
        )
    assert result.exit_code == 0, result.output
    assert mock_install.call_args.kwargs["engine"] == "kas"


def test_cao_install_rejects_an_unknown_engine():
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.install import install

    with patch("cli_agent_orchestrator.cli.commands.install.install_agent") as mock_install:
        result = CliRunner().invoke(install, ["reviewer", "--engine", "v4"])
    assert result.exit_code == 2
    mock_install.assert_not_called()


def _oversized_prompt():
    from cli_agent_orchestrator.services.kiro_v3_validate import MAX_AGENT_FILE_BYTES

    return "x" * (MAX_AGENT_FILE_BYTES + 1)


@pytest.mark.parametrize("via", ["prompt", "resources"])
def test_a_v3_install_refused_by_the_size_cap_writes_nothing(paths, via):
    """The size cap is the one check whose result depends on the prompt and the
    resources, so the pre-write render must carry both (#1167 review)."""
    big = _oversized_prompt()
    field = f"prompt: {big}" if via == "prompt" else f"resources: ['file://{big}']"
    _store(paths, "huge", f"role: reviewer\n{field}\n")
    store_before = (paths["agent-store"] / "huge.md").read_bytes()
    result = _install("huge", engine="v3")
    assert not result.success
    assert "kiro_v3_invalid:too_large" in result.message
    assert not (paths["kiro"] / "huge.json").exists()
    assert not (paths["agent-context"] / "huge.md").exists()
    assert (paths["agent-store"] / "huge.md").read_bytes() == store_before


def test_a_v3_reinstall_refused_by_the_size_cap_keeps_every_earlier_file(paths):
    _store(paths, "huge", "role: reviewer\n")
    assert _install("huge", engine="v3").success
    agent_before = (paths["kiro"] / "huge.json").read_bytes()
    context_before = (paths["agent-context"] / "huge.md").read_bytes()
    _store(paths, "huge", f"role: reviewer\nprompt: {_oversized_prompt()}\n")
    store_before = (paths["agent-store"] / "huge.md").read_bytes()
    result = _install("huge", engine="v3")
    assert not result.success
    assert "kiro_v3_invalid:too_large" in result.message
    assert (paths["kiro"] / "huge.json").read_bytes() == agent_before
    assert (paths["agent-context"] / "huge.md").read_bytes() == context_before
    assert (paths["agent-store"] / "huge.md").read_bytes() == store_before


def test_a_refused_v3_install_writes_nothing_at_all(paths):
    """The refusal comes before the context copy, the store record and the agent file."""
    _store(paths, "hooked", "role: reviewer\nhooks:\n  agentSpawn: [{command: 'echo hi'}]\n")
    store_before = (paths["agent-store"] / "hooked.md").read_bytes()
    result = _install("hooked", engine="v3")
    assert not result.success
    assert not (paths["kiro"] / "hooked.json").exists()
    assert not (paths["agent-context"] / "hooked.md").exists()
    assert (paths["agent-store"] / "hooked.md").read_bytes() == store_before
