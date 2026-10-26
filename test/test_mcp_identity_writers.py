"""Every provider that writes an MCP server's launch config forwards the REAL terminal id.

``CAO_TERMINAL_ID`` is an AUTHORIZATION input (#671/#972): both MCP gates read it
to decide whether the caller is a bound terminal, gated on its recorded
``allowed_tools``, or an operator context that is not gated at all. A provider
that lets a profile choose the value therefore lets a profile choose its own
authorization posture -- ``env: {CAO_TERMINAL_ID: <another terminal's id>}``
starts CAO's own MCP server as a VALID FOREIGN terminal and both gates authorize
against *that* terminal's allowlist.

This was found and fixed one writer at a time, twice. Round 1 of #1062 converted
four (claude_code, cursor_cli, omp, kimi_cli); independent review then found
three more still preserving a pinned value (copilot_cli, codex,
kimi_runtime_home) and demonstrated the borrowed-allowlist bypass through each.
Enumerating writers by hand is what let the second three survive the first fix,
so this test ENUMERATES THEM FROM THE SOURCE the way
``test_launch_path_invariant`` and ``test_callsite_lint_rules`` enumerate theirs.

Two kinds of site are found under ``providers/`` and ``backends/``:

* an **identity write** -- ``CAO_TERMINAL_ID`` (or ``CAO_TERMINAL_ID_ENV``) used
  as a dict key, a ``setdefault``/``pop`` key, or the left side of a
  ``KEY=value`` env string;
* an **MCP config writer** -- a function calling ``resolve_mcp_server_config`` or
  ``resolve_cao_mcp_command``, which is what every provider does to build an MCP
  server's launch entry.

A site is CLEAN when its enclosing function calls
``mcp_resolution.forward_terminal_identity``, the one helper that makes the
launching terminal's own id win and logs a profile that tried to pin one. Every
other site must appear in ``ALLOWED`` below with a one-line reason. A new writer
fails this test until it forwards or is classified here, and a classified site
that disappears fails it too, so the list cannot go stale.

This is a structural guard over the SHAPE of every site. The behavioural
guarantee -- that a pinned ``''``/``other-id``/``deadbeef`` really is replaced by
the launching terminal's id in each provider's real output -- is
``test/providers/test_terminal_identity_forwarding.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Optional, Set

SRC = Path(__file__).resolve().parents[1] / "src" / "cli_agent_orchestrator"
PACKAGES = (SRC / "providers", SRC / "backends")

#: The env var itself, and the module constant that names it.
IDENTITY_NAMES = {"CAO_TERMINAL_ID", "CAO_TERMINAL_ID_ENV"}

#: Calling either of these is what makes a function an MCP-config writer.
MCP_WRITER_CALLS = {"resolve_mcp_server_config", "resolve_cao_mcp_command"}

#: The one helper that settles the identity.
FORWARDER = "forward_terminal_identity"

#: Methods that WRITE or REMOVE under a dict key. A plain ``env.get(KEY)`` read
#: is not a write and is not a guard-on-absence, so it is not a site -- including
#: it classified antigravity's ``_is_pinned_terminal_entry`` detector as a writer.
KEYED_METHODS = {"setdefault", "pop"}

#: Sites that may write or omit the identity without the forwarder, each with the
#: reason. Key = ``<path under cli_agent_orchestrator/>::<enclosing function>``.
ALLOWED: Dict[str, str] = {
    "providers/antigravity_cli.py::_register_mcp_servers": (
        "stricter rule by design (#940/#958): agy's mcp_config.json is SHARED by "
        "every terminal, so the key is DROPPED and inherited from each agy "
        "process instead of pinned to any one terminal"
    ),
    "providers/kimi_cli.py::_build_kimi_code_command": (
        "exports the REAL id on the launch line (`env CAO_TERMINAL_ID=<self>`) "
        "for Kimi Code's children to inherit; the per-server override lives in "
        "kimi_runtime_home.merge_mcp_servers, which this hands its terminal id to"
    ),
    "backends/herdr_backend.py::_build_env_args": (
        "assigns the REAL terminal id into the pane's own `--env` args LAST, so "
        "an operator-forwarded CAO_TERMINAL_ID cannot win; this is the pane "
        "environment, not an MCP server entry"
    ),
    "providers/minimax_code.py::_serialize_server": (
        "a staticmethod that assembles one entry's env (including PATH for an "
        "absolute command); the identity is written by `_write_plugin`, which "
        "forwards every stdio entry it serialized"
    ),
}

#: Sites that MUST be found, so a broken walker cannot pass vacuously: one of
#: each kind, and one of each disposition.
SENTINELS = {
    "providers/antigravity_cli.py::_register_mcp_servers",
    "providers/claude_code.py::_build_claude_command",
    "providers/codex.py::_build_codex_command",
    "providers/copilot_cli.py::_build_runtime_mcp_config",
    "providers/cursor_cli.py::_write_plugin_dir",
    "providers/grok_cli.py::_render_mcp_config",
    "providers/kimi_cli.py::_build_kimi_code_command",
    "providers/kimi_cli.py::_build_kimi_command",
    "providers/kimi_runtime_home.py::merge_mcp_servers",
    "providers/minimax_code.py::_serialize_server",
    "providers/minimax_code.py::_write_plugin",
    "providers/omp.py::_write_extension_root",
    "backends/herdr_backend.py::_build_env_args",
}


def _callee(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _names_identity(node: ast.AST) -> bool:
    """Does this expression name ``CAO_TERMINAL_ID`` as a value or a constant?"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value in IDENTITY_NAMES
    if isinstance(node, ast.Name):
        return node.id in IDENTITY_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr in IDENTITY_NAMES
    return False


