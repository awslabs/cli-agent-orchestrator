"""Every provider builds MCP env through CAO's shared policy, never with the token.

The runtime-channel token's value is never written into an MCP config: the bundled
server receives ``CAO_RUNTIME_TOKEN_FILE`` (a path) from the deployment, and a
profile's own reserved keys are dropped (see ``apply_mcp_env_policy``). This
module checks the two static properties that keep that true for every provider,
including ones added later:

* each module that builds MCP config calls one of the shared resolver/policy
  functions (discovery also guards itself against silently finding nothing);
* no provider module writes ``CAO_RUNTIME_TOKEN`` as a key of an env mapping.

The behavioural side (the value never appears in files or argv, planted keys are
stripped) is covered by ``test_no_persisted_runtime_token.py`` and
``test_profile_cannot_plant_reserved_mcp_env.py``.
"""

import ast
import pathlib

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
PROVIDERS_DIR = _REPO_ROOT / "src" / "cli_agent_orchestrator" / "providers"
# Shared helpers that serialize an MCP config on a provider's behalf.
EXTRA_MODULES = [
    _REPO_ROOT / "src" / "cli_agent_orchestrator" / "utils" / "opencode_config.py",
]
POLICY_CALLS = {
    "resolve_mcp_server_config",
    "apply_mcp_env_policy",
    "shared_endpoint_child_env",
}
TOKEN_NAMES = {"CAO_RUNTIME_TOKEN", "RUNTIME_TOKEN_ENV"}


def _candidates():
    return (
        sorted(p for p in PROVIDERS_DIR.glob("*.py") if p.name not in {"__init__.py", "base.py"})
        + EXTRA_MODULES
    )


def _modules_building_mcp_config():
    out = []
    for path in _candidates():
        tree = ast.parse(path.read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in POLICY_CALLS
        ]
        if calls:
            out.append((path, tree))
    return out


def _names_the_token(node) -> bool:
    if isinstance(node, ast.Constant) and node.value == "CAO_RUNTIME_TOKEN":
        return True
    return isinstance(node, ast.Name) and node.id in TOKEN_NAMES


def test_at_least_the_known_providers_are_discovered():
    """Guard the guard: a discovery that silently finds nothing proves nothing."""
    found = {p.stem for p, _ in _modules_building_mcp_config()}
    expected = {
        "antigravity_cli",
        "claude_code",
        "codex",
        "copilot_cli",
        "cursor_cli",
        "grok_cli",
        "kimi_cli",
        "minimax_code",
        "omp",
        "opencode_config",
    }
    missing = expected - found
    assert not missing, f"discovery stopped finding known providers: {sorted(missing)}"


@pytest.mark.parametrize("path", _candidates(), ids=lambda p: p.stem)
def test_no_provider_writes_the_token_into_an_env_mapping(path):
    offenders = []
    for node in ast.walk(ast.parse(path.read_text())):
        # env["CAO_RUNTIME_TOKEN"] = ...
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and _names_the_token(target.slice):
                    offenders.append(f"{path.name}:{node.lineno} assigns the token key")
        # {"CAO_RUNTIME_TOKEN": ...}
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if key is not None and _names_the_token(key):
                    offenders.append(f"{path.name}:{node.lineno} dict literal with the token key")
    assert not offenders, "a provider writes the runtime token into MCP env:\n  " + "\n  ".join(
        offenders
    )
