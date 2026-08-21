"""Workflow spec authoring service (issue #312, Bolt 2 / N2).

The core service behind the four authoring CLI verbs (validate / list / get /
delete) and their ``/workflows`` HTTP endpoints. Spec YAML files on disk are the
single source of truth (B2-BR-2); the ``workflow_index`` SQLite table is a
**derived, droppable** projection rebuilt byte-identically from the files alone
(B2-BR-3).

Scope discipline (Q1): this service ships ONLY the author -> persist surface.
``run`` / ``cancel`` / run-``status`` and the implicit-upsert-on-``run`` *trigger*
are NOT here — they land in Bolt 3 with the run engine (N5). The
``upsert_index`` / ``rebuild_index_from_files`` machinery DOES ship and is
exercised by ``list_workflows`` and authoring round-trips.

Path/name validation is never reimplemented (project Mandated rule): directories
go through the shared ``tmux_client._resolve_and_validate_working_directory``;
names go through ``WORKFLOW_NAME_RE`` after a ``basename`` reduction with explicit
``.``/``..`` traversal rejection.

The service raises only NARROW exceptions (``ValueError`` / ``FileNotFoundError`` /
``KeyError``); the API boundary maps them to ``HTTPException`` (B2-BR-9).
"""

from __future__ import annotations

import ast
import glob
import hashlib
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union, cast

import yaml

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import (
    WORKFLOW_INPUT_TYPES,
    WORKFLOW_MAX_SPEC_BYTES,
    WORKFLOW_NAME_RE,
    WORKFLOW_SPEC_DIR,
)
from cli_agent_orchestrator.models.workflow import (
    InputDecl,
    LintFinding,
    ScriptSpec,
    TierCollisionError,
    ValidationResult,
    WorkflowIndexRow,
    WorkflowSpec,
    _default_matches_type,
)
from cli_agent_orchestrator.models.workflow import validate_only as _model_validate_only
from cli_agent_orchestrator.services.script_lint import lint_script

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(WORKFLOW_NAME_RE)


# ---------------------------------------------------------------------------
# Name / path validation (reuses the shared validators — never reimplemented)
# ---------------------------------------------------------------------------
def _validate_name(name: str) -> str:
    """Reduce ``name`` to its basename and match the anchored ``WORKFLOW_NAME_RE``.

    Rejects traversal tokens (``.``/``..``) and any name whose basename differs
    from the input (a path was supplied where a bare name was required). Raises
    ``ValueError`` on rejection (B2-BR-1) -> HTTPException 400 at the boundary.
    """
    if name in (".", ".."):
        raise ValueError(f"workflow name '{name}' is not allowed (traversal token)")
    if os.path.basename(name) != name:
        raise ValueError(f"workflow name '{name}' must not contain path separators")
    if not _NAME_RE.match(name):
        raise ValueError(f"workflow name '{name}' is invalid (must match {WORKFLOW_NAME_RE})")
    return name


def _safe_dir(scan_dir: Optional[str]) -> str:
    """Canonicalize + policy-check a scan directory via the shared validator.

    Defaults to ``WORKFLOW_SPEC_DIR`` when ``scan_dir`` is None, creating it if
    absent so a fresh install has a real (allowed) directory to validate. Raises
    ``ValueError`` if the resolved path is a blocked system directory (B2-BR-1).
    """
    if scan_dir is None:
        WORKFLOW_SPEC_DIR.mkdir(parents=True, exist_ok=True)
        scan_dir = str(WORKFLOW_SPEC_DIR)
    # The shared validator: realpath + absolute-guard + blocked-dir frozenset.
    return tmux_client._resolve_and_validate_working_directory(scan_dir)


