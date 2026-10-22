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
``MemoryService()`` inside ``store_lesson``). So the scan also rejects every
module that opens a state client itself. ``FORBIDDEN_STATE_MODULES`` is derived
by AST-scanning the whole package -- except ``clients/`` (the clients
themselves) and ``mcp_server/`` (the scanned side) -- for a direct
``clients.tmux``/``clients.database`` import, rather than hand-picked, so a
tool that reaches for a state module ``mcp_server/`` has never imported
before is still caught, whether it lives under ``services/``, ``decisions/``,
``providers/`` or ``backends/``; see ``_derive_forbidden_state_modules`` and
the two pinned allowlists below.

Pins are NAME-aware. A pin names the module, the enclosing function and the
exact names that may be imported from it. ``from memory_service import
MEMORY_DISABLED_MESSAGE`` and ``from memory_service import MemoryService`` are
different imports to this guard even when they sit in the same ``from``
block, so adding ``MemoryService`` to an already-pinned module-scope block is
a finding, not a free pass. A plain ``import pkg.memory_service`` binds every
name in the module and can never be pinned.

Depth is deliberately ONE hop. ``services.memory_gateway`` -- the HTTP path the
dual-mode tools fall back to -- imports ``MemoryService`` at module scope, so a
transitive rule would forbid the very module that keeps those tools HTTP-first,
and would flag nearly every ``mcp_server`` import along with it, contradicting
the "process-local read-only services" carve-out above. The rule is therefore:
an ``mcp_server`` module may not import a module that *itself* names a state
client.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import (
    Dict,
    FrozenSet,
    Iterable,
    Iterator,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Tuple,
)

import pytest

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

# Name recorded when an import binds a whole module rather than named members:
# ``import pkg.mod``, ``import pkg.mod as m`` or ``from pkg import mod``. Every
# attribute of the module is reachable through that binding, so no pin may list
# it (``test_no_pin_allows_the_whole_module``).
WHOLE_MODULE = "*"

