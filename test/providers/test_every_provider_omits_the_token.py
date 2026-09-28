"""EVERY provider must keep CAO_RUNTIME_TOKEN out of its MCP config (#802).

This finding was filed five separate times, each naming a different site, because
each round I fixed the site that was named instead of the property being violated.
The sites, in the order they were reported: the resolver's own default; opencode and
antigravity; cursor and claude_code (indirectly, through
``resolve_mcp_server_config``'s default); grok, minimax and omp; then codex, kimi
and copilot putting it in argv.

So this test enumerates the property rather than the instances. It discovers every
provider module that builds an MCP config and asserts each one asks for the token to
be omitted — which means a provider added next month is covered without anyone
remembering this file exists.

Two ways a provider may legitimately do it:

* ``persisted=True`` — for a config written to a FILE the provider re-reads. This
  also selects the stable PATH launcher for command resolution, which is what a
  persisted path wants.
* ``omit_token=True`` — for a config serialized into ARGV. Same token behaviour, but
  it keeps the interpreter-sibling command resolution, which a config rebuilt on
  every launch needs (a PATH lookup there can resolve to a different install).

argv is not safer than a file: a 0600 file is readable by the owner, while argv is
readable by every local process. Both must omit it.
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
RESOLVERS = {
    "resolve_mcp_server_config",
    "shared_endpoint_child_env",
    "shared_endpoint_child_env_for",
}
# Names that mean "leave the token out".
OMITTING_KWARGS = {"persisted", "omit_token"}


def _modules_building_mcp_config():
    """Every module that calls a resolver, so a new provider is picked up for free."""
    candidates = (
        sorted(p for p in PROVIDERS_DIR.glob("*.py") if p.name not in {"__init__.py", "base.py"})
        + EXTRA_MODULES
    )
    out = []
    for path in candidates:
        tree = ast.parse(path.read_text())
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in RESOLVERS
        ]
        if calls:
            out.append((path, calls))
    return out


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


@pytest.mark.parametrize(
    "path,calls", _modules_building_mcp_config(), ids=lambda v: getattr(v, "stem", "")
)
def test_every_resolver_call_asks_for_the_token_to_be_omitted(path, calls):
    offenders = []
    for call in calls:
        kwargs = {kw.arg for kw in call.keywords if kw.arg}
        if not (kwargs & OMITTING_KWARGS):
            offenders.append(f"{path}:{call.lineno} {call.func.id}() has no {OMITTING_KWARGS}")
    assert not offenders, (
        "a provider builds an MCP config without omitting the runtime token:\n  "
        + "\n  ".join(offenders)
        + "\n\nPass persisted=True for a file-backed config, or omit_token=True for an\n"
        "inline/argv one (which keeps sibling command resolution)."
    )
