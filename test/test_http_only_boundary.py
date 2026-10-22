"""Guard test: the MCP boundary must stay HTTP-only.

Every module under ``src/cli_agent_orchestrator/mcp_server/`` reaches Backplane
state exclusively through the FastAPI REST/SSE surface over HTTP (or through
process-local read-only services). It must never import the state clients
``clients.tmux`` or ``clients.database`` directly. This test AST-scans every
``mcp_server`` module and fails if any forbidden import is present, locking the
invariant the codebase already satisfies (Requirement 7; Correctness Property 9).

Scanning only for the state *clients* is not enough. A tool that instantiates a
state *service* -- which opens those clients on its behalf -- passes a
clients-only check while reaching the database anyway (issue #744:
``MemoryService()`` inside ``store_lesson``). So the scan also rejects the
service modules that open a state client themselves. ``FORBIDDEN_STATE_SERVICES``
is derived by AST-scanning every module under ``services/`` for a direct
``clients.tmux``/``clients.database`` import, rather than hand-picked, so a
tool that reaches for a state service ``mcp_server/`` has never imported
before is still caught; see ``_derive_forbidden_state_services`` and the two
pinned allowlists below.

Depth is deliberately ONE hop. ``services.settings_service`` imports
``services.vault.config``, which imports ``clients.database``, so a transitive
rule would flag nearly every ``mcp_server`` import -- including
``mcp_server.utils`` itself -- and would contradict the "process-local
read-only services" carve-out above. The rule is therefore: an ``mcp_server``
module may not import a module that *itself* names a state client.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

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

# Name used for imports that sit at module scope rather than inside a function.
MODULE_SCOPE = "<module>"

# Prose reasons for the four state services ``mcp_server/`` actually imports
# today. Kept as documentation only -- ``FORBIDDEN_STATE_SERVICES`` below is
# derived by scanning, so a service that starts getting imported by
# ``mcp_server/`` is caught even without an entry here; add one anyway when
# that happens, to keep the failure message specific.
_KNOWN_STATE_SERVICE_REASONS: Dict[str, str] = {
    # Writes memory metadata rows and wiki files: SessionLocal,
    # MemoryMetadataModel, record_project_alias (memory_service.py:299, :391,
    # :433, :493, :1150).
    "cli_agent_orchestrator.services.memory_service": (
        "opens clients.database (SessionLocal, MemoryMetadataModel) to read and "
        "write memory rows"
    ),
    # Reads/writes WorkflowOutcomeModel rows (outcome_service.py:89, :131, :175).
    "cli_agent_orchestrator.services.outcome_service": (
        "opens clients.database (SessionLocal, WorkflowOutcomeModel) for workflow " "outcome rows"
    ),
    # Module-scope ``from cli_agent_orchestrator.clients import database``
    # (ephemeral_service.py:22), so merely importing it binds the state client
    # into the MCP process.
    "cli_agent_orchestrator.services.ephemeral_service": (
        "imports clients.database at module scope (ephemeral_service.py:22)"
    ),
    # Module-scope import of MemoryMetadataModel, SessionLocal, TerminalModel,
    # VaultNoteModel, VaultRecallCounterModel (vault/reader.py:15-21).
    "cli_agent_orchestrator.services.vault.reader": (
        "imports clients.database models at module scope (vault/reader.py:15-21)"
    ),
}

# --------------------------------------------------------------------------
# Pinned exceptions.
#
# Both maps are keyed by ``(module relative to the package root, enclosing
# function or <module>, forbidden module)``. Keying on the enclosing function
# rather than a line number means the pin survives edits above it but still
# expires if the import moves to a different tool.
#
# Keep these EXACT: `test_pinned_allowlists_are_exact` fails if an entry stops
# matching a real import, so a fixed tool must be removed from the map in the
# same change.
# --------------------------------------------------------------------------

# Legitimate: the tool picks the HTTP path when a remote Backplane is
# configured and only falls back to the in-process service for a local,
# single-host install. The remote fallback is
# ``services.memory_gateway`` (``remote_memory_url()`` gate + ``store_memory`` /
# ``recall_memory`` / ``forget_memory``, which POST to the
# ``/internal/memory/{store,recall,forget}`` routes on the remote Backplane).
#
# Caveat worth knowing: ``memory_gateway`` itself imports ``MemoryService`` at
# module scope (memory_gateway.py:15-19), so even the remote path still binds
# ``clients.database`` into the MCP process. The gate proves intent, not
# physical isolation; see the note in the PR for #1005.
DUAL_MODE_ALLOWLIST: Dict[Tuple[str, str, str], str] = {
    (
        "mcp_server/server.py",
        MODULE_SCOPE,
        "cli_agent_orchestrator.services.memory_service",
    ): (
        "constants and exception types only (MEMORY_DISABLED_MESSAGE, "
        "MemoryDisabledError, MemoryPartialWriteError) -- no client is opened"
    ),
    (
        "mcp_server/server.py",
        MODULE_SCOPE,
        "cli_agent_orchestrator.services.outcome_service",
    ): (
        "one constant only (LEARNING_DISABLED_CODE); the report_outcome and "
        "store_lesson write paths are HTTP (POST /outcomes, POST /lessons)"
    ),
    (
        "mcp_server/server.py",
        "memory_store",
        "cli_agent_orchestrator.services.memory_service",
    ): (
        "dual-mode: remote fallback is memory_gateway.store_memory " "(POST /internal/memory/store)"
    ),
    (
        "mcp_server/server.py",
        "memory_recall",
        "cli_agent_orchestrator.services.memory_service",
    ): (
        "dual-mode: remote fallback is memory_gateway.recall_memory "
        "(POST /internal/memory/recall)"
    ),
    (
        "mcp_server/server.py",
        "memory_forget",
        "cli_agent_orchestrator.services.memory_service",
    ): (
        "dual-mode: remote fallback is memory_gateway.forget_memory "
        "(POST /internal/memory/forget)"
    ),
    (
        "mcp_server/server.py",
        "memory_recall",
        "cli_agent_orchestrator.services.vault.reader",
    ): "MEMORY_MANAGER_PROFILE, a str constant (vault/reader.py:37) -- no client is opened",
    (
        "mcp_server/server.py",
        "assign_elastic",
        "cli_agent_orchestrator.services.ephemeral_service",
    ): "log_refusal only: a logger.warning with no state access (ephemeral_service.py:152)",
    (
        "mcp_server/server.py",
        "_create_ephemeral_from_arguments",
        "cli_agent_orchestrator.services.ephemeral_service",
    ): "log_refusal only: a logger.warning with no state access (ephemeral_service.py:152)",
}

# NOT legitimate: entries here reach the in-process service with no HTTP path
# at all, so against a remote Backplane they write to the wrong database. Pin
# here only so the gate can ship green while the product fix lands separately,
# and name the tracking issue in the reason.
#
# Deleting an entry is necessary for its issue to close, not sufficient: this
# scan sees imports, not every in-process path. The #744 pin (store_lesson
# importing MemoryService) was removed when #996 moved store_lesson to
# ``POST /lessons``; the in-process ``is_learning_enabled()`` gate that #996
# also removed was never visible to this scan, because ``settings_service``
# does not import a state client.
KNOWN_GAP_ALLOWLIST: Dict[Tuple[str, str, str], str] = {}

ALLOWLIST: Dict[Tuple[str, str, str], str] = {**DUAL_MODE_ALLOWLIST, **KNOWN_GAP_ALLOWLIST}

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_MCP_SERVER_DIR = _PACKAGE_ROOT / "mcp_server"
_SERVICES_DIR = _PACKAGE_ROOT / "services"

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _mcp_server_modules() -> List[Path]:
    """Return every Python module under the mcp_server package."""

    return sorted(_MCP_SERVER_DIR.rglob("*.py"))


def _service_modules() -> List[Path]:
    """Return every Python module under the services package."""

    return sorted(_SERVICES_DIR.rglob("*.py"))


def _module_name(path: Path) -> str:
    """Dotted module name for a path under the package root."""

    rel = path.relative_to(_PACKAGE_ROOT).with_suffix("")
    return "cli_agent_orchestrator." + ".".join(rel.parts)


def _scoped_imports(tree: ast.AST, scope: str = MODULE_SCOPE) -> Iterator[Tuple[str, int, str]]:
    """Yield ``(enclosing function, line, dotted target)`` for every import.

    One walker, not two: this recurses the tree itself (rather than
    ``ast.walk``) purely so each import carries the name of the function it sits
    in -- ``ast.walk`` flattens the tree and loses that. Function *bodies* are
    still visited, exactly as ``ast.walk`` visited them before, which is what
    catches the deferred ``from ... import MemoryService`` inside a tool.
    """

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, _FUNCTION_NODES):
            # Nested defs keep the outermost tool name: that is the unit an
            # allowlist entry is about.
            inner_scope = node.name if scope == MODULE_SCOPE else scope
            yield from _scoped_imports(node, inner_scope)
            continue
        if isinstance(node, ast.ClassDef):
            yield from _scoped_imports(node, node.name if scope == MODULE_SCOPE else scope)
            continue
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield scope, node.lineno, alias.name
            continue
        if isinstance(node, ast.ImportFrom):
            # ``module`` may be None for ``from . import x``; fold the imported
            # names in so ``from ..clients import tmux`` is still caught.
            base = node.module or ""
            yield scope, node.lineno, base
            for alias in node.names:
                yield scope, node.lineno, (f"{base}.{alias.name}" if base else alias.name)
            continue
        yield from _scoped_imports(node, scope)


def _imports_state_client_directly(target: str) -> bool:
    """True if an import target names clients.tmux/clients.database itself.

    Used only to derive ``FORBIDDEN_STATE_SERVICES`` below -- deliberately
    checks against ``FORBIDDEN_MODULES``/``FORBIDDEN_SUFFIXES`` only, not the
    (not-yet-built) state-service set, so the derivation stays one hop deep:
    a service is forbidden because it names a client itself, not because it
    imports another forbidden service.
    """

    for module in FORBIDDEN_MODULES:
        if target == module or target.startswith(f"{module}."):
            return True
    for suffix in FORBIDDEN_SUFFIXES:
        if target == suffix or target.endswith(f".{suffix}"):
            return True
    return False


def _derive_forbidden_state_services() -> Dict[str, str]:
    """AST-scan services/ for every module that opens a state client itself.

    An mcp_server module importing one of these has the same non-HTTP path to
    the database as importing the client directly (the gap #744 found in
    ``store_lesson``). Scanning finds every such service, not just the ones
    mcp_server/ happens to import today, so the next tool that reaches for
    one of the other 21 is caught without editing this file.
    """

    derived: Dict[str, str] = {}
    for module_path in _service_modules():
        name = _module_name(module_path)
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        if any(_imports_state_client_directly(target) for _, _, target in _scoped_imports(tree)):
            derived[name] = _KNOWN_STATE_SERVICE_REASONS.get(
                name, "opens clients.tmux/clients.database directly"
            )
    return derived


FORBIDDEN_STATE_SERVICES: Dict[str, str] = _derive_forbidden_state_services()


def _all_forbidden() -> Tuple[str, ...]:
    return tuple(FORBIDDEN_MODULES) + tuple(FORBIDDEN_STATE_SERVICES)


def _forbidden_root(target: str) -> Optional[str]:
    """Return the forbidden module an import target names, else None."""

    for module in _all_forbidden():
        if target == module or target.startswith(f"{module}."):
            return module
    for suffix in FORBIDDEN_SUFFIXES:
        if target == suffix or target.endswith(f".{suffix}"):
            return suffix
    return None


def _is_forbidden(target: str) -> bool:
    """True if an import target names a forbidden state client or state service."""

    return _forbidden_root(target) is not None


def _findings() -> Dict[Tuple[str, str, str], int]:
    """Every forbidden import in mcp_server/, keyed by (module, scope, root).

    The value is the first line the import was seen on, for the failure
    message. One ``from X import A, B`` yields a single finding: the key is the
    forbidden module, not the raw target.
    """

    found: Dict[Tuple[str, str, str], int] = {}
    for module_path in _mcp_server_modules():
        rel = module_path.relative_to(_PACKAGE_ROOT).as_posix()
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        for scope, lineno, target in _scoped_imports(tree):
            root = _forbidden_root(target)
            if root is None:
                continue
            found.setdefault((rel, scope, root), lineno)
    return found


def test_mcp_server_package_exists() -> None:
    """Sanity: the scan target directory is present and non-empty."""

    assert _MCP_SERVER_DIR.is_dir()
    assert _mcp_server_modules(), "no mcp_server modules found to scan"


def test_mcp_server_modules_do_not_import_state_clients() -> None:
    """No module under mcp_server/ may import clients.tmux or clients.database."""

    violations: List[str] = []
    for (rel, scope, root), lineno in sorted(_findings().items()):
        if root not in FORBIDDEN_MODULES and root not in FORBIDDEN_SUFFIXES:
            continue
        violations.append(f"{rel}:{lineno} ({scope}) imports forbidden module '{root}'")

    assert not violations, "HTTP-only boundary violated:\n" + "\n".join(violations)


def test_mcp_server_modules_do_not_open_state_services() -> None:
    """No module under mcp_server/ may import a state service unless pinned.

    A state service opens ``clients.database`` on the caller's behalf, so
    importing one is the same boundary break as importing the client -- the
    gap #744 found in ``store_lesson``. Pinned exceptions live in
    ``DUAL_MODE_ALLOWLIST`` (HTTP-first, local fallback) and
    ``KNOWN_GAP_ALLOWLIST`` (tracked, not yet fixed).
    """

    violations: List[str] = []
    for (rel, scope, root), lineno in sorted(_findings().items()):
        if root not in FORBIDDEN_STATE_SERVICES:
            continue
        if (rel, scope, root) in ALLOWLIST:
            continue
        violations.append(
            f"{rel}:{lineno} ({scope}) imports state service '{root}' "
            f"-- {FORBIDDEN_STATE_SERVICES[root]}. "
            "Route it through the FastAPI surface, or pin it in "
            "DUAL_MODE_ALLOWLIST / KNOWN_GAP_ALLOWLIST with a reason."
        )

    assert not violations, "HTTP-only boundary violated (state service):\n" + "\n".join(violations)


def test_pinned_allowlists_are_exact() -> None:
    """Every pinned exception must still match a real import.

    Without this, a fixed tool leaves a stale pin behind that silently
    re-permits the next regression in the same function.
    """

    found = set(_findings())
    stale = sorted(key for key in ALLOWLIST if key not in found)
    assert not stale, (
        "stale allowlist entries in test_http_only_boundary.py -- these imports no "
        "longer exist, delete the pins:\n" + "\n".join(f"  {k}" for k in stale)
    )


def test_walker_sees_function_scoped_imports() -> None:
    """The walker must reach imports deferred inside a function body.

    This is the shape #744 hid behind: the state service is imported at the top
    of the tool, not the top of the module. Pinned so a future refactor of the
    walker cannot quietly stop looking inside functions.
    """

    tree = ast.parse(
        "import os\n"
        "\n"
        "async def store_lesson():\n"
        "    from cli_agent_orchestrator.services.memory_service import MemoryService\n"
        "    if True:\n"
        "        from cli_agent_orchestrator.clients import database\n"
        "    return MemoryService(), database\n"
    )
    seen = {(scope, target) for scope, _, target in _scoped_imports(tree)}

    assert (MODULE_SCOPE, "os") in seen
    assert ("store_lesson", "cli_agent_orchestrator.services.memory_service") in seen
    # Nested inside an `if` inside the function: still attributed to the tool.
    assert ("store_lesson", "cli_agent_orchestrator.clients.database") in seen
    assert _forbidden_root("cli_agent_orchestrator.services.memory_service.MemoryService") == (
        "cli_agent_orchestrator.services.memory_service"
    )


def test_every_forbidden_state_service_has_a_reason() -> None:
    """Documentation gate: each forbidden service records what it opens."""

    missing = sorted(
        name for name, reason in FORBIDDEN_STATE_SERVICES.items() if not reason.strip()
    )
    assert not missing, f"FORBIDDEN_STATE_SERVICES entries without a reason: {missing}"


def test_forbidden_state_services_cover_services_not_imported_by_mcp_server() -> None:
    """The forbidden set must be derived, not limited to today's four imports.

    A hand-picked list scoped to what mcp_server/ already imports cannot catch
    the *next* tool reaching for an unrelated state service -- e.g.
    ``session_service``, which opens ``clients.database`` at module scope but
    nothing under mcp_server/ imports it today. This fails against a
    hardcoded four-entry map and passes only because the set is derived by
    scanning services/.
    """

    bypass_candidates = (
        "cli_agent_orchestrator.services.session_service",
        "cli_agent_orchestrator.services.terminal_service",
        "cli_agent_orchestrator.services.inbox_service",
        "cli_agent_orchestrator.services.approval_store",
    )
    missing = [name for name in bypass_candidates if name not in FORBIDDEN_STATE_SERVICES]
    assert not missing, f"derived FORBIDDEN_STATE_SERVICES is missing: {missing}"
    # 24 after #1031 made services/vault/config.py client-free. (P3-1 removes
    # this count in the next commit: it is a snapshot, not a contract.)
    assert len(FORBIDDEN_STATE_SERVICES) == 24
