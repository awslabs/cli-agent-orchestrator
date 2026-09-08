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
