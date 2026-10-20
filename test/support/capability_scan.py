"""Capability-reachability scanning over the source tree (issue #1005).

The defect class, from #814 and #752: a capability flag or detection signal is
DECLARED on a base class and never CONSULTED on the decision path. Nothing
errors -- behaviour just falls back to a weaker signal, silently, and the
declaration reads like a working feature to the next person.

#814's guard caught one half of it for one surface: a ``BaseProvider`` boolean
no provider sets has an unreachable True branch. The other half is a capability
that providers DO set and no decision path reads; and the same shape exists on
the backend contract, on the plugin event vocabulary, and on the provider
opt-in hooks. This module is the generalised scan: enumerate what a base class
declares, then find who overrides it and who actually reads it.

Two helpers, both pure AST (no imports of product code, so a scan never runs
module-level side effects):

- :func:`declared_capabilities` -- what a base class declares.
- :func:`find_references` -- where each name is overridden and consumed.

A reference is classified by its syntactic position, not by which file it is
in:

- ``DECLARATION``  the declaring module's own ``name: bool = False`` / ``def name``
- ``OVERRIDE``     an assignment or ``def`` of the same name in an implementation
- ``CONSUMER``     any other read: ``self.name``, ``backend.name()``, or a string
  literal in a call/decorator argument, dict-key, or comparison position --
  e.g. ``getattr(x, "name", default)``, ``@hook("post_x")``, ``{"post_x": ...}``.
  A bare string assignment like ``LABEL = "name"`` does NOT count: it shares
  the name without reading it.

Only a CONSUMER proves the capability reaches a decision. An OVERRIDE with no
CONSUMER anywhere is precisely the #752/#814 shape.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "cli_agent_orchestrator"


class RefKind(str, Enum):
    """How a name occurrence relates to the capability."""

    DECLARATION = "declaration"
    OVERRIDE = "override"
    CONSUMER = "consumer"


@dataclass(frozen=True)
class Capability:
    """One declared capability on a base class."""

    name: str
    kind: str
    module: Path
    lineno: int

    @property
    def where(self) -> str:
        return f"{self.module.relative_to(REPO_ROOT)}:{self.lineno}"


@dataclass(frozen=True)
class Reference:
    """One occurrence of a capability name in the source tree."""

    kind: RefKind
    module: Path
    lineno: int

    @property
    def where(self) -> str:
        return f"{self.module.relative_to(REPO_ROOT)}:{self.lineno}"


@dataclass
class ScanResult:
    """Per-capability scan outcome."""

    capability: Capability
    overrides: list[Reference] = field(default_factory=list)
    consumers: list[Reference] = field(default_factory=list)

    @property
    def reachable(self) -> bool:
        """True when some decision path outside the declaring module reads it."""
        return bool(self.consumers)


# --------------------------------------------------------------------------
# Helper 1: what does the base class declare?
# --------------------------------------------------------------------------


def _is_bool_annotation(node: ast.AnnAssign) -> bool:
    return ast.unparse(node.annotation) in ("bool", "Optional[bool]", "bool | None")


def _returns_only_bool_constants(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value is not None]
    return bool(returns) and all(
        isinstance(r.value, ast.Constant) and isinstance(r.value.value, bool) for r in returns
    )


def _is_property(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return "property" in [ast.unparse(d) for d in fn.decorator_list]


def _body_is_inert(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the default body does nothing but return a constant / pass.

    That is the signature of an OPT-IN HOOK: the base declares the extension
    point and declines to implement it, so the behaviour only exists if some
    caller invokes it.
    """
    body = [st for st in fn.body if not (isinstance(st, ast.Expr) and _is_docstring(st))]
    if not body:
        return True
    return all(
        isinstance(st, ast.Pass)
        or (isinstance(st, ast.Return) and (st.value is None or isinstance(st.value, ast.Constant)))
        for st in body
    )


def _is_docstring(st: ast.Expr) -> bool:
    return isinstance(st.value, ast.Constant) and isinstance(st.value.value, str)


def _class_def(tree: ast.AST, class_name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"class {class_name} not found -- the scan has rotted, not the code")