# Prose reasons for the four state modules ``mcp_server/`` actually imports
# today. Documentation only: ``FORBIDDEN_STATE_MODULES`` below is derived by
# scanning, so a module that starts getting imported by ``mcp_server/`` is
# caught even without an entry here (it gets the generic reason); add one
# when that happens, to keep the failure message specific. The one thing
# this map is held to is accuracy: ``test_known_reasons_name_derived_modules``
# fails if an entry names a module the scan no longer derives, so a reason
# cannot outlive the import it describes.
_KNOWN_STATE_MODULE_REASONS: Dict[str, str] = {
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


class Pin(NamedTuple):
    """One allowed import of a forbidden state module.

    ``names`` is the exact set of names the pinned statement may bind. The
    exactness test fails if the real import binds fewer names (the pin is
    over-broad: trim it) and the service test fails if it binds more (a new
    name rode in on an existing block: route it through HTTP or pin it on
    purpose).
    """

    names: Tuple[str, ...]
    reason: str


# Key shape shared by findings and pins:
# (module relative to the package root, enclosing function or <module>,
#  forbidden module).
PinKey = Tuple[str, str, str]

# --------------------------------------------------------------------------
# Pinned exceptions.
#
# Both maps are keyed by ``(module relative to the package root, enclosing
# function or <module>, forbidden module)``. Keying on the enclosing function
# rather than a line number means the pin survives edits above it but still
# expires if the import moves to a different tool. The pin's ``names`` must
# match the real import exactly, in both directions.
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
# Caveat worth knowing: every import of ``memory_service`` -- the two
# module-scope constant imports below included -- binds ``clients.database``
# into the MCP process at import time, through memory_service.py:30 ->
# services/vault/binding.py:11 -> clients/database.py, whose module body runs
# ``_ensure_db_dir()`` and ``create_engine`` (database.py:756-757).
# ``memory_gateway`` does the same (memory_gateway.py:15-19). "No client is
# used" below therefore means no client is *called* through that import, not
# that the process is isolated from it. Moving the constants and exception
# types to a client-free module would remove the link and both module-scope
# pins; see the note in the PR for #1005.
DUAL_MODE_ALLOWLIST: Dict[PinKey, Pin] = {
    (
        "mcp_server/server.py",
        MODULE_SCOPE,
        "cli_agent_orchestrator.services.memory_service",
    ): Pin(
        ("MEMORY_DISABLED_MESSAGE", "MemoryDisabledError", "MemoryPartialWriteError"),
        "a message constant and two exception types -- no client is used "
        "through these names (importing the module still binds clients.database; "
        "see the caveat above)",
    ),
    (
        "mcp_server/server.py",
        MODULE_SCOPE,
        "cli_agent_orchestrator.services.outcome_service",
    ): Pin(
        ("LEARNING_DISABLED_CODE",),
        "one constant only; the report_outcome and store_lesson write paths are "
        "HTTP (POST /outcomes, POST /lessons)",
    ),
    (
        "mcp_server/server.py",
        "memory_store",
        "cli_agent_orchestrator.services.memory_service",
    ): Pin(
        ("MemoryService",),
        "dual-mode: remote fallback is memory_gateway.store_memory "
        "(POST /internal/memory/store)",
    ),
    (
        "mcp_server/server.py",
        "memory_recall",
        "cli_agent_orchestrator.services.memory_service",
    ): Pin(
        ("MemoryService",),
        "dual-mode: remote fallback is memory_gateway.recall_memory "
        "(POST /internal/memory/recall)",
    ),
    (
        "mcp_server/server.py",
        "memory_forget",
        "cli_agent_orchestrator.services.memory_service",
    ): Pin(
        ("MemoryService",),
        "dual-mode: remote fallback is memory_gateway.forget_memory "
        "(POST /internal/memory/forget)",
    ),
    (
        "mcp_server/server.py",
        "memory_recall",
        "cli_agent_orchestrator.services.vault.reader",
    ): Pin(
        ("MEMORY_MANAGER_PROFILE",),
        "a str constant (vault/reader.py:37) -- no client is used",
    ),
    (
        "mcp_server/server.py",
        "assign_elastic",
        "cli_agent_orchestrator.services.ephemeral_service",
    ): Pin(
        ("log_refusal",),
        "a logger.warning with no state access (ephemeral_service.py:152)",
    ),
    (
        "mcp_server/server.py",
        "_create_ephemeral_from_arguments",
        "cli_agent_orchestrator.services.ephemeral_service",
    ): Pin(
        ("log_refusal",),
        "a logger.warning with no state access (ephemeral_service.py:152)",
    ),
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
KNOWN_GAP_ALLOWLIST: Dict[PinKey, Pin] = {}

ALLOWLIST: Dict[PinKey, Pin] = {**DUAL_MODE_ALLOWLIST, **KNOWN_GAP_ALLOWLIST}

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_MCP_SERVER_DIR = _PACKAGE_ROOT / "mcp_server"

# Top-level package directories the derivation does not scan: ``clients`` is
# the forbidden target itself, ``mcp_server`` is the side being guarded.
_UNSCANNED_TOP_LEVEL = frozenset({"clients", "mcp_server"})

_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


class ImportSite(NamedTuple):
    """One import statement as the walker sees it.

    ``module`` is the dotted module the statement names (the ``from`` base, or
    the alias of a plain ``import``). ``names`` is what the statement binds
    from it: the imported names for ``from X import A, B``, or
    ``(WHOLE_MODULE,)`` for a plain ``import X``.
    """

    scope: str
    lineno: int
    module: str
    names: Tuple[str, ...]

    def targets(self) -> Iterator[str]:
        """Every dotted path this statement could be naming.

        ``from pkg.clients import database`` names ``pkg.clients`` *and*
        ``pkg.clients.database``; yielding both keeps a bare ``from ..clients
        import tmux`` catchable through ``FORBIDDEN_SUFFIXES``.
        """

        yield self.module
        for name in self.names:
            if name == WHOLE_MODULE:
                continue
            yield f"{self.module}.{name}" if self.module else name


class Finding(NamedTuple):
    """A forbidden import in mcp_server/: first line seen and the names bound."""

    lineno: int
    names: FrozenSet[str]


def _mcp_server_modules() -> List[Path]:
    """Return every Python module under the mcp_server package."""

    return sorted(_MCP_SERVER_DIR.rglob("*.py"))


def _state_candidate_modules(package_root: Path = _PACKAGE_ROOT) -> List[Path]:
    """Every module that may be a state module: the package minus clients/ and mcp_server/.

    The rule is "a module that itself names a state client", wherever it
    lives: ``decisions/store.py``, ``providers/manager.py`` and
    ``backends/factory.py`` name one just as ``services/session_service.py``
    does, and a tool can import any of them.
    """

    return sorted(
        path
        for path in package_root.rglob("*.py")
        if path.relative_to(package_root).parts[0] not in _UNSCANNED_TOP_LEVEL
    )


def _module_name(path: Path, package_root: Path = _PACKAGE_ROOT) -> str:
    """Dotted module name for a path under the package root."""

    rel = path.relative_to(package_root).with_suffix("")
    return package_root.name + "." + ".".join(rel.parts)


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _scoped_imports(tree: ast.AST, scope: str = MODULE_SCOPE) -> Iterator[ImportSite]:
    """Yield an ``ImportSite`` for every import statement in the tree.

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
                yield ImportSite(scope, node.lineno, alias.name, (WHOLE_MODULE,))
            continue
        if isinstance(node, ast.ImportFrom):
            # ``module`` may be None for ``from . import x``; ``targets()``
            # folds the imported names in so ``from ..clients import tmux`` is
            # still caught.
            base = node.module or ""
            yield ImportSite(scope, node.lineno, base, tuple(alias.name for alias in node.names))
            continue
        yield from _scoped_imports(node, scope)


def _imports_state_client_directly(target: str) -> bool:
    """True if an import target names clients.tmux/clients.database itself.

    Used only to derive ``FORBIDDEN_STATE_MODULES`` below -- deliberately
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


def _derive_forbidden_state_modules(
    module_paths: Optional[Iterable[Path]] = None, package_root: Path = _PACKAGE_ROOT
) -> Dict[str, str]:
    """AST-scan the package for every module that opens a state client itself.

    An mcp_server module importing one of these has the same non-HTTP path to
    the database as importing the client directly (the gap #744 found in
    ``store_lesson``). Scanning finds every such module, not just the ones
    mcp_server/ happens to import today, so the next tool that reaches for
    a module nothing in mcp_server/ imports yet is caught without editing
    this file. ``module_paths``/``package_root`` exist so the derivation can
    be proven on a synthetic package (``test_derivation_finds_exactly_the_
    modules_that_name_a_client``); the real set is derived with the defaults.
    """

    derived: Dict[str, str] = {}
    if module_paths is None:
        module_paths = _state_candidate_modules(package_root)
    for module_path in module_paths:
        name = _module_name(module_path, package_root)
        tree = _parse(module_path)
        if any(
            _imports_state_client_directly(target)
            for site in _scoped_imports(tree)
            for target in site.targets()
        ):
            derived[name] = _KNOWN_STATE_MODULE_REASONS.get(
                name, "opens clients.tmux/clients.database directly"
            )
    return derived


FORBIDDEN_STATE_MODULES: Dict[str, str] = _derive_forbidden_state_modules()


def _all_forbidden() -> Tuple[str, ...]:
    return tuple(FORBIDDEN_MODULES) + tuple(FORBIDDEN_STATE_MODULES)


def _forbidden_root(target: str) -> Optional[str]:
    """Return the forbidden module an import target names, else None."""

    for module in _all_forbidden():
        if target == module or target.startswith(f"{module}."):
            return module
    for suffix in FORBIDDEN_SUFFIXES:
        if target == suffix or target.endswith(f".{suffix}"):
            return suffix
    return None


def _forbidden_hits(site: ImportSite) -> Iterator[Tuple[str, Tuple[str, ...]]]:
    """Yield ``(forbidden root, names bound from it)`` for one import site.

    ``from pkg.memory_service import A, B`` binds ``A`` and ``B`` from the
    forbidden module. ``from pkg.services import memory_service`` and
    ``import pkg.services.memory_service`` bind the module itself, which is
    recorded as ``WHOLE_MODULE`` because every name is reachable through it.
    """

    root = _forbidden_root(site.module)
    if root is not None:
        yield root, site.names
        return
    for name in site.names:
        if name == WHOLE_MODULE:
            continue
        target = f"{site.module}.{name}" if site.module else name
        root = _forbidden_root(target)
        if root is not None:
            yield root, (WHOLE_MODULE,)


def _findings_in(modules: Iterable[Tuple[str, ast.AST]]) -> Dict[PinKey, Finding]:
    """Every forbidden import in the given ``(relative path, tree)`` pairs.

    Keyed by (module, scope, forbidden root). The value carries the first line
    the import was seen on, for the failure message, and the union of names
    bound from that root in that scope -- so two ``from X import ...`` blocks
    in one function are judged together against one pin.
    """

    found: Dict[PinKey, Finding] = {}
    for rel, tree in modules:
        for site in _scoped_imports(tree):
            for root, names in _forbidden_hits(site):
                key = (rel, site.scope, root)
                prior = found.get(key)
                if prior is None:
                    found[key] = Finding(site.lineno, frozenset(names))
                else:
                    found[key] = Finding(prior.lineno, prior.names | frozenset(names))
    return found


def _findings() -> Dict[PinKey, Finding]:
    """Every forbidden import under the real mcp_server/ package."""

    return _findings_in(
        (path.relative_to(_PACKAGE_ROOT).as_posix(), _parse(path)) for path in _mcp_server_modules()
    )


def _state_service_violations(
    findings: Mapping[PinKey, Finding],
    allowlist: Mapping[PinKey, Pin],
    forbidden_modules: Mapping[str, str],
) -> List[str]:
    """Findings that name a state module and are not exactly covered by a pin."""

    violations: List[str] = []
    for (rel, scope, root), finding in sorted(findings.items()):
        if root not in forbidden_modules:
            continue
        pin = allowlist.get((rel, scope, root))
        if pin is None:
            violations.append(
                f"{rel}:{finding.lineno} ({scope}) imports state module '{root}' "
                f"-- {forbidden_modules[root]}. "
                "Route it through the FastAPI surface, or pin it in "
                "DUAL_MODE_ALLOWLIST / KNOWN_GAP_ALLOWLIST with a reason and the exact names."
            )
            continue
        unpinned = sorted(finding.names - set(pin.names))
        if unpinned:
            what = "binds the whole module" if WHOLE_MODULE in unpinned else f"imports {unpinned}"
            violations.append(
                f"{rel}:{finding.lineno} ({scope}) {what} from state module '{root}' "
                f"but the pin allows only {sorted(pin.names)}. "
                "Route the new name through the FastAPI surface, or widen the pin on "
                "purpose with a reason."
            )
    return violations


def _stale_pins(findings: Mapping[PinKey, Finding], allowlist: Mapping[PinKey, Pin]) -> List[str]:
    """Pins that no longer describe a real import, in either direction.

    A pin whose key matches no import re-permits the next regression in that
    function; a pin that allows names the import no longer binds does the same
    for those names. Both are stale.
    """

    stale: List[str] = []
    for key, pin in sorted(allowlist.items()):
        finding = findings.get(key)
        if finding is None:
            stale.append(f"  {key}: no import matches this pin")
            continue
        unused = sorted(set(pin.names) - finding.names)
        if unused:
            stale.append(f"  {key}: pin allows {unused} but the import does not bind them")
    return stale


def test_mcp_server_package_exists() -> None:
    """Sanity: the scan target directory is present and non-empty."""

    assert _MCP_SERVER_DIR.is_dir()
    assert _mcp_server_modules(), "no mcp_server modules found to scan"


def test_mcp_server_modules_do_not_import_state_clients() -> None:
    """No module under mcp_server/ may import clients.tmux or clients.database."""

    violations: List[str] = []
    for (rel, scope, root), finding in sorted(_findings().items()):
        if root not in FORBIDDEN_MODULES and root not in FORBIDDEN_SUFFIXES:
            continue
        violations.append(f"{rel}:{finding.lineno} ({scope}) imports forbidden module '{root}'")

    assert not violations, "HTTP-only boundary violated:\n" + "\n".join(violations)


def test_mcp_server_modules_do_not_open_state_services() -> None:
    """No module under mcp_server/ may import a state module unless pinned.

    A state module opens ``clients.database`` on the caller's behalf, so
    importing one is the same boundary break as importing the client -- the
    gap #744 found in ``store_lesson``. Pinned exceptions live in
    ``DUAL_MODE_ALLOWLIST`` (HTTP-first, local fallback) and
    ``KNOWN_GAP_ALLOWLIST`` (tracked, not yet fixed), and each pin covers
    exactly the names it lists.
    """

    violations = _state_service_violations(_findings(), ALLOWLIST, FORBIDDEN_STATE_MODULES)
    assert not violations, "HTTP-only boundary violated (state module):\n" + "\n".join(violations)


def test_pinned_allowlists_are_exact() -> None:
    """Every pinned exception must still match a real import, name for name.

    Without this, a fixed tool leaves a stale pin behind that silently
    re-permits the next regression in the same function.
    """

    stale = _stale_pins(_findings(), ALLOWLIST)
    assert not stale, (
        "stale allowlist entries in test_http_only_boundary.py -- these pins no "
        "longer describe a real import, delete or trim them:\n" + "\n".join(stale)
    )


def test_no_pin_allows_the_whole_module() -> None:
    """A pin that allows every name is not a pin.

    ``WHOLE_MODULE`` is what the scan records for ``import pkg.mod`` and
    ``from pkg import mod``; listing it in a pin would re-create the hole P2-2
    closed, so the allowlists may not contain it and may not be empty of names.
    """

    bad = sorted(
        key for key, pin in ALLOWLIST.items() if WHOLE_MODULE in pin.names or not pin.names
    )
    assert not bad, f"pins must list the exact names they allow: {bad}"


def test_walker_sees_function_scoped_imports() -> None:
    """The walker must reach imports deferred inside a function body.

    This is the shape #744 hid behind: the state module is imported at the top
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
    seen = {(site.scope, site.module, site.names) for site in _scoped_imports(tree)}

    assert (MODULE_SCOPE, "os", (WHOLE_MODULE,)) in seen
    assert (
        "store_lesson",
        "cli_agent_orchestrator.services.memory_service",
        ("MemoryService",),
    ) in seen
    # Nested inside an `if` inside the function: still attributed to the tool.
    assert ("store_lesson", "cli_agent_orchestrator.clients", ("database",)) in seen
    assert _forbidden_root("cli_agent_orchestrator.services.memory_service.MemoryService") == (
        "cli_agent_orchestrator.services.memory_service"
    )


# --------------------------------------------------------------------------
# Mutation tests: the shapes the guard exists to catch, run against the REAL
# allowlist on a synthetic server.py so the pins themselves are under test.
# --------------------------------------------------------------------------

# The module-scope block server.py has today, which the first two pins cover.
_PINNED_MODULE_BLOCK = (
    "from cli_agent_orchestrator.services.memory_service import (\n"
    "    MEMORY_DISABLED_MESSAGE,\n"
    "    MemoryDisabledError,\n"
    "    MemoryPartialWriteError,\n"
    ")\n"
    "from cli_agent_orchestrator.services.outcome_service import LEARNING_DISABLED_CODE\n"
)

_SERVER_REL = "mcp_server/server.py"


def _server_findings(source: str) -> Dict[PinKey, Finding]:
    return _findings_in([(_SERVER_REL, ast.parse(source))])


def test_pinned_module_block_alone_is_clean() -> None:
    """Control: the real module-scope block passes, so the pins do the work below."""

    violations = _state_service_violations(
        _server_findings(_PINNED_MODULE_BLOCK), ALLOWLIST, FORBIDDEN_STATE_MODULES
    )
    assert violations == []


# (label, mutated server.py source, substring the violation message must carry)
_SERVER_MUTATIONS: List[Tuple[str, str, str]] = [
    (
        "P2-2: MemoryService added to the pinned module-scope block",
        _PINNED_MODULE_BLOCK.replace(
            "    MEMORY_DISABLED_MESSAGE,\n",
            "    MEMORY_DISABLED_MESSAGE,\n    MemoryService,\n",
        )
        + "\nasync def zz_probe(content):\n    return MemoryService().store(content)\n",
        "imports ['MemoryService'] from state module "
        "'cli_agent_orchestrator.services.memory_service' but the pin allows only",
    ),
    (
        "P2-2: a second module-scope from-import of the same module",
        _PINNED_MODULE_BLOCK
        + "from cli_agent_orchestrator.services.memory_service import MemoryService\n",
        "imports ['MemoryService']",
    ),
    (
        "P2-2: plain import binds the whole module at module scope",
        _PINNED_MODULE_BLOCK + "import cli_agent_orchestrator.services.memory_service as ms\n",
        "binds the whole module from state module",
    ),
    (
        "P2-2: from-package import binds the whole module at module scope",
        _PINNED_MODULE_BLOCK + "from cli_agent_orchestrator.services import memory_service\n",
        "binds the whole module from state module",
    ),
    (
        "P2-2: a new name on the single-constant outcome_service pin",
        _PINNED_MODULE_BLOCK.replace(
            "import LEARNING_DISABLED_CODE\n",
            "import LEARNING_DISABLED_CODE, record_outcome\n",
        ),
        "imports ['record_outcome'] from state module "
        "'cli_agent_orchestrator.services.outcome_service'",
    ),
    (
        "#744 shape: MemoryService imported inside an unpinned tool",
        _PINNED_MODULE_BLOCK + "\nasync def store_lesson(content):\n"
        "    from cli_agent_orchestrator.services.memory_service import MemoryService\n"
        "    return MemoryService().store(content)\n",
        "(store_lesson) imports state module 'cli_agent_orchestrator.services.memory_service'",
    ),
    (
        "P3-2: state module outside services/ imported inside a tool",
        _PINNED_MODULE_BLOCK + "\nasync def zz_probe():\n"
        "    from cli_agent_orchestrator.decisions.store import DecisionStore\n"
        "    return DecisionStore\n",
        "(zz_probe) imports state module 'cli_agent_orchestrator.decisions.store'",
    ),
    (
        "P3-2: provider manager imported at module scope",
        _PINNED_MODULE_BLOCK
        + "from cli_agent_orchestrator.providers.manager import provider_manager\n",
        "(<module>) imports state module 'cli_agent_orchestrator.providers.manager'",
    ),
    (
        "pinned tool reaching for a name its pin does not allow",
        _PINNED_MODULE_BLOCK + "\nasync def memory_store(content):\n"
        "    from cli_agent_orchestrator.services.memory_service import (\n"
        "        MemoryService,\n"
        "        record_project_alias,\n"
        "    )\n"
        "    return MemoryService(), record_project_alias\n",
        "(memory_store) imports ['record_project_alias'] from state module",
    ),
]


@pytest.mark.parametrize(
    ("label", "mutation", "expect"),
    _SERVER_MUTATIONS,
    ids=[label for label, _, _ in _SERVER_MUTATIONS],
)
def test_mutated_server_fails_the_state_service_gate(
    label: str, mutation: str, expect: str
) -> None:
    """Each mutation must produce exactly the violation the message promises."""

    violations = _state_service_violations(
        _server_findings(mutation), ALLOWLIST, FORBIDDEN_STATE_MODULES
    )
    assert violations, f"{label}: guard passed a mutated server.py"
    assert any(expect in v for v in violations), f"{label}: unexpected message:\n" + "\n".join(
        violations
    )


def test_over_broad_pin_is_reported_stale() -> None:
    """A pin that allows a name the import does not bind is stale, not slack.

    This is the reviewer's head-tree scenario inverted: if the function import
    of ``MemoryService`` is deleted but its pin is widened onto the module
    block, the exactness test must still fail.
    """

    over_broad: Dict[PinKey, Pin] = {
        (_SERVER_REL, MODULE_SCOPE, "cli_agent_orchestrator.services.memory_service"): Pin(
            (
                "MEMORY_DISABLED_MESSAGE",
                "MemoryDisabledError",
                "MemoryPartialWriteError",
                "MemoryService",
            ),
            "too wide",
        ),
        (_SERVER_REL, "store_lesson", "cli_agent_orchestrator.services.memory_service"): Pin(
            ("MemoryService",), "gone"
        ),
    }
    stale = _stale_pins(_server_findings(_PINNED_MODULE_BLOCK), over_broad)
    assert len(stale) == 2
    assert any("pin allows ['MemoryService'] but the import does not bind them" in s for s in stale)
    assert any("'store_lesson'" in s and "no import matches" in s for s in stale)


def test_known_reasons_name_derived_modules() -> None:
    """Every hand-written reason must describe a module the scan still derives.

    The derivation fills in a generic reason for any module without an entry,
    so "every entry has a reason" can never fail and is not asserted. What
    can rot is the other direction: a module stops importing a client (as
    services/vault/config.py did in #1031) and its prose reason lingers,
    describing an import that no longer exists. That is the stale-pin failure
    mode again, so it is held to the same standard.
    """

    stale = sorted(set(_KNOWN_STATE_MODULE_REASONS) - set(FORBIDDEN_STATE_MODULES))
    assert not stale, (
        "_KNOWN_STATE_MODULE_REASONS names modules the scan no longer derives; "
        f"delete these entries: {stale}"
    )


def test_forbidden_state_modules_cover_modules_not_imported_by_mcp_server() -> None:
    """The forbidden set must be derived, not limited to today's four imports.

    A hand-picked list scoped to what mcp_server/ already imports cannot catch
    the *next* tool reaching for an unrelated state module -- e.g.
    ``session_service``, which opens ``clients.database`` at module scope but
    nothing under mcp_server/ imports it today, or ``decisions.store``, which
    is not under services/ at all. This fails against a hardcoded four-entry
    map, and against a services/-only scan, and passes only because the set
    is derived by scanning the whole package.
    """

    bypass_candidates = (
        "cli_agent_orchestrator.services.session_service",
        "cli_agent_orchestrator.services.terminal_service",
        "cli_agent_orchestrator.services.inbox_service",
        "cli_agent_orchestrator.services.approval_store",
        # Outside services/: state modules a services/-only scan missed (P3-2).
        "cli_agent_orchestrator.decisions.store",
        "cli_agent_orchestrator.providers.manager",
        "cli_agent_orchestrator.backends.factory",
    )
    missing = [name for name in bypass_candidates if name not in FORBIDDEN_STATE_MODULES]
    assert not missing, f"derived FORBIDDEN_STATE_MODULES is missing: {missing}"
    # The clients themselves and the guarded side are never "state modules".
    assert not [name for name in FORBIDDEN_STATE_MODULES if ".clients." in name]
    assert not [name for name in FORBIDDEN_STATE_MODULES if ".mcp_server." in name]
    # No size assertion on purpose: the set tracks the package as it changes
    # (#1031 shrank it by making vault/config.py client-free; a new
    # database-backed module grows it) and neither is a boundary regression.
    # The derivation itself is proven on a synthetic package below.


def test_derivation_finds_exactly_the_modules_that_name_a_client(tmp_path: Path) -> None:
    """The derived set is every module that names a client itself, one hop deep.

    Run on a synthetic package so the assertion is about the derivation, not a
    snapshot of today's package: a function-scope import, a module-scope
    import and a relative import are all found, under services/ and under any
    other package directory; a client-free module is not; a module that
    imports a forbidden *module* is not (one hop); the clients themselves and
    the guarded mcp_server/ side are never candidates.
    """

    pkg = tmp_path / "fakepkg"
    services = pkg / "services"
    services.mkdir(parents=True)
    (services / "opens_db_in_function.py").write_text(
        "def store(x):\n"
        "    from cli_agent_orchestrator.clients.database import SessionLocal\n"
        "    return SessionLocal\n"
    )
    (services / "opens_tmux_at_module.py").write_text(
        "from cli_agent_orchestrator.clients import tmux\n"
    )
    (services / "opens_db_relative.py").write_text("from ..clients.database import SessionLocal\n")
    (services / "pure.py").write_text("import os\nfrom fakepkg.utils import helper\n")
    (services / "one_hop_away.py").write_text(
        "from fakepkg.services.opens_db_in_function import store\n"
    )
    decisions = pkg / "decisions"
    decisions.mkdir()
    (decisions / "store.py").write_text("from cli_agent_orchestrator.clients.database import x\n")
    for excluded in _UNSCANNED_TOP_LEVEL:
        (pkg / excluded).mkdir()
        (pkg / excluded / "inner.py").write_text(
            "from cli_agent_orchestrator.clients.database import SessionLocal\n"
        )

    derived = _derive_forbidden_state_modules(package_root=pkg)

    assert sorted(derived) == [
        "fakepkg.decisions.store",
        "fakepkg.services.opens_db_in_function",
        "fakepkg.services.opens_db_relative",
        "fakepkg.services.opens_tmux_at_module",
    ]
    assert all(reason for reason in derived.values())
