"""Shared call-site lint helper for structural guard tests (#1005).

Several defect classes in this repo share one shape: a *call site* carries a
hand-written literal that was correct when it was typed and silently stopped
covering its surface later. #896 was a hand-enumerated ``OrchestrationType``
subset; #710 was timeout numbers that did not derive from the bound they had to
respect; #735 was a poll that accepted a frame from before the turn it was
waiting on. Each of those issues hand-rolled its own ``ast`` walk over ``src/``.

This module is the shared half of those walks:

* :func:`modules` / :func:`parse_sources` -- parse every module under ``src/``
  once and hand back the AST *and* the source lines, so a rule can look for an
  inline marker next to the node it flagged.
* :func:`enum_members` -- map every ``Enum`` subclass defined in ``src/`` to its
  member names, so a rule can tell a *subset* literal from a *complete* one.
* :func:`inline_marker` -- read an inline ``# <prefix>: <reason>`` escape hatch
  off the flagged line (or the line that opens the statement).
* :func:`check_against_allowlist` -- the ratchet. Compare the sites a rule found
  against an explicit allowlist and raise ``AssertionError`` with a message that
  names ``file:line`` and says what to do. It fails in *three* directions: a new
  unexplained site is a violation, a site beyond a row's reviewed occurrence
  count is a violation, and an allowlist row that no longer matches anything is
  also a violation -- so a row can cover repeats of one idiom in one module,
  but only as many as were actually reviewed.

Nothing here imports ``cli_agent_orchestrator`` at module scope beyond locating
the package directory, and nothing here executes product code -- these are
stdlib ``ast`` reads of the tree on disk.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

import cli_agent_orchestrator

#: Root of the package under lint. Rules report paths relative to its parent so
#: a failure message reads ``cli_agent_orchestrator/services/foo.py:12``.
PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).resolve().parent

#: Base-class names that make a ``class`` an enumeration for rule purposes.
#: Matched on the bare attribute name so ``enum.StrEnum`` and ``StrEnum`` both
#: count, and so a mixin spelling like ``class X(str, Enum)`` is caught.
ENUM_BASE_NAMES: FrozenSet[str] = frozenset(
    {"Enum", "StrEnum", "IntEnum", "IntFlag", "Flag", "ReprEnum"}
)


# ---------------------------------------------------------------------------
# Source discovery and parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Module:
    """One parsed module under lint.

    Attributes:
        path: Absolute path on disk.
        relpath: Path relative to ``PACKAGE_ROOT.parent``, used in messages and
            as the stable key for allowlist rows.
        tree: The parsed module AST.
        lines: Source lines, 0-indexed, for inline-marker lookups.
    """

    path: Path
    relpath: str
    tree: ast.Module
    lines: Tuple[str, ...]


def modules(root: Optional[Path] = None) -> List[Path]:
    """Return every Python module under ``root`` (default: the package), sorted.

    Sorted so rule failures list sites in a stable order and a reviewer can
    diff two runs.
    """
    base = PACKAGE_ROOT if root is None else root
    return sorted(base.rglob("*.py"))


@lru_cache(maxsize=4)
def parse_sources(root: Optional[Path] = None) -> Tuple[Module, ...]:
    """Parse every module under ``root`` once and cache the result.

    Cached because three rule modules each want the whole tree; parsing ~280
    files costs well under a second but there is no reason to pay it per rule.

    Raises:
        SyntaxError: If a module under lint does not parse. That is a real
            failure -- a guard that silently skipped unparseable files would be
            the exact blind spot these rules exist to remove.
    """
    out: List[Module] = []
    anchor = (PACKAGE_ROOT if root is None else root).parent
    for path in modules(root):
        text = path.read_text(encoding="utf-8")
        out.append(
            Module(
                path=path,
                relpath=path.relative_to(anchor).as_posix(),
                tree=ast.parse(text, filename=str(path)),
                lines=tuple(text.splitlines()),
            )
        )
    return tuple(out)


# ---------------------------------------------------------------------------
# Enum discovery
# ---------------------------------------------------------------------------


def _base_names(node: ast.ClassDef) -> FrozenSet[str]:
    """Bare names of a class's bases (``enum.Enum`` -> ``Enum``)."""
    names = set()
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.add(base.id)
        elif isinstance(base, ast.Attribute):
            names.add(base.attr)
    return frozenset(names)