def _safe_spec_path(path: Union[str, Path], base_dir: Optional[str] = None) -> str:
    """Canonicalize a spec FILE path and bind it to a CONFIGURED base directory.

    The single guarded entry for turning a user/agent-supplied spec path into a
    real path safe to stat/open. The API contract accepts BOTH absolute and
    relative spec paths (every authoring caller — CLI, HTTP, tests — passes an
    absolute path resolved against its own cwd/tmp fixture): a relative ``path``
    is joined onto the configured base BEFORE resolution, while an absolute
    ``path`` resolves as-is (never re-anchored/stripped) — either way the
    containment check below is what actually gates access, not the shape of
    the input string.

    Deliberately mirrors ``utils/path_validation.py::resolve_and_validate_path``
    — the ``os.path.realpath`` + ``str.startswith`` idiom CodeQL's
    ``py/path-injection`` query already recognizes as a sanitizer in THIS repo
    (that module carries zero open alerts), returning the SAME plain ``str``
    shape that module returns. A ``pathlib``-only rewrite (``Path.resolve()``/
    ``Path.is_relative_to()``, and later a hybrid that still wrapped the
    checked string in ``Path(real_path)`` before returning) was tried and is
    NOT recognized by the same query at any downstream sink that receives the
    wrapped ``Path`` object — CodeQL's sanitizer-then-sink match apparently
    doesn't track taint through a ``Path()`` constructor call, even when its
    argument is the exact checked string. Returning the bare ``str`` (as
    ``path_validation.py`` does) is what every "fixed" alert in this file's
    history has in common. Two stages:

    1. ``os.path.realpath(os.path.abspath(...))`` canonicalizes the path
       (resolves symlinks + ``..``) — the PathNormalization step CodeQL
       tracks.
    2. ``_safe_dir`` policy-checks the base directory (``base_dir`` if given,
       else ``WORKFLOW_SPEC_DIR``) against the blocked-system-directory
       frozenset, then we assert the resolved file lies INSIDE that validated
       base via ``startswith(safe_base + os.sep)`` — the SafeAccessCheck that
       clears the normalized path for the filesystem ops downstream.

    The base is a SEPARATELY-derived configured root, NOT the file's own parent —
    so the containment check is load-bearing: a spec must resolve inside the
    workflow directory (or the caller-supplied ``scan_dir``). A path whose
    realpath escapes that base (e.g. a symlink pointing out, ``..`` traversal,
    or an arbitrary external path) is rejected rather than silently followed.

    Every CodeQL-flagged sink downstream MUST open/stat the value this
    function RETURNS DIRECTLY — never re-derive a path from the original
    string, and never re-wrap the returned string in ``Path(...)`` before the
    sink — so the resolve-then-contain check dominates the sink.

    Returns:
        The resolved, contained realpath ``str`` — the only value callers may
        pass to a filesystem operation.

    Raises:
        ValueError: the base directory is blocked, or the resolved file escapes
            that validated base directory.
    """
    if not path or (isinstance(path, str) and not path.strip()):
        raise ValueError("workflow spec path is required")

    safe_base = _safe_dir(base_dir)  # None -> WORKFLOW_SPEC_DIR; realpath + blocked-dir guard
    user_path = os.fspath(path)
    candidate = user_path if os.path.isabs(user_path) else os.path.join(safe_base, user_path)
    real_path = os.path.realpath(os.path.abspath(candidate))
    if real_path != safe_base and not real_path.startswith(safe_base + os.sep):
        raise ValueError(f"workflow spec path '{path}' escapes its validated directory")
    return real_path


# ---------------------------------------------------------------------------
# Colocated resolve-contain-AND-access helpers (CodeQL py/path-injection)
# ---------------------------------------------------------------------------
# ``_safe_spec_path`` above resolves+contains a path and then RETURNS it. That
# is a genuine traversal defence, but CodeQL's ``py/path-injection`` barrier for
# ``str.startswith`` is *flow-sensitive and function-local*: the "contained"
# state a guard establishes inside ``_safe_spec_path`` is NOT carried across the
# ``return`` to the caller, so an ``open()``/``os.path.isfile()`` sink in the
# CALLER still sees a normalized-but-unchecked path and is (correctly, from the
# query's point of view) flagged — alerts 166/167/168.
#
# The fix is to colocate the containment SafeAccessCheck with the filesystem
# sink in the SAME function, so the guard dominates the sink and the query's
# barrier applies. These helpers own every taint-reachable ``open``/``isfile``
# on a user-supplied spec path; callers receive the *result* (bytes / a bool-ish
# path), never a bare path they must re-open. The guard uses the single positive
# ``startswith(base + os.sep)`` idiom from CodeQL's own "GOOD" example (the
# trailing separator also closes the ``/base`` vs ``/base-evil`` prefix hole).