def _identity_write(node: ast.AST) -> Optional[str]:
    """Classify one node as an identity WRITE, or return None.

    Deliberately NOT a bare "the text CAO_TERMINAL_ID appears here" check: the
    name shows up in prose comments and docstrings all over these modules, and a
    presence test would classify a dozen innocent functions as writers and go
    stale on the next doc edit.

    Only writes count, never reads. ``if "CAO_TERMINAL_ID" not in env:`` was the
    bypass's outer shape, but it is the ``env[...] = ...`` it guards that does
    the damage, and that is a subscript store below -- whereas reads are also how
    antigravity DETECTS a pinned entry and how Codex checks its ``env_vars``
    inheritance list, both legitimate.
    """
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for target in targets:
            if isinstance(target, ast.Subscript) and _names_identity(target.slice):
                return "subscript store"
        if node.value is not None and isinstance(node.value, ast.Dict):
            if any(_names_identity(key) for key in node.value.keys if key is not None):
                return "dict literal"
    if isinstance(node, ast.Dict):
        if any(_names_identity(key) for key in node.keys if key is not None):
            return "dict literal"
    if isinstance(node, ast.Call):
        name = _callee(node)
        if name in KEYED_METHODS and node.args and _names_identity(node.args[0]):
            return f"{name}() key"
    if isinstance(node, ast.JoinedStr):
        rendered = "".join(
            str(part.value) for part in node.values if isinstance(part, ast.Constant)
        )
        if any(f"{name}=" in rendered for name in IDENTITY_NAMES):
            return "env string"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if any(f"{name}=" in node.value for name in IDENTITY_NAMES):
            return "env string"
    return None


def _owner_map(tree: ast.Module) -> Dict[int, str]:
    """Every node's nearest enclosing function name (``<module>`` if none)."""
    owner: Dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = (
                child.name
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                else current
            )
            owner[id(child)] = inner
            visit(child, inner)

    visit(tree, "<module>")
    return owner


def identity_sites() -> Dict[str, dict]:
    """Every identity-write and MCP-writer site, keyed ``<path>::<function>``."""
    sites: Dict[str, dict] = {}
    for package in PACKAGES:
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            owner = _owner_map(tree)
            rel = str(path.relative_to(SRC))
            forwarding: Set[str] = {
                owner.get(id(node), "<module>")
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and _callee(node) == FORWARDER
            }
            found: Dict[str, dict] = {}
            for node in ast.walk(tree):
                kind = _identity_write(node)
                is_write = kind is not None
                if kind is None and isinstance(node, ast.Call):
                    called = _callee(node)
                    # A forwarding call is itself a site, so every function that
                    # settles the identity is enumerated and SENTINELS can pin
                    # one that would otherwise vanish from the walk.
                    if called in MCP_WRITER_CALLS or called == FORWARDER:
                        kind = f"{called}()"
                if kind is None:
                    continue
                function = owner.get(id(node), "<module>")
                entry = found.setdefault(
                    f"{rel}::{function}",
                    {
                        "kinds": set(),
                        "writes": set(),
                        "line": getattr(node, "lineno", 0),
                        "forwards": function in forwarding,
                    },
                )
                entry["kinds"].add(kind)
                if is_write:
                    entry["writes"].add(kind)
                entry["line"] = min(entry["line"], getattr(node, "lineno", entry["line"]))
            sites.update(found)
    return sites


def _is_clean(info: dict) -> bool:
    """Clean = the forwarder settles the identity and nothing else touches it.

    A hand-rolled write is NOT excused by a forwarding call elsewhere in the same
    function. Function-level granularity was the first version of this guard and
    it was too coarse: reintroducing the ``env.setdefault`` bypass into
    ``copilot_cli._build_runtime_mcp_config`` still passed, because the same
    function also forwards for CAO's own built-in entry. Nothing but
    ``forward_terminal_identity`` decides this value.
    """
    return not info["writes"] and info["forwards"]


SITES = identity_sites()


def test_the_walker_finds_the_known_sites():
    missing = SENTINELS - set(SITES)
    assert not missing, f"walker lost known MCP identity sites: {sorted(missing)}"
    assert len(SITES) >= 12, sorted(SITES)


def test_every_mcp_identity_writer_forwards_the_real_terminal_id():
    unclassified = sorted(
        f"{key} (line {info['line']}: {', '.join(sorted(info['kinds']))})"
        for key, info in SITES.items()
        if not _is_clean(info) and key not in ALLOWED
    )
    assert not unclassified, (
        "MCP server env writer(s) that either hand-roll the CAO_TERMINAL_ID "
        f"write or never call mcp_resolution.{FORWARDER}, and are not classified "
        "in ALLOWED. A profile that pins CAO_TERMINAL_ID here chooses the "
        "authorization identity CAO's own MCP server runs under (#1062):\n  "
        + "\n  ".join(unclassified)
    )


def test_the_allowlist_is_real_and_still_needed():
    stale = sorted(key for key in ALLOWED if key not in SITES)
    assert not stale, f"ALLOWED names sites that no longer exist: {stale}"
    now_forwarding = sorted(key for key in ALLOWED if _is_clean(SITES[key]))
    assert not now_forwarding, (
        "these sites now forward the identity; drop them from ALLOWED so the "
        f"classification stays honest: {now_forwarding}"
    )
    for key, why in ALLOWED.items():
        assert len(why.split()) >= 8, f"{key}: justification too short"