def enum_members(sources: Optional[Sequence[Module]] = None) -> Dict[str, FrozenSet[str]]:
    """Map each ``Enum`` subclass defined under lint to its member names.

    Members are the public class-level assignment targets. ``_``-prefixed names,
    methods and nested classes are skipped: ``_ignore_``, ``_missing_`` and
    helper methods are not members, and counting them would make a complete
    literal look like a subset.

    A name defined twice in the tree collapses to one entry; the rules here only
    ever ask "is this literal the whole enum", and the two shapes that matter
    (duplicate module-private enums) have identical membership in practice. A
    rule that needs per-module resolution should key off the module itself.
    """
    found: Dict[str, FrozenSet[str]] = {}
    for module in parse_sources() if sources is None else sources:
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if not (_base_names(node) & ENUM_BASE_NAMES):
                continue
            members = set()
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and not target.id.startswith("_"):
                            members.add(target.id)
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if not stmt.target.id.startswith("_"):
                        members.add(stmt.target.id)
            found[node.name] = frozenset(members)
    return found


# ---------------------------------------------------------------------------
# Call-site records and the inline escape hatch
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """One flagged call site.

    Attributes:
        relpath: Module path relative to the package parent.
        line: 1-indexed line of the flagged node.
        key: The allowlist key. Deliberately *line-free* so moving code does not
            invalidate a reviewed allowlist row; see :func:`check_against_allowlist`.
        detail: Short human rendering of what was flagged, e.g.
            ``TerminalStatus in {COMPLETED, IDLE}``.
    """

    relpath: str
    line: int
    key: Tuple[str, ...]
    detail: str

    @property
    def where(self) -> str:
        """``path:line``, the clickable form used in failure messages."""
        return f"{self.relpath}:{self.line}"


def inline_marker(module: Module, lineno: int, prefix: str) -> Optional[str]:
    """Return the reason from an inline ``# <prefix>: <reason>`` marker, if any.

    Looks at the flagged line and the two lines above it. The two-line lookback
    exists because ``black`` wraps long membership tests, which pushes the set
    literal onto its own line while the natural place for the marker is the
    ``if`` that opens the statement.

    Returns:
        The reason text with surrounding whitespace stripped, or ``None`` if no
        marker is present. An empty reason (``# subset-ok:``) returns ``None``
        too -- a marker with no reason is not an explanation.
    """
    token = f"# {prefix}:"
    for candidate in (lineno, lineno - 1, lineno - 2):
        if not 1 <= candidate <= len(module.lines):
            continue
        text = module.lines[candidate - 1]
        if token in text:
            reason = text.split(token, 1)[1].strip()
            if reason:
                return reason
    return None


# ---------------------------------------------------------------------------
# The ratchet
# ---------------------------------------------------------------------------

#: An allowlist row is either a bare reason (occurrence count defaults to 1)
#: or an explicit ``(count, reason)`` pair for a row that a human reviewed as
#: covering more than one call site under the same key.
AllowlistRow = Union[str, Tuple[int, str]]


def row_count_and_reason(row: AllowlistRow) -> Tuple[int, str]:
    """Unpack an :data:`AllowlistRow` into its reviewed count and reason."""
    if isinstance(row, tuple):
        return row
    return 1, row


