"""Guard test: the MCP boundary must stay HTTP-only.

Every module under ``src/cli_agent_orchestrator/mcp_server/`` reaches Backplane
state exclusively through the FastAPI REST/SSE surface over HTTP (or through
process-local read-only services). It must never import the state clients
``clients.tmux`` or ``clients.database`` directly. This test AST-scans every
``mcp_server`` module and fails if any forbidden import is present, locking the
invariant the codebase already satisfies (Requirement 7; Correctness Property 9).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import cli_agent_orchestrator

# Forbidden import targets. Both the dotted module path and any submodule of it
# (e.g. ``clients.database.terminals``) are rejected.
FORBIDDEN_MODULES = (
    "cli_agent_orchestrator.clients.tmux",
    "cli_agent_orchestrator.clients.database",
)

# Bare suffixes used to also catch relative or partially-qualified imports such
# as ``from ..clients import tmux`` or ``import clients.database``.
FORBIDDEN_SUFFIXES = ("clients.tmux", "clients.database")

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_MCP_SERVER_DIR = _PACKAGE_ROOT / "mcp_server"


def _mcp_server_modules() -> List[Path]:
    """Return every Python module under the mcp_server package."""

    return sorted(_MCP_SERVER_DIR.rglob("*.py"))


def _imported_targets(tree: ast.AST) -> List[str]:
    """Collect the dotted module targets of every import in an AST."""

    targets: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # ``module`` may be None for ``from . import x``; fold the imported
            # names in so ``from ..clients import tmux`` is still caught.
            base = node.module or ""
            targets.append(base)
            for alias in node.names:
                targets.append(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _is_forbidden(target: str) -> bool:
    """True if an import target names a forbidden state client."""

    if target in FORBIDDEN_MODULES:
        return True
    if any(target.startswith(f"{m}.") for m in FORBIDDEN_MODULES):
        return True
    return any(target == s or target.endswith(f".{s}") for s in FORBIDDEN_SUFFIXES)


def test_mcp_server_package_exists() -> None:
    """Sanity: the scan target directory is present and non-empty."""

    assert _MCP_SERVER_DIR.is_dir()
    assert _mcp_server_modules(), "no mcp_server modules found to scan"


def test_mcp_server_modules_do_not_import_state_clients() -> None:
    """No module under mcp_server/ may import clients.tmux or clients.database."""

    violations: List[str] = []
    for module_path in _mcp_server_modules():
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        for target in _imported_targets(tree):
            if _is_forbidden(target):
                rel = module_path.relative_to(_PACKAGE_ROOT)
                violations.append(f"{rel} imports forbidden module '{target}'")

    assert not violations, "HTTP-only boundary violated:\n" + "\n".join(violations)