def declared_capabilities(
    module: Path,
    class_name: str,
    *,
    kinds: Sequence[str],
    name_filter: Callable[[str], bool] | None = None,
) -> list[Capability]:
    """Enumerate the capability attributes ``class_name`` declares in ``module``.

    Args:
        module: file containing the base class.
        class_name: the base class whose declarations are the capability surface.
        kinds: which declaration shapes to collect --
            ``"bool_attr"``   ``flag: bool = False`` class attributes;
            ``"bool_prop"``   ``@property`` returning only bool constants;
            ``"bool_method"`` plain method annotated ``-> bool``;
            ``"opt_in_hook"`` method whose default body is inert (pass/constant);
            ``"event_type"``  ``event_type: str = "..."`` on a dataclass subclass.
        name_filter: optional predicate to narrow by name (e.g. ``supports_*``).

    Returns:
        Capabilities in declaration order. Private names (leading ``_``) and
        dunders are never capabilities -- they are internal state.
    """
    tree = ast.parse(module.read_text())
    cls = _class_def(tree, class_name)
    found: dict[str, Capability] = {}

    def add(name: str, kind: str, lineno: int) -> None:
        if name.startswith("_") or name in found:
            return
        if name_filter is not None and not name_filter(name):
            return
        found[name] = Capability(name=name, kind=kind, module=module, lineno=lineno)

    for st in cls.body:
        if isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            if "bool_attr" in kinds and _is_bool_annotation(st):
                add(st.target.id, "bool_attr", st.lineno)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns_bool = st.returns is not None and ast.unparse(st.returns) == "bool"
            if "bool_prop" in kinds and _is_property(st) and _returns_only_bool_constants(st):
                add(st.name, "bool_prop", st.lineno)
            elif "bool_method" in kinds and returns_bool and not _is_property(st):
                add(st.name, "bool_method", st.lineno)
            elif "opt_in_hook" in kinds and not _is_property(st) and _body_is_inert(st):
                add(st.name, "opt_in_hook", st.lineno)

    if "event_type" in kinds:
        found.update(_declared_event_types(tree, class_name, module, name_filter))

    return sorted(found.values(), key=lambda c: c.lineno)


def _declared_event_types(
    tree: ast.AST,
    base_name: str,
    module: Path,
    name_filter: Callable[[str], bool] | None,
) -> dict[str, Capability]:
    """Collect the ``event_type`` string literal each subclass of ``base_name`` declares.

    The capability here is the event NAME, because that string is the whole
    contract between an emitter and a ``@hook("...")`` subscriber: a vocabulary
    entry nobody emits is a hook that can never fire.
    """
    out: dict[str, Capability] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if base_name not in [ast.unparse(b) for b in node.bases]:
            continue
        for st in node.body:
            if (
                isinstance(st, ast.AnnAssign)
                and isinstance(st.target, ast.Name)
                and st.target.id == "event_type"
                and isinstance(st.value, ast.Constant)
                and isinstance(st.value.value, str)
                and st.value.value
            ):
                name = st.value.value
                if name_filter is None or name_filter(name):
                    out[name] = Capability(
                        name=name, kind="event_type", module=module, lineno=st.lineno
                    )
    return out


# --------------------------------------------------------------------------
# Helper 2: who overrides it, and who reads it?
# --------------------------------------------------------------------------


def python_files(root: Path, *, exclude: Iterable[Path] = ()) -> list[Path]:
    """Every ``*.py`` under ``root``, excluding the given paths and dirs."""
    excluded = [p.resolve() for p in exclude]

    def skipped(path: Path) -> bool:
        resolved = path.resolve()
        return any(resolved == e or e in resolved.parents for e in excluded)

    return sorted(p for p in root.rglob("*.py") if not skipped(p))


