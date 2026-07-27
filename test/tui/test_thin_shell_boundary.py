"""Guard test: the ``cao tui`` package must stay a thin shell.

Every module under ``src/cli_agent_orchestrator/tui/`` composes the existing
``cao`` CLI surface and reads live state exclusively over HTTP. It may import
only the standard library, ``prompt_toolkit``, ``requests``,
``cli_agent_orchestrator.constants`` and
``cli_agent_orchestrator.utils.path_validation``. It must NEVER import the heavy
in-process layers (``services``, ``clients``, ``backends``, ``providers``,
``models``) or the ``cli`` command modules / ``cli`` object.

Why this matters (the chain the guard defends against): importing
``cli_agent_orchestrator.cli.main`` pulls in ``cli.commands.launch``, which at
module top imports ``backends.registry`` (``get_backend``) and
``services.settings_service`` (``get_server_settings``) — the exact heavy
dependency graph a thin shell must not drag in (ADR-007). This test AST-scans
every ``tui`` module and fails on any forbidden import, locking the NFR-2 / SC-2
invariant. Modeled on ``test/test_http_only_boundary.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import cli_agent_orchestrator

# Layer 1 — forbidden import targets, fully qualified. Both the dotted path and
# any submodule of it (e.g. ``services.settings_service.helpers``) are rejected.
FORBIDDEN_MODULES = (
    "cli_agent_orchestrator.services",
    "cli_agent_orchestrator.clients",
    "cli_agent_orchestrator.backends",
    "cli_agent_orchestrator.providers",
    "cli_agent_orchestrator.models",
    "cli_agent_orchestrator.cli",
)

# Layer 2 — bare suffixes, to also catch relative or partially-qualified imports
# such as ``from ..services import settings_service`` or ``import backends.registry``.
# Includes representative specific targets plus the bare package tails.
FORBIDDEN_SUFFIXES = (
    "services.settings_service",
    "backends.registry",
    "clients.tmux",
    "clients.database",
    "services",
    "clients",
    "backends",
    "providers",
    "models",
    # bare `cli` tail: catches `from .. import cli` / `from ..cli import ...`,
    # whose AST base is just `cli` (Layer-1 only sees the fully-qualified form).
    # Added per U1 architecture-review advisory so the relative cli import is
    # also blocked — the cli object transitively pulls backends+services (ADR-007).
    "cli",
)

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_TUI_DIR = _PACKAGE_ROOT / "tui"


def _tui_modules() -> List[Path]:
    """Return every Python module under the tui package."""

    return sorted(_TUI_DIR.rglob("*.py"))


def _imported_targets(tree: ast.AST) -> List[str]:
    """Collect the dotted module targets of every import in an AST."""

    targets: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # ``module`` may be None for ``from . import x``; fold the imported
            # names in so ``from ..services import settings_service`` is caught.
            base = node.module or ""
            targets.append(base)
            for alias in node.names:
                targets.append(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _is_forbidden(target: str) -> bool:
    """True if an import target names a forbidden heavy layer or cli module."""

    if target in FORBIDDEN_MODULES:
        return True
    if any(target.startswith(f"{m}.") for m in FORBIDDEN_MODULES):
        return True
    return any(target == s or target.endswith(f".{s}") for s in FORBIDDEN_SUFFIXES)


def test_tui_package_exists() -> None:
    """Sanity: the scan target directory is present and non-empty.

    Guards against an empty-glob false pass — if ``tui/`` disappeared or held no
    modules, the boundary test below would vacuously pass.
    """

    assert _TUI_DIR.is_dir()
    assert _tui_modules(), "no tui modules found to scan"


def test_tui_imports_are_thin_shell() -> None:
    """No module under tui/ may import a forbidden heavy layer or cli module."""

    violations: List[str] = []
    for module_path in _tui_modules():
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        for target in _imported_targets(tree):
            if _is_forbidden(target):
                rel = module_path.relative_to(_PACKAGE_ROOT)
                violations.append(f"{rel} imports forbidden module '{target}'")

    assert not violations, "thin-shell boundary violated:\n" + "\n".join(violations)