def check_against_allowlist(
    *,
    rule: str,
    sites: Iterable[Site],
    allowlist: Mapping[Tuple[str, ...], AllowlistRow],
    remedy: str,
    marked: Optional[Mapping[Tuple[str, ...], str]] = None,
) -> None:
    """Fail unless every flagged site is explained and every row still matches.

    Allowlist keys are the line-free :attr:`Site.key`, so one row can cover the
    repetitions of a single idiom inside one module (for example the four
    ``TerminalStatus in {COMPLETED, IDLE}`` tests in ``status_monitor.py``) and
    reformatting never invalidates a reviewed row -- but only up to the
    occurrence count a human actually reviewed. A plain string reason implies a
    reviewed count of 1; a row that covers more than one site under its key
    must spell the count out as ``(count, reason)``, or an (N+1)th call site
    added to a module that already has a one-occurrence row would pass with
    zero signal.

    Args:
        rule: Short rule name, used in the message header.
        sites: Every site the rule flagged.
        allowlist: ``{key: row}`` rows a human reviewed and accepted. See
            :data:`AllowlistRow`.
        remedy: What the author of a *new* violation should do, in one or two
            sentences. This is the whole value of the rule, so it is required.
        marked: ``{key: reason}`` for sites that carried an inline marker.
            Treated exactly like an allowlist row but reported separately so a
            stale inline marker is not blamed on the table.

    Raises:
        AssertionError: On any unexplained site, any site beyond its row's
            reviewed count, or any row that matched nothing.
    """
    sites = list(sites)
    marked = {} if marked is None else marked
    marked_keys = set(marked)
    seen = {site.key for site in sites}

    unexplained: List[Site] = []
    overflow: List[Site] = []
    consumed: Dict[Tuple[str, ...], int] = {}
    for site in sites:
        if site.key in marked_keys:
            continue
        if site.key not in allowlist:
            unexplained.append(site)
            continue
        allowed_count, _ = row_count_and_reason(allowlist[site.key])
        consumed[site.key] = consumed.get(site.key, 0) + 1
        if consumed[site.key] > allowed_count:
            overflow.append(site)

    stale_rows = sorted(key for key in allowlist if key not in seen)
    stale_marks = sorted(key for key in marked_keys if key not in seen)

    if not (unexplained or overflow or stale_rows or stale_marks):
        return

    problems: List[str] = [f"call-site lint [{rule}] failed."]

    if unexplained:
        problems.append("")
        problems.append(f"{len(unexplained)} unexplained call site(s):")
        for site in unexplained:
            problems.append(f"  {site.where}  {site.detail}")
        problems.append("")
        problems.append(f"What to do: {remedy}")

    if overflow:
        problems.append("")
        problems.append(
            f"{len(overflow)} call site(s) exceed their allowlist row's reviewed "
            "occurrence count:"
        )
        for site in overflow:
            allowed_count, reason = row_count_and_reason(allowlist[site.key])
            problems.append(
                f"  {site.where}  {site.detail}  (row allows {allowed_count}, reviewed as: {reason})"
            )
        problems.append("")
        problems.append(f"What to do: {remedy}")

    if stale_rows:
        problems.append("")
        problems.append(
            f"{len(stale_rows)} allowlist row(s) no longer match any call site. "
            "The site was fixed or moved -- delete the row. This list can only "
            "shrink; that is the point."
        )
        for key in stale_rows:
            _, reason = row_count_and_reason(allowlist[key])
            problems.append(f"  {' | '.join(key)}   (reason was: {reason})")

    if stale_marks:
        problems.append("")
        problems.append(
            f"{len(stale_marks)} inline marker(s) no longer sit on a flagged "
            "call site. Remove the stale comment."
        )
        for key in stale_marks:
            problems.append(f"  {' | '.join(key)}")

    raise AssertionError("\n".join(problems))


# ---------------------------------------------------------------------------
# Rule primitives
# ---------------------------------------------------------------------------


def _strip_value(node: ast.expr) -> ast.expr:
    """Unwrap a trailing ``.value`` so ``Status.X.value`` reads as ``Status.X``."""
    if isinstance(node, ast.Attribute) and node.attr == "value":
        return node.value
    return node