def _declared_names(tree: ast.AST) -> Iterator[tuple[str, int]]:
    """Names this module DEFINES: assignment targets and def/class names."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield node.name, node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id, node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    yield target.id, target.lineno
                elif isinstance(target, ast.Attribute):
                    yield target.attr, target.lineno


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids of the Constant nodes that are docstrings, which never consume anything.

    String literals must count as reads -- the real consumers of a provider
    flag are ``getattr(provider, "supports_screen_detection", False)`` -- but a
    docstring that merely NAMES a capability is documentation, not a decision.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and _is_docstring(body[0]):
                out.add(id(body[0].value))
    return out


def _parent_map(tree: ast.AST) -> dict[int, ast.AST]:
    """Map each node's ``id`` to its parent, for position-sensitive checks."""
    parents: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _is_proving_literal_position(node: ast.Constant, parent: ast.AST | None) -> bool:
    """True when a string literal sits where a decision path would read it.

    Call/decorator argument -- ``getattr(provider, "name", default)``,
    ``@hook("post_create_terminal")``; dict key -- an event-type lookup table
    like ``{"post_create_terminal": "launch"}``; comparison -- ``event_type ==
    "post_create_session"``. Anything else -- most commonly a bare assignment
    RHS like ``LABEL = "name"`` -- is a label that merely shares the
    capability's name, not a read of it, and must not count as a consumer.
    """
    if isinstance(parent, ast.Call):
        return node in parent.args or any(node is kw.value for kw in parent.keywords)
    if isinstance(parent, ast.Dict):
        return node in parent.keys
    if isinstance(parent, ast.Compare):
        return node is parent.left or node in parent.comparators
    return False


def _used_names(tree: ast.AST, *, include_strings: bool) -> Iterator[tuple[str, int]]:
    """Names this module READS: attribute access, bare identifiers, proving literals."""
    declared = {(name, line) for name, line in _declared_names(tree)}
    docstrings = _docstring_nodes(tree) if include_strings else set()
    parents = _parent_map(tree) if include_strings else {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if (node.attr, node.lineno) not in declared:
                yield node.attr, node.lineno
        elif isinstance(node, ast.Name):
            if (node.id, node.lineno) not in declared:
                yield node.id, node.lineno
        elif include_strings and isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings and _is_proving_literal_position(
                node, parents.get(id(node))
            ):
                yield node.value, node.lineno


def find_references(
    names: Iterable[str],
    *,
    declaring_module: Path,
    implementation_files: Sequence[Path],
    search_files: Sequence[Path],
    include_strings: bool = False,
) -> dict[str, ScanResult]:
    """Locate every override and consumer of ``names`` across ``search_files``.

    ``implementation_files`` only changes how a *definition* is labelled: an
    assignment or ``def`` there is an OVERRIDE, while a read there is a
    CONSUMER like any other. The declaring module contributes neither -- its
    own declaration and its own internal reads cannot vouch for a capability
    reaching a decision path elsewhere.
    """
    wanted = set(names)
    results = {n: ScanResult(capability=Capability(n, "", declaring_module, 0)) for n in wanted}
    impl = {p.resolve() for p in implementation_files}
    declaring = declaring_module.resolve()

    for path in search_files:
        if path.resolve() == declaring:
            continue
        tree = ast.parse(path.read_text())
        is_impl = path.resolve() in impl
        if is_impl:
            for name, lineno in _declared_names(tree):
                if name in wanted:
                    results[name].overrides.append(Reference(RefKind.OVERRIDE, path, lineno))
        for name, lineno in _used_names(tree, include_strings=include_strings):
            if name in wanted:
                results[name].consumers.append(Reference(RefKind.CONSUMER, path, lineno))
    return results


def scan_surface(
    *,
    declaring_module: Path,
    class_name: str,
    kinds: Sequence[str],
    implementation_glob: str,
    name_filter: Callable[[str], bool] | None = None,
    include_strings: bool = False,
    search_root: Path = SRC_ROOT,
) -> list[ScanResult]:
    """Run both helpers over one (base class, implementation glob) pair."""
    caps = declared_capabilities(declaring_module, class_name, kinds=kinds, name_filter=name_filter)
    implementations = [
        p for p in sorted(search_root.glob(implementation_glob)) if p != declaring_module
    ]
    refs = find_references(
        [c.name for c in caps],
        declaring_module=declaring_module,
        implementation_files=implementations,
        search_files=python_files(search_root),
        include_strings=include_strings,
    )
    out = []
    for cap in caps:
        result = refs[cap.name]
        result.capability = cap
        out.append(result)
    return out