def _resolve_contained_spec_path(path: Union[str, Path], safe_base: str) -> str:
    """Canonicalize ``path`` and return it ONLY if it resolves under ``safe_base``.

    Pure path math (no filesystem access): mirrors ``_safe_spec_path``'s
    resolution so the two ``open``/``isfile`` helpers below share identical
    semantics. The containment guard itself is intentionally NOT here — it is
    re-asserted inline next to each sink so CodeQL's function-local barrier
    covers the sink.
    """
    if not path or (isinstance(path, str) and not path.strip()):
        raise ValueError("workflow spec path is required")
    user_path = os.fspath(path)
    candidate = user_path if os.path.isabs(user_path) else os.path.join(safe_base, user_path)
    return os.path.realpath(os.path.abspath(candidate))


def _read_contained_spec_bytes(
    path: Union[str, Path], base_dir: Optional[str] = None
) -> tuple[str, bytes]:
    """Resolve + contain + READ a spec file, guard colocated with the sinks.

    Returns ``(real_path, raw)`` where ``raw`` is capped at
    ``WORKFLOW_MAX_SPEC_BYTES + 1`` bytes (callers own the over-cap message and
    the decode). The ``os.path.isfile`` and ``open`` sinks live HERE, right
    after the ``startswith`` containment SafeAccessCheck, so the check dominates
    them within one function (unlike a returned path, whose checked state CodeQL
    drops at the call boundary — the cause of alerts 166/167).

    Raises:
        ValueError: the base directory is blocked or the resolved path escapes it.
        FileNotFoundError: the resolved path is not an existing regular file.
    """
    safe_base = _safe_dir(base_dir)
    real_path = _resolve_contained_spec_path(path, safe_base)
    # SafeAccessCheck — single positive containment guard, colocated with the
    # open() sink below (a spec FILE is always strictly UNDER its base dir).
    if not real_path.startswith(safe_base + os.sep):
        raise ValueError(f"workflow spec path '{path}' escapes its validated directory")
    if not os.path.isfile(real_path):
        raise FileNotFoundError(f"workflow spec not found: {path}")
    with open(real_path, "rb") as fh:
        return real_path, fh.read(WORKFLOW_MAX_SPEC_BYTES + 1)


def _contained_spec_file(path: Union[str, Path], base_dir: Optional[str] = None) -> Optional[str]:
    """Resolve + contain a candidate path; return its realpath IFF it is a file.

    Used by ``get_workflow`` to decide "path vs bare name" without leaking an
    unchecked path to the ``os.path.isfile`` sink (alert 168). The containment
    guard is colocated with the ``isfile`` sink; an escaping path is a
    ``ValueError`` (matching the previous ``_safe_spec_path`` behavior), an
    in-base non-file returns ``None`` (caller falls through to the index lookup).
    """
    safe_base = _safe_dir(base_dir)
    real_path = _resolve_contained_spec_path(path, safe_base)
    # SafeAccessCheck — single positive containment guard, colocated with the
    # isfile sink below. Must match the ``_read_contained_spec_bytes`` form: a
    # COMPOUND ``!= base and not startswith`` guard leaves the ``real_path ==
    # base`` branch reaching the sink un-guarded, which CodeQL (correctly) will
    # not treat as a barrier. A candidate that resolves exactly to the base dir
    # is not a spec file, so rejecting it here is the right behavior anyway.
    if not real_path.startswith(safe_base + os.sep):
        raise ValueError(f"workflow spec path '{path}' escapes its validated directory")
    return real_path if os.path.isfile(real_path) else None


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------
def load_and_validate(path: str, base_dir: Optional[str] = None) -> WorkflowSpec:
    """Load a spec file, validate its grammar, and return the typed model (C2).

    The single read path. The containing directory is policy-checked by the
    shared validator before any read (B2-BR-1). Grammar is checked via Bolt 1's
    ``validate_only`` (which never raises); a ``fail`` result is promoted to a
    ``ValueError`` so the boundary maps it to 400. A ``pass_reserved`` spec loads
    successfully — reserved-ness is not a load error (Bolt-1 BR-3).

    The file is read EXACTLY ONCE: the same decoded text is fed to grammar
    validation and to model construction. Reading twice (validate the path, then
    re-open it) opened a TOCTOU window — validate could pass on revision A while
    the second read loaded revision B that never cleared grammar validation
    (PR #326 review). One read, one parse, no window.

    Raises:
        FileNotFoundError: the path is not an existing file.
        ValueError: the directory is blocked, the file is unreadable, or the
            spec fails grammar validation.
    """
    # Resolve + contain + read behind ONE guarded helper: the containment
    # SafeAccessCheck is colocated with the open() sink inside the helper, so no
    # unchecked path reaches a filesystem op here (clears alert 166). The file is
    # read EXACTLY ONCE; the capped bytes feed BOTH validation and construction.
    _real_path, raw = _read_contained_spec_bytes(path, base_dir)
    if len(raw) > WORKFLOW_MAX_SPEC_BYTES:
        raise ValueError(f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"spec is not valid UTF-8: {e}") from e

    result = _model_validate_only(text)  # raw text, not path; NEVER raises (BR-7)
    if result.status == "fail":
        raise ValueError("; ".join(result.errors) or "spec failed validation")

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("spec root must be a mapping (YAML object)")
    # WorkflowSpec construction re-runs grammar validation; it cannot fail here
    # because validate_only already passed, but the typed model is the contract.
    return WorkflowSpec(**data)


