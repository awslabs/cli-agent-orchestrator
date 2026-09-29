"""Catalog-wide role contract for packaged profiles and custom roles.

Fail-closed unknown-role handling must not break the shipped agent store.
Every bundled profile has to resolve on a clean settings file, and both
supported custom-role configuration shapes must still work.
"""

from importlib import resources

import pytest

from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

BUNDLED_PROFILES = sorted(
    item.name[: -len(".md")]
    for item in resources.files("cli_agent_orchestrator.agent_store").iterdir()
    if item.name.endswith(".md")
)

_SETTINGS_LOAD = "cli_agent_orchestrator.services.settings_service._load"

WORKFLOW_SCOUT_TOOLS = ["@builtin", "fs_read", "execute_bash", "@cao-mcp-server"]
CUSTOM_ROLE_TOOLS = ["fs_read", "execute_bash", "@cao-mcp-server"]

# Two policies an operator might have saved under the scout's name before it
# became a built-in. Neither grants execute_bash; each withholds something the
# built-in grants and grants something the built-in withholds, so a silent swap
# is visible from both directions.
SAVED_SCOUT_POLICIES = [["fs_read", "fs_list"], ["fs_read", "web_fetch"]]


def _settings_with_role(shape: str, name: str, tools: list[str]) -> dict:
    if shape == "nested":
        return {"agents": {"roles": {name: list(tools)}}}
    return {"roles": {name: list(tools)}}


def _fresh_settings(monkeypatch) -> None:
    monkeypatch.setattr(_SETTINGS_LOAD, lambda: {})


def _resolve_bundled(name: str) -> list[str]:
    text = (resources.files("cli_agent_orchestrator.agent_store") / f"{name}.md").read_text()
    profile = parse_agent_profile_text(text, name)
    mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
    return resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)


@pytest.mark.parametrize("name", BUNDLED_PROFILES)
def test_bundled_profile_resolves_on_fresh_settings(name, monkeypatch):
    """Install/launch/delegate all call resolve_allowed_tools on packaged profiles."""
    _fresh_settings(monkeypatch)
    allowed = _resolve_bundled(name)
    assert allowed, f"{name} resolved to an empty allowlist"
    assert "*" not in allowed, f"{name} must not fall open to unrestricted"


def test_workflow_scout_keeps_documented_allowlist(monkeypatch):
    """Do not silently widen the scout to developer or ['*'] to dodge the exception."""
    _fresh_settings(monkeypatch)
    allowed = _resolve_bundled("workflow_scout")
    assert allowed == WORKFLOW_SCOUT_TOOLS
    assert allowed != list(ROLE_TOOL_DEFAULTS["developer"])


def test_unknown_role_rejected_on_fresh_settings(monkeypatch):
    _fresh_settings(monkeypatch)
    with pytest.raises(ValueError, match="Unknown role 'Supervisor'"):
        resolve_allowed_tools(None, "Supervisor")


def test_nested_custom_role_from_settings(monkeypatch):
    monkeypatch.setattr(
        _SETTINGS_LOAD,
        lambda: {"agents": {"roles": {"data_analyst": list(CUSTOM_ROLE_TOOLS)}}},
    )
    assert resolve_allowed_tools(None, "data_analyst") == CUSTOM_ROLE_TOOLS


def test_legacy_flat_custom_role_from_settings(monkeypatch):
    monkeypatch.setattr(
        _SETTINGS_LOAD,
        lambda: {"roles": {"data_analyst": list(CUSTOM_ROLE_TOOLS)}},
    )
    assert resolve_allowed_tools(None, "data_analyst") == CUSTOM_ROLE_TOOLS


@pytest.mark.parametrize("shape", ["nested", "flat"])
@pytest.mark.parametrize("saved", SAVED_SCOUT_POLICIES, ids=["read-list", "read-fetch"])
def test_saved_role_outranks_new_builtin_of_same_name(shape, saved, monkeypatch):
    """A policy saved before the name became a built-in keeps resolving as saved.

    Built-ins-first lookup silently replaced the saved list with the built-in's,
    so an upgrade granted execute_bash and withheld what the operator asked for.
    """
    monkeypatch.setattr(_SETTINGS_LOAD, lambda: _settings_with_role(shape, "workflow_scout", saved))
    assert resolve_allowed_tools(None, "workflow_scout") == saved


@pytest.mark.parametrize("shape", ["nested", "flat"])
def test_settings_role_shadowing_builtin_is_logged_by_name(shape, monkeypatch, caplog):
    monkeypatch.setattr(
        _SETTINGS_LOAD, lambda: _settings_with_role(shape, "workflow_scout", ["fs_read", "fs_list"])
    )
    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.utils.tool_mapping"):
        resolve_allowed_tools(None, "workflow_scout")
    shadow_lines = [
        r.getMessage() for r in caplog.records if "shadows the built-in" in r.getMessage()
    ]
    assert len(shadow_lines) == 1
    assert "'workflow_scout'" in shadow_lines[0]
    # The policy itself is not logged: settings contents stay out of the server log.
    assert "fs_list" not in shadow_lines[0]


def test_noncolliding_custom_role_still_resolves_beside_saved_scout(monkeypatch):
    monkeypatch.setattr(
        _SETTINGS_LOAD,
        lambda: {
            "roles": {
                "workflow_scout": ["fs_read", "fs_list"],
                "data_analyst": list(CUSTOM_ROLE_TOOLS),
            }
        },
    )
    assert resolve_allowed_tools(None, "data_analyst") == CUSTOM_ROLE_TOOLS
    assert resolve_allowed_tools(None, "workflow_scout") == ["fs_read", "fs_list"]


def test_settings_supervisor_overrides_builtin_supervisor(monkeypatch, caplog):
    """The precedence change is deliberate for every built-in name, not only the scout."""
    monkeypatch.setattr(_SETTINGS_LOAD, lambda: {"roles": {"supervisor": ["fs_read"]}})
    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.utils.tool_mapping"):
        assert resolve_allowed_tools(None, "supervisor") == ["fs_read"]
    assert any("'supervisor'" in r.getMessage() for r in caplog.records)


def test_builtin_resolves_without_warning_when_settings_has_other_roles(monkeypatch, caplog):
    monkeypatch.setattr(
        _SETTINGS_LOAD, lambda: {"roles": {"data_analyst": list(CUSTOM_ROLE_TOOLS)}}
    )
    with caplog.at_level("WARNING", logger="cli_agent_orchestrator.utils.tool_mapping"):
        assert resolve_allowed_tools(None, "workflow_scout") == WORKFLOW_SCOUT_TOOLS
    assert not [r for r in caplog.records if "shadows the built-in" in r.getMessage()]
