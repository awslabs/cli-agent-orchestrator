"""Guard test: the ``cao tui`` package must stay a thin shell — ALLOW-LIST enforced.

Every module under ``src/cli_agent_orchestrator/tui/`` composes the existing
``cao`` CLI surface and reads live state exclusively over HTTP. It may import
**only** the standard-library modules enumerated in :data:`ALLOWED_STDLIB`,
``prompt_toolkit``, ``requests``, ``cli_agent_orchestrator.constants``,
``cli_agent_orchestrator.utils.path_validation``, and the ``tui`` package's own
modules. It must NEVER import the heavy in-process layers (``services``,
``clients``, ``backends``, ``providers``, ``models``) or the ``cli`` command
modules / ``cli`` object.

Why this matters (the chain the guard defends against): importing
``cli_agent_orchestrator.cli.main`` pulls in ``cli.commands.launch``, which at
module top imports ``backends.registry`` (``get_backend``) and
``services.settings_service`` (``get_server_settings``) — the exact heavy
dependency graph a thin shell must not drag in (ADR-007).

**This guard is an allow-list, deliberately (FR-8.1).** It was previously a
*deny-list* of enumerated forbidden modules and suffixes, which does not hold the
claim the module docstrings and the PR body make for it: under a deny-list
anything *not enumerated* passes **by omission** — including
``cli_agent_orchestrator.api``, ``cli_agent_orchestrator.utils.terminal``, and
every future package nobody thought to add. The promise being enforced ("a future
change that pulls service logic into the TUI fails CI") is an allow-list promise,
so the mechanism now matches it: an import that is not explicitly admitted below
fails, and admitting one is a deliberate, reviewable edit to this file.

**Known limitation — the scan is DIRECT-IMPORT ONLY.** :func:`_tui_modules`
globs ``tui/*.py`` and nothing else, so the allow-list constrains what ``tui``
modules import *themselves*, not what those imports in turn pull in. A concrete
live example: ``tui/*`` legitimately imports ``cli_agent_orchestrator.constants``,
and ``constants.py`` itself does ``from cli_agent_orchestrator.models.provider
import ProviderType`` — so a "forbidden" layer (``models``) is loaded
*indirectly* today and this guard passes. Closing that gap means either breaking
the ``constants`` → ``models`` edge or scanning the transitive closure, both of
which are out of scope for this change; it is recorded here and in ``docs/tui.md``
so no reader mistakes this guard for transitive enforcement. (No issue number is
cited: filing one is the human's, not this workflow's.)

Modeled on ``test/test_http_only_boundary.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Tuple

import pytest

import cli_agent_orchestrator

# --------------------------------------------------------------------------- #
# THE ALLOW-LIST. An import target is admitted iff it equals an entry below or   #
# is a dotted descendant of one (``prompt_toolkit.layout.menus`` is admitted by   #
# ``prompt_toolkit``; ``typing.Optional`` by ``typing``). Everything else fails.   #
#                                                                                #
# Re-derived by AST-scanning the CURRENT tree, so it admits exactly the imports    #
# in force and no more (FR-8.2). Adding an entry is a deliberate edit that shows   #
# up in review — which is the whole point of inverting the polarity.               #
# --------------------------------------------------------------------------- #

# Standard library — enumerated individually rather than admitted wholesale, so a
# new stdlib dependency (e.g. ``socket``, ``asyncio``, ``threading``) is a visible
# decision rather than a silent one.
ALLOWED_STDLIB: Tuple[str, ...] = (
    "__future__",
    "contextlib",
    "dataclasses",
    "logging",
    "re",
    "shlex",
    "subprocess",
    "sys",
    "time",
    "typing",
    "urllib.parse",
)

# Third-party — the two the thin shell is allowed to know about. ``prompt_toolkit``
# is the rendering toolkit; ``requests`` is the ONLY way live state is read (BR-1:
# reads are HTTP-only). Note ``pyperclip`` is deliberately ABSENT: the clipboard is
# reached through ``prompt_toolkit.clipboard.pyperclip.PyperclipClipboard``, and no
# module under ``tui/`` imports ``pyperclip`` directly (FR-5.1 / C-3).
ALLOWED_THIRD_PARTY: Tuple[str, ...] = (
    "prompt_toolkit",
    "requests",
)

# First-party — the tui package itself, plus the two narrow modules it is allowed
# to reach out to. Both are leaf-ish helpers, NOT service layers:
#   * ``constants``            — the base-URL / port constants (see the limitation
#                                note in the module docstring: this edge is the
#                                transitive gap).
#   * ``utils.path_validation`` — the SHARED path validator every path-bearing
#                                argument must route through (NFR-9). Reimplementing
#                                it inside ``tui`` would be the real violation.
ALLOWED_FIRST_PARTY: Tuple[str, ...] = (
    "cli_agent_orchestrator.tui",
    "cli_agent_orchestrator.constants",
    "cli_agent_orchestrator.utils.path_validation",
)

ALLOWED: Tuple[str, ...] = ALLOWED_STDLIB + ALLOWED_THIRD_PARTY + ALLOWED_FIRST_PARTY

# Representative heavy layers, used ONLY by the load-bearing proof below. These are
# not the enforcement mechanism (the allow-list is) — they are the synthetic
# violations the proof feeds in to show the allow-list actually rejects something.
PROOF_VIOLATIONS: Tuple[str, ...] = (
    "cli_agent_orchestrator.services.settings_service",
    "cli_agent_orchestrator.clients.tmux",
    "cli_agent_orchestrator.backends.registry",
    "cli_agent_orchestrator.providers.claude_code",
    "cli_agent_orchestrator.models.provider",
    "cli_agent_orchestrator.cli.main",
    "cli_agent_orchestrator.api.main",
    "cli_agent_orchestrator.utils.terminal",
    "cli_agent_orchestrator.utils.agent_profiles",
    # Bare tails, as a relative import (``from .. import cli``) presents them.
    "cli",
    "services",
    "models",
    # A brand-new third-party dependency nobody enumerated.
    "httpx",
    "pyperclip",
)

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_TUI_DIR = _PACKAGE_ROOT / "tui"


def _tui_modules() -> List[Path]:
    """Return every Python module under the tui package.

    Direct-import scan only — see the module docstring's known-limitation note.
    """

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
            # names in so ``from ..services import settings_service`` is caught
            # even though its AST base is only ``services``.
            base = node.module or ""
            targets.append(base)
            for alias in node.names:
                targets.append(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _is_allowed(target: str) -> bool:
    """True iff ``target`` is admitted by the allow-list.

    A target is admitted when it equals an allowed entry, or is a dotted
    descendant of one — so symbols (``typing.Optional``) and submodules
    (``prompt_toolkit.layout.menus``) ride on their parent's admission while a
    sibling package (``cli_agent_orchestrator.utils.terminal`` alongside the
    allowed ``utils.path_validation``) does NOT.

    The empty string is admitted: ``ast.ImportFrom`` yields it as the *base* of a
    purely relative ``from . import x``, whose real target is folded in separately
    as the bare alias name and checked on its own.
    """

    if target == "":
        return True
    return any(target == allowed or target.startswith(f"{allowed}.") for allowed in ALLOWED)


def test_tui_package_exists() -> None:
    """Sanity: the scan target directory is present and non-empty.

    Guards against an empty-glob false pass — if ``tui/`` disappeared or held no
    modules, the boundary test below would vacuously pass.
    """

    assert _TUI_DIR.is_dir()
    assert _tui_modules(), "no tui modules found to scan"


def test_tui_imports_are_thin_shell() -> None:
    """Every import under tui/ must be explicitly ADMITTED by the allow-list.

    The failure message names the offending module, target, and what to do — an
    allow-list failure is often a legitimate new dependency, and the reviewer needs
    to see the decision rather than guess it.
    """

    violations: List[str] = []
    for module_path in _tui_modules():
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(module_path))
        for target in _imported_targets(tree):
            if not _is_allowed(target):
                rel = module_path.relative_to(_PACKAGE_ROOT)
                violations.append(f"{rel} imports non-admitted module '{target}'")

    assert not violations, (
        "thin-shell boundary violated — these imports are not on the allow-list in\n"
        "test/tui/test_thin_shell_boundary.py. If the import is legitimately part of\n"
        "a thin shell, ADD it to ALLOWED_* with a rationale; if it pulls in a heavy\n"
        "in-process layer, it does not belong under tui/ (ADR-007):\n" + "\n".join(violations)
    )


@pytest.mark.parametrize("forbidden", PROOF_VIOLATIONS)
def test_the_allow_list_rejects_a_synthetic_forbidden_import(forbidden: str) -> None:
    """FR-8.3: prove the guard is LOAD-BEARING, not merely green.

    Feeds a synthetic module through the same ``_imported_targets`` →
    ``_is_allowed`` pipeline the real scan uses. Inverting the guard back to a
    deny-list makes the non-enumerated cases here (``api.main``,
    ``utils.terminal``, ``utils.agent_profiles``, ``httpx``, ``pyperclip``) pass by
    omission and this REDs — which is exactly the defect FR-8.1 reports.
    """

    tree = ast.parse(f"import {forbidden}\n" if "." not in forbidden else f"import {forbidden}")
    targets = _imported_targets(tree)

    assert targets, "the synthetic source produced no import target to check"
    assert not any(_is_allowed(target) for target in targets), (
        f"the allow-list admitted the forbidden module '{forbidden}' — the guard is not "
        "load-bearing"
    )


@pytest.mark.parametrize(
    "forbidden_source",
    [
        "from cli_agent_orchestrator.services import settings_service",
        "from cli_agent_orchestrator.utils.terminal import send_keys",
        "from ..services import settings_service",
        "from .. import cli",
        "from cli_agent_orchestrator.api.main import app",
        "import pyperclip",
    ],
    ids=[
        "absolute-services",
        "sibling-utils-module",
        "relative-services",
        "relative-cli",
        "api-main",
        "direct-pyperclip",
    ],
)
def test_the_allow_list_rejects_forbidden_import_STATEMENTS(forbidden_source: str) -> None:
    """FR-8.3, the statement forms — including the RELATIVE ones.

    A relative ``from ..services import settings_service`` presents to the AST as
    base ``"services"``, which no absolute-path check would catch. Both the
    ``ImportFrom`` base and the folded ``base.alias`` are exercised here, and
    ``from .. import cli`` (base ``""``, alias ``cli``) covers the case where the
    base carries no information at all.
    """

    targets = _imported_targets(ast.parse(forbidden_source))
    non_admitted = [target for target in targets if not _is_allowed(target)]

    assert non_admitted, f"nothing in {forbidden_source!r} was rejected: {targets}"


def test_the_allow_list_admits_the_imports_actually_in_use() -> None:
    """The counterpart: the allow-list must not be so tight it rejects real imports.

    Without this, "make the guard pass" could be satisfied by an allow-list that
    admits nothing while the scan silently found no modules. Asserts a
    representative sample of each admitted category resolves.
    """

    for admitted in (
        "typing.Optional",
        "urllib.parse.quote",
        "prompt_toolkit.layout.menus.CompletionsMenu",
        "requests",
        "cli_agent_orchestrator.tui.server_client.ServerAuthRequired",
        "cli_agent_orchestrator.constants.API_BASE_URL",
        "cli_agent_orchestrator.utils.path_validation.resolve_and_validate_path",
    ):
        assert _is_allowed(admitted), f"the allow-list wrongly rejects {admitted!r}"


def test_the_allow_list_has_no_dead_entries() -> None:
    """Every allow-list entry must be justified by an import that actually exists.

    An allow-list only stays meaningful if it is pruned as well as extended — a
    leftover entry is a hole nobody is watching. A failure here is not necessarily
    a bug; it means an import was removed and its permission should be too.
    """

    used: set[str] = set()
    for module_path in _tui_modules():
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        for target in _imported_targets(tree):
            for allowed in ALLOWED:
                if target == allowed or target.startswith(f"{allowed}."):
                    used.add(allowed)

    unused = sorted(set(ALLOWED) - used)
    assert not unused, (
        "these allow-list entries are no longer justified by any import under tui/ "
        f"— prune them: {unused}"
    )