def validate_only(path: str, base_dir: Optional[str] = None) -> ValidationResult:
    """Read a spec file behind the path guard and validate its grammar (FR-1.3).

    The path is canonicalized + bound to its configured base directory first
    (B2-BR-1) so an out-of-policy path is a ``ValueError`` (-> 400). The file is
    read here (behind the guard) and only its decoded TEXT is handed to the
    model's text-only ``validate_only`` — the model never touches the filesystem
    (removes the path-injection sink at the source). A missing/unreadable file
    becomes a ``fail`` ValidationResult so the surface still NEVER raises for a
    well-formed-but-absent spec, matching the model's never-raise contract.

    Raises:
        ValueError: the base directory is blocked or the path escapes it.
    """
    # Resolve + contain + read behind the guarded helper (open sink colocated
    # with the containment check — clears alert 167). An escaping/blocked path is
    # a ValueError (-> 400); a missing/unreadable file degrades to a ``fail``
    # result so the surface still NEVER raises for a well-formed-but-absent spec.
    try:
        _real_path, raw = _read_contained_spec_bytes(path, base_dir)
    except OSError as exc:
        # FileNotFoundError (missing spec) and any other read error degrade to a
        # ``fail`` result — validate_only NEVER raises for an absent spec.
        logger.debug("validate_only: could not read spec %s: %s", path, exc)
        return ValidationResult(status="fail", errors=[f"could not read spec: {exc}"])
    if len(raw) > WORKFLOW_MAX_SPEC_BYTES:
        return ValidationResult(
            status="fail",
            errors=[f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)"],
        )
    return _model_validate_only(raw.decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Index machinery (derived, droppable — B2-BR-2/B2-BR-3)
# ---------------------------------------------------------------------------
def _connect():
    """Open a short-lived SQLite connection to the shared DB file."""
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    return sqlite3.connect(str(DATABASE_FILE))


def upsert_index(spec: Union[WorkflowSpec, ScriptSpec], source_path: str) -> None:
    """Idempotently materialize a spec into ``workflow_index`` (C2, FR-2.3).

    Keyed by ``name`` (ON CONFLICT DO UPDATE) so re-authoring the same spec
    updates the row in place rather than duplicating. ``source_path`` MUST
    already be the resolved, contained realpath ``str`` a caller got back
    from ``_safe_spec_path`` — this function stores it as-is, with NO
    re-derivation (no ``os.path.realpath`` re-run, no wrapping/unwrapping),
    which would re-introduce an unchecked path string into the value later
    read back by ``_resolve_source_path`` and fed to a filesystem sink.
    ``indexed_at`` is derived bookkeeping (ISO-8601 Z), never an ordering key
    (B2-BR-3 orders by ``name``).

    A ``ScriptSpec`` (U5, A2) indexes with ``mode="script"`` and
    ``step_count=None`` — step count is run-time-determined and unknowable at
    index time (BR-4). A ``WorkflowSpec`` keeps the unchanged YAML behavior.
    """
    if isinstance(spec, ScriptSpec):
        mode = "script"
        step_count: Optional[int] = None
        description = ""
    else:
        mode = spec.mode
        step_count = len(spec.steps)
        description = spec.description
    row = WorkflowIndexRow(
        name=spec.name,
        source_path=source_path,
        mode=mode,
        step_count=step_count,
        description=description,
        indexed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    with _connect() as conn:
        conn.execute(
            "INSERT INTO workflow_index "
            "(name, source_path, mode, step_count, description, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "source_path=excluded.source_path, mode=excluded.mode, "
            "step_count=excluded.step_count, description=excluded.description, "
            "indexed_at=excluded.indexed_at",
            (
                row.name,
                row.source_path,
                row.mode,
                row.step_count,
                row.description,
                row.indexed_at,
            ),
        )
        conn.commit()


def rebuild_index_from_files(scan_dir: Optional[str] = None) -> int:
    """Full-rebuild ``workflow_index`` from the spec files in ``scan_dir`` (C1a, A2).

    The index is disposable: DELETE everything, then re-materialize from the
    files in a **stable** (case-sensitive filename) sort so the resulting listing
    is byte-identical across drop+relist (B2-BR-3). An unparseable YAML spec is
    SKIPPED and logged — it never appears in the listing in either run, so
    identity is preserved. A same-stem cross-tier collision (BR-2) is skipped
    from indexing (not raised — a collision is rejected at ACCESS time, in
    ``get_workflow``, not at scan time, so other names still index).

    Returns the number of rows rebuilt.
    """
    safe_dir = _safe_dir(scan_dir)
    yaml_paths = sorted(
        glob.glob(os.path.join(safe_dir, "*.yaml")) + glob.glob(os.path.join(safe_dir, "*.yml"))
    )
    py_paths = sorted(glob.glob(os.path.join(safe_dir, "*.py")))
    with _connect() as conn:
        conn.execute("DELETE FROM workflow_index")
        conn.commit()
    rows = 0
    for path in yaml_paths:
        try:
            # Bind containment to the SAME dir we globbed from (not WORKFLOW_SPEC_DIR)
            # so a caller-supplied scan_dir resolves its own specs. The glob
            # string itself is untrusted until re-validated — resolve it via
            # _safe_spec_path and store THAT (not the raw glob string) in the
            # index, matching the .py loop below.
            real_path = _safe_spec_path(path, base_dir=safe_dir)
            spec = load_and_validate(real_path, base_dir=safe_dir)
        except (ValueError, FileNotFoundError) as e:
            logger.warning("rebuild: skipping unparseable spec %s: %s", path, e)
            continue
        upsert_index(spec, real_path)
        rows += 1
    for path in py_paths:
        stem = _stem_of(path)
        try:
            _check_tier_collision(stem, safe_dir)
        except TierCollisionError as e:
            logger.warning("rebuild: skipping colliding script spec %s: %s", path, e)
            continue
        try:
            # Bind containment to the SAME dir we globbed from, mirroring the
            # YAML loop above — the glob string is untrusted until re-validated
            # against safe_dir; the resolved realpath this returns is the ONLY
            # value passed to _read_script_spec (never the raw glob string).
            real_path = _safe_spec_path(path, base_dir=safe_dir)
            script_spec = _read_script_spec(real_path, stem, base_dir=safe_dir)
        except (ValueError, OSError, UnicodeDecodeError) as e:
            logger.warning("rebuild: skipping unreadable script spec %s: %s", path, e)
            continue
        upsert_index(script_spec, real_path)
        rows += 1
    return rows


def list_workflows(scan_dir: Optional[str] = None) -> List[WorkflowIndexRow]:
    """List indexed workflows, rebuilding the index if missing/stale (FR-2.1).

    Always rebuilds from the files before listing: the files are canonical
    (B2-BR-2), so a transparent rebuild guarantees the listing reflects disk and
    is byte-identical after a manual drop. Rows are returned ``ORDER BY name`` —
    the single ordering key the byte-identity invariant rests on (B2-BR-3).

    COST CEILING: each of ``list`` / ``get`` / ``delete`` triggers a FULL O(n)
    rebuild (glob + n reads + n parses + n upserts). Fine for the handful of
    specs Bolt 2 targets, but a future caller (e.g. the run engine) MUST NOT call
    ``get_workflow`` in a loop — a 100-step workflow would be 100 rebuilds =
    O(n²) reads. Resolve the spec once and pass it down instead.
    """
    rebuild_index_from_files(scan_dir)
    with _connect() as conn:
        cursor = conn.execute(
            "SELECT name, source_path, mode, step_count, description, indexed_at "
            "FROM workflow_index ORDER BY name"
        )
        return [
            WorkflowIndexRow(
                name=r[0],
                source_path=r[1],
                mode=r[2],
                step_count=r[3],
                description=r[4],
                indexed_at=r[5],
            )
            for r in cursor.fetchall()
        ]


def _resolve_source_path(name: str, scan_dir: Optional[str] = None) -> str:
    """Return the canonical YAML path for an indexed workflow ``name``.

    Rebuilds the index first so the lookup reflects disk. Raises ``KeyError`` if
    no workflow with that name exists (B2-BR-9) -> HTTPException 404.
    """
    _validate_name(name)
    rebuild_index_from_files(scan_dir)
    with _connect() as conn:
        row = conn.execute(
            "SELECT source_path FROM workflow_index WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        raise KeyError(name)
    return str(row[0])


def render_findings(findings: List[LintFinding]) -> List[dict]:
    """Render ``LintFinding`` values into the run route's 422 findings body.

    The validate route returns ``lint_script(...).model_dump()`` directly; this
    helper is used when ``ScriptLintError`` must be mapped to an HTTP error.
    """
    return [finding.model_dump() for finding in findings]


def _stem_of(path: str) -> str:
    """Return the file stem (basename minus extension) for tier/collision keys."""
    return os.path.splitext(os.path.basename(path))[0]


def _check_tier_collision(stem: str, safe_dir: str) -> None:
    """Raise ``TierCollisionError`` if ``stem`` exists in BOTH tiers in ``safe_dir``.

    A same-stem sibling across the ``.py`` / ``.yaml`` / ``.yml`` extensions
    within one scan dir is a rejected collision (BR-2) — never resolved by
    precedence. Consulted by both the access-time (A1) and scan-time (A2)
    paths.
    """
    siblings = glob.glob(os.path.join(safe_dir, f"{stem}.yaml")) + glob.glob(
        os.path.join(safe_dir, f"{stem}.yml")
    )
    if siblings:
        raise TierCollisionError(stem)


def _extract_inputs(source: str) -> Dict[str, InputDecl]:
    """AST-parse a script's module-level ``INPUTS`` declaration (Unit A, FR-A1).

    Finds the FIRST module-level assignment to the name ``INPUTS`` and builds the
    typed ``InputDecl`` map the run-path validator (``_validate_inputs``) consumes.
    NEVER executes or imports the module — this is a pure ``ast`` walk (M2, the
    no-execution + HTTP-only guarantee), so a script with import-time side effects
    is parsed, not run.

    Rules (BR-A1/BR-A2):
    - Unparseable source (``SyntaxError``) -> ``ValueError`` (mapped to 400 at the
      run route; caught by ``rebuild_index_from_files``). ``SyntaxError`` is NOT a
      ``ValueError`` subclass, so it is re-raised as one explicitly.
    - No module-level ``INPUTS`` -> ``{}`` (INPUTS is OPTIONAL).
    - ``INPUTS`` must be a dict literal; each key a string; each value a dict
      literal with keys ``⊆ {type, required, default}``; ``type`` one of
      ``WORKFLOW_INPUT_TYPES``; a default whose type disagrees with ``type`` is a
      ``ValueError`` (reuses the shared author-time ``_default_matches_type``).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        # SyntaxError is not a ValueError subclass; map it so the run-route
        # boundary (ValueError -> 400) and rebuild's ``except ValueError`` catch it.
        raise ValueError(f"malformed workflow script: {e}") from e

    inputs_node: Optional[ast.expr] = None
    for stmt in tree.body:  # module-level statements only (no nested scopes)
        if isinstance(stmt, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == "INPUTS" for t in stmt.targets):
                inputs_node = stmt.value
                break
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
            if isinstance(target, ast.Name) and target.id == "INPUTS" and stmt.value is not None:
                inputs_node = stmt.value
                break

    if inputs_node is None:
        return {}  # INPUTS is optional (BR-A1)

    if not isinstance(inputs_node, ast.Dict):
        raise ValueError("INPUTS must be a dict literal")

    result: Dict[str, InputDecl] = {}
    for key_node, value_node in zip(inputs_node.keys, inputs_node.values):
        if key_node is None:  # ``{**spread}`` has a None key — not a literal entry
            raise ValueError("INPUTS must be a dict literal (no ** unpacking)")
        try:
            key = ast.literal_eval(key_node)
        except (ValueError, SyntaxError) as e:
            raise ValueError(f"INPUTS key is not a literal: {e}") from e
        if not isinstance(key, str):
            raise ValueError(f"INPUTS key {key!r} must be a string")
        if not isinstance(value_node, ast.Dict):
            raise ValueError(f"INPUTS['{key}'] must be a dict literal")

        fields: Dict[str, object] = {}
        for fk_node, fv_node in zip(value_node.keys, value_node.values):
            if fk_node is None:
                raise ValueError(f"INPUTS['{key}'] must be a dict literal (no ** unpacking)")
            try:
                fk = ast.literal_eval(fk_node)
            except (ValueError, SyntaxError) as e:
                raise ValueError(f"INPUTS['{key}'] has a non-literal key: {e}") from e
            if fk not in ("type", "required", "default"):
                raise ValueError(
                    f"INPUTS['{key}'] has unexpected key '{fk}' "
                    "(allowed: type, required, default)"
                )
            try:
                fields[fk] = ast.literal_eval(fv_node)
            except (ValueError, SyntaxError) as e:
                raise ValueError(f"INPUTS['{key}']['{fk}'] is not a literal: {e}") from e

        declared_type = fields.get("type")
        if declared_type not in WORKFLOW_INPUT_TYPES:
            raise ValueError(
                f"INPUTS['{key}'] type {declared_type!r} is invalid "
                f"(allowed: {', '.join(WORKFLOW_INPUT_TYPES)})"
            )
        default = cast(Union[str, int, bool, None], fields.get("default"))
        if default is not None and not _default_matches_type(default, str(declared_type)):
            raise ValueError(
                f"INPUTS['{key}'] default {default!r} does not match declared "
                f"type '{declared_type}'"
            )
        result[key] = InputDecl(**fields)  # type: ignore[arg-type]

    return result


def _read_script_spec(path: str, stem: str, base_dir: Optional[str] = None) -> ScriptSpec:
    """Read + lint a ``.py`` spec file into a ``ScriptSpec`` (A1, E1).

    Re-validates ``path`` through ``_safe_spec_path`` itself — this is the
    ONLY entry that opens a ``.py`` spec file, and it must stay safe no matter
    which caller reaches it. Some callers (``get_workflow``'s bare-name arm,
    via ``_resolve_source_path``) hand back a plain string pulled from the
    SQLite index rather than an already-validated path, so re-validating HERE
    — not trusting the caller to have done it — is what keeps every ``.py``
    open() sink covered by the resolve-then-contain check regardless of call
    site.

    The load-time lint (U1) is INFORMATIONAL only — feeds ``validate``/
    ``list``/``get`` rendering (BR-6); it is a SEPARATE call from U4's
    run-path defensive re-check.
    """
    # Read behind the guarded helper: the containment SafeAccessCheck is
    # colocated with the open() sink inside ``_read_contained_spec_bytes`` (never
    # trust the caller to have validated ``path`` — the bare-name arm hands back
    # a raw string read out of SQLite). This is the ONLY entry that opens a
    # ``.py`` spec file.
    real_path, raw = _read_contained_spec_bytes(path, base_dir)
    if len(raw) > WORKFLOW_MAX_SPEC_BYTES:
        raise ValueError(f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (short-circuited read)")
    display_path = real_path
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"spec is not valid UTF-8: {e}") from e
    content_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    result = lint_script(source, display_path)
    # Unit A: extract the typed INPUTS declaration (AST-only, never executed).
    # A malformed INPUTS raises ValueError, propagating exactly as a bad YAML
    # spec does (-> 400 at the run route / skipped in rebuild).
    #
    # LOAD-PATH graceful degradation: if the lint pass already recorded a
    # ``syntax`` finding, the source has no parseable AST — there is nothing for
    # ``_extract_inputs`` to walk, and re-raising here would abort the load and
    # DROP that informational finding (BR-6). So we SKIP extraction and let the
    # syntax finding stand (spec.inputs = {}). A syntactically VALID script with
    # a bad INPUTS literal has no syntax finding, so ``_extract_inputs`` still
    # runs and still raises ValueError — the real author error the load path
    # must surface. The run path stays fail-closed via ``_validate_inputs``.
    if any(f.rule_id == "syntax" for f in result.findings):
        inputs: Dict[str, InputDecl] = {}
    else:
        inputs = _extract_inputs(source)
    return ScriptSpec(
        name=stem,
        path=display_path,
        source=source,
        content_hash=content_hash,
        findings=result.findings,
        inputs=inputs,
    )


def get_workflow(
    name_or_path: str, scan_dir: Optional[str] = None
) -> Union[WorkflowSpec, ScriptSpec]:
    """Return the parsed/validated spec for a workflow name or a file path (C4, A1).

    Extension-based tier dispatch (FR-4.2): ``.yaml``/``.yml`` resolves via the
    UNCHANGED YAML path (byte-identical, FR-5.1); ``.py`` resolves to a
    ``ScriptSpec`` — collision-checked (BR-2) THEN read THEN load-time-linted
    (BR-6) — before construction. Raises ``KeyError`` for an unknown name
    (-> 404), ``TierCollisionError`` for a same-stem cross-tier sibling
    (-> 409), ``ValueError`` for an unrecognized extension (-> 400),
    ``FileNotFoundError`` / ``ValueError`` as ``load_and_validate`` does for
    the YAML arm.
    """
    # A path-like argument is canonicalized + bound to its configured base
    # directory BEFORE the stat (never stat raw user input); a bare name falls
    # through to the index lookup. ``_contained_spec_file`` colocates the
    # containment guard with its ``os.path.isfile`` sink (clears alert 168) and
    # returns the contained realpath only when it names an existing file; a
    # blocked/escaping path raises ValueError.
    if os.sep in name_or_path or (os.altsep and os.altsep in name_or_path):
        safe_path = _contained_spec_file(name_or_path, scan_dir)
        if safe_path is not None:
            return _load_by_extension(safe_path, scan_dir)
    # The resolved source_path lives under scan_dir (the index was rebuilt from
    # it), so bind containment to that same dir on load.
    source_path = _resolve_source_path(name_or_path, scan_dir)
    return _load_by_extension(source_path, scan_dir)


def _load_by_extension(real_path: str, scan_dir: Optional[str]) -> Union[WorkflowSpec, ScriptSpec]:
    """Extension-based dispatch shared by both ``get_workflow`` call sites (A1)."""
    ext = os.path.splitext(real_path)[1].lower()
    if ext in (".yaml", ".yml"):
        return load_and_validate(real_path, base_dir=scan_dir)  # UNCHANGED, FR-5.1
    if ext == ".py":
        safe_dir = _safe_dir(scan_dir)
        stem = _stem_of(real_path)
        _check_tier_collision(stem, safe_dir)  # -> TierCollisionError (409)
        # ``real_path`` may still be an UNVALIDATED string here (the bare-name
        # arm hands back whatever ``_resolve_source_path`` read out of SQLite);
        # ``_read_script_spec`` re-validates it against ``scan_dir`` itself
        # before opening — never trust this call site's naming.
        return _read_script_spec(real_path, stem, base_dir=scan_dir)
    raise ValueError(f"unrecognized spec extension: {ext}")


def delete_workflow(name: str, scan_dir: Optional[str] = None) -> None:
    """Delete a workflow's canonical YAML file and its index row (FR-2.4, B2-BR-4).

    Files are canonical, so removing the YAML is the authoritative act; the index
    row removal is bookkeeping (rebuild would also drop it). An unknown name
    raises ``KeyError`` -> 404; a repeat delete of an already-removed name is a
    404, not a silent success (the unknown name is surfaced, not masked).
    ``_resolve_source_path`` returns a raw string pulled out of SQLite — the
    SAME shape of value ``_read_script_spec`` re-validates before its own
    sink — so this function re-validates it through ``_safe_spec_path`` too
    before ``os.remove``, rather than trusting the index row is still
    in-policy (a reconfigured ``scan_dir`` or direct DB write could otherwise
    let ``os.remove`` follow an unchecked path).
    """
    source_path = _safe_spec_path(_resolve_source_path(name, scan_dir), scan_dir)
    try:
        os.remove(source_path)
    except FileNotFoundError:
        # The index row pointed at a now-missing file. Drop the stale row and
        # surface the unknown name rather than masking it as success.
        with _connect() as conn:
            conn.execute("DELETE FROM workflow_index WHERE name = ?", (name,))
            conn.commit()
        raise KeyError(name)
    except OSError as e:
        raise ValueError(f"could not delete workflow '{name}': {e}") from e
    with _connect() as conn:
        conn.execute("DELETE FROM workflow_index WHERE name = ?", (name,))
        conn.commit()