def enum_subset_sites(
    sources: Optional[Sequence[Module]] = None,
    members: Optional[Mapping[str, FrozenSet[str]]] = None,
) -> List[Site]:
    """Flag ``x in {A.P, A.Q}`` / ``in (A.P.value, ...)`` literals over one Enum.

    A site is flagged only when *every* element resolves to a member of the
    *same* enum defined under lint, and the member set is a strict subset of
    that enum. A literal naming the whole enum is not flagged: it is already
    exhaustive, and adding a member makes it a subset, which flags it then --
    which is exactly the moment a human should look.

    Set, tuple and list literals all count; so do ``not in`` tests, since the
    coverage hole is identical.
    """
    srcs = parse_sources() if sources is None else sources
    table = enum_members(srcs) if members is None else members
    out: List[Site] = []
    for module in srcs:
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Compare):
                continue
            for op, comparator in zip(node.ops, node.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                if not isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                    continue
                if not comparator.elts:
                    continue
                classes = set()
                names = set()
                resolved = True
                for element in comparator.elts:
                    base = _strip_value(element)
                    if (
                        isinstance(base, ast.Attribute)
                        and isinstance(base.value, ast.Name)
                        and base.value.id in table
                    ):
                        classes.add(base.value.id)
                        names.add(base.attr)
                    else:
                        resolved = False
                        break
                if not resolved or len(classes) != 1:
                    continue
                enum_name = classes.pop()
                if names >= set(table[enum_name]):
                    continue  # exhaustive literal: nothing to explain
                subset = ", ".join(sorted(names))
                out.append(
                    Site(
                        relpath=module.relpath,
                        line=node.lineno,
                        key=(module.relpath, enum_name, subset),
                        detail=(
                            f"{enum_name} subset {{{subset}}} "
                            f"({len(names)} of {len(table[enum_name])} members)"
                        ),
                    )
                )
    return out


def _callee_name(call: ast.Call) -> str:
    """Best-effort name of the thing being called, for messages and keys."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return "<expression>"


def _numeric_constant(node: ast.expr) -> Optional[float]:
    """Return the value of a bare (optionally negated) int/float literal.

    ``bool`` is excluded: ``timeout=True`` is a different mistake and no rule
    here claims to catch it.
    """
    negative = isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
    inner = node.operand if negative else node
    if not isinstance(inner, ast.Constant):
        return None
    if isinstance(inner.value, bool) or not isinstance(inner.value, (int, float)):
        return None
    return -inner.value if negative else inner.value


def bare_timeout_sites(sources: Optional[Sequence[Module]] = None) -> List[Site]:
    """Flag ``timeout=<number>`` keyword arguments written as bare literals.

    Matches any keyword whose name contains ``timeout`` (``timeout``,
    ``read_timeout``, ``timeout_s``), because the defect class is the number,
    not the spelling. A value that is a name, attribute, call or arithmetic
    expression is *not* flagged -- deriving the number from a named constant or
    a setting is the behaviour the rule is asking for, so it must pass silently.
    """
    srcs = parse_sources() if sources is None else sources
    out: List[Site] = []
    for module in srcs:
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg is None or "timeout" not in keyword.arg:
                    continue
                value = _numeric_constant(keyword.value)
                if value is None:
                    continue
                callee = _callee_name(node)
                out.append(
                    Site(
                        relpath=module.relpath,
                        line=node.lineno,
                        key=(module.relpath, f"{callee}({keyword.arg}={value:g})"),
                        detail=f"{callee}({keyword.arg}={value:g}) is a bare literal",
                    )
                )
    return out


def declared_parameters(
    func_name: str, sources: Optional[Sequence[Module]] = None
) -> FrozenSet[str]:
    """Union of every parameter name on every ``def func_name`` under lint.

    Used by the staleness rule to ask "does this contract exist yet?" rather
    than hard-coding the answer. Covers positional, keyword-only and ``*``/``**``
    names so a parameter added behind ``*`` is still seen.
    """
    srcs = parse_sources() if sources is None else sources
    names: set = set()
    for module in srcs:
        for node in ast.walk(module.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != func_name:
                continue
            args = node.args
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                names.add(arg.arg)
            for extra in (args.vararg, args.kwarg):
                if extra is not None:
                    names.add(extra.arg)
    return frozenset(names)


def call_sites(func_name: str, sources: Optional[Sequence[Module]] = None) -> List[Site]:
    """Every call to ``func_name`` under lint, keyed by module and callee."""
    srcs = parse_sources() if sources is None else sources
    out: List[Site] = []
    for module in srcs:
        for node in ast.walk(module.tree):
            if isinstance(node, ast.Call) and _callee_name(node) == func_name:
                passed = sorted(kw.arg for kw in node.keywords if kw.arg)
                out.append(
                    Site(
                        relpath=module.relpath,
                        line=node.lineno,
                        key=(module.relpath, func_name),
                        detail=f"{func_name}(...) keywords={passed or '[]'}",
                    )
                )
    return out


def missing_kwarg_sites(
    func_name: str,
    kwarg: str,
    sources: Optional[Sequence[Module]] = None,
) -> List[Site]:
    """Flag calls to ``func_name`` that do not pass ``kwarg`` explicitly.

    A ``**kwargs`` splat is also flagged, not exempted: the rule cannot see
    inside a dict, so it cannot tell whether the splat carries ``kwarg``, and
    treating "cannot tell" as "passes" would let a splat silently satisfy a
    gate it never verified. A splat site must carry a reviewed opt-out row
    just like any other site that cannot derive the gate.
    """
    srcs = parse_sources() if sources is None else sources
    out: List[Site] = []
    for module in srcs:
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.Call) or _callee_name(node) != func_name:
                continue
            names = {kw.arg for kw in node.keywords}
            if kwarg in names:
                continue
            key = (module.relpath, func_name, kwarg)
            if None in names:
                out.append(
                    Site(
                        relpath=module.relpath,
                        line=node.lineno,
                        key=key,
                        detail=(
                            f"{func_name}(...) passes **kwargs; cannot verify "
                            f"{kwarg}= statically"
                        ),
                    )
                )
                continue
            out.append(
                Site(
                    relpath=module.relpath,
                    line=node.lineno,
                    key=key,
                    detail=f"{func_name}(...) does not pass {kwarg}=",
                )
            )
    return out
