"""HTTP-only boundary guard for src/cao_workflow/ (BR-2, security-design.md choke point 1).

DISTINCT from the pre-existing ``test/test_http_only_boundary.py`` (the
unrelated MCP-server boundary guard) — different path, different package
under test, to avoid a pytest-collection name collision. Both assert "this
component only talks HTTP," applied to different components; that file is
untouched by this unit.

Static AST walk, not a naive grep — a grep could miss a dynamic
``importlib.import_module`` call. Asserts:
1. zero import of ``cli_agent_orchestrator`` (or any submodule) anywhere
   under ``src/cao_workflow/``;
2. the only network primitive imported is ``urllib.request``/``urllib.error``
   (no third-party HTTP client, no other network module).
"""

from __future__ import annotations

import ast
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "cao_workflow"

_DISALLOWED_NETWORK_ROOTS = {
    "requests",
    "httpx",
    "aiohttp",
    "http.client",
    "socket",
}


def _module_files() -> "list[Path]":
    files = sorted(_PACKAGE_ROOT.rglob("*.py"))
    assert files, f"expected .py files under {_PACKAGE_ROOT}, found none"
    return files


def _imported_module_names(tree: ast.AST) -> "set[str]":
    names: "set[str]" = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module)
        elif isinstance(node, ast.Call):
            # Catch dynamic `importlib.import_module("...")` calls too.
            func = node.func
            is_import_module = (
                isinstance(func, ast.Attribute) and func.attr == "import_module"
            ) or (isinstance(func, ast.Name) and func.id == "import_module")
            if is_import_module and node.args and isinstance(node.args[0], ast.Constant):
                if isinstance(node.args[0].value, str):
                    names.add(node.args[0].value)
    return names


def test_no_cli_agent_orchestrator_imports():
    violations = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported_module_names(tree):
            if name == "cli_agent_orchestrator" or name.startswith("cli_agent_orchestrator."):
                violations.append((path, name))
    assert not violations, (
        "src/cao_workflow/ must import nothing from cli_agent_orchestrator "
        f"(BR-2, HTTP-only boundary); violations: {violations}"
    )


def test_only_urllib_used_for_network():
    violations = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported_module_names(tree):
            root = name.split(".")[0]
            if root in _DISALLOWED_NETWORK_ROOTS or name in _DISALLOWED_NETWORK_ROOTS:
                violations.append((path, name))
    assert not violations, (
        "src/cao_workflow/ must use only urllib for HTTP transport (BR-8), "
        f"no third-party/other network client; violations: {violations}"
    )


def test_no_third_party_imports():
    """Belt-and-braces with BR-8: only stdlib + intra-package imports allowed."""
    stdlib_or_local_prefixes = {
        "cao_workflow",
        "dataclasses",
        "json",
        "os",
        "threading",
        "typing",
        "urllib",
        "__future__",
    }
    violations = []
    for path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for name in _imported_module_names(tree):
            root = name.split(".")[0]
            if root not in stdlib_or_local_prefixes:
                violations.append((path, name))
    assert not violations, f"unexpected non-stdlib import under src/cao_workflow/: {violations}"
