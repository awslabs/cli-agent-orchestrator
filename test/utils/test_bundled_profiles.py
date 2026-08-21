"""Guards on the shipped bundled agent profiles.

These assert the contract of the profiles that ship inside the wheel — most
importantly that their cao-mcp-server MCP server is declared as the bare
console-script command, NOT a per-launch network fetch (the old
``uvx --from git+https://...`` form, which cold-fetched the whole package on
every agent launch and tripped provider MCP startup timeouts).
"""

from importlib import resources

import frontmatter
import pytest

from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

# Enumerate dynamically so a newly added bundled profile is guarded
# automatically instead of silently skipped.
BUNDLED_PROFILES = sorted(
    item.name[: -len(".md")]
    for item in resources.files("cli_agent_orchestrator.agent_store").iterdir()
    if item.name.endswith(".md")
)


def _load_bundled(name: str):
    text = (resources.files("cli_agent_orchestrator.agent_store") / f"{name}.md").read_text()
    return parse_agent_profile_text(text, name)


@pytest.mark.parametrize("name", BUNDLED_PROFILES)
def test_bundled_profile_uses_console_script_mcp_command(name):
    """Bundled profiles must declare cao-mcp-server as the bare console script."""
    profile = _load_bundled(name)
    assert profile.mcpServers, f"{name} should declare a cao-mcp-server"
    entry = profile.mcpServers["cao-mcp-server"]
    assert (
        entry["command"] == "cao-mcp-server"
    ), f"{name} must use the installed console script, not a network fetch"
    assert entry.get("args", []) == []


@pytest.mark.parametrize("name", BUNDLED_PROFILES)
def test_bundled_profile_does_not_network_fetch_mcp_server(name):
    """Regression guard: no profile may reintroduce the uvx git+https fetch."""
    raw = (resources.files("cli_agent_orchestrator.agent_store") / f"{name}.md").read_text()
    assert (
        "git+https://github.com/awslabs/cli-agent-orchestrator" not in raw
    ), f"{name} reintroduced the cold uvx git+https fetch"


@pytest.mark.parametrize("name", BUNDLED_PROFILES)
def test_bundled_profile_parses(name):
    """Each bundled profile must parse with valid frontmatter."""
    text = (resources.files("cli_agent_orchestrator.agent_store") / f"{name}.md").read_text()
    meta = frontmatter.loads(text).metadata
    assert meta.get("name") == name
    assert meta.get("description")
