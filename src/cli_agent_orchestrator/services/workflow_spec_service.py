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

import glob
import logging
import os
import re
from datetime import datetime, timezone
from typing import List, Optional

import yaml

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import (
    WORKFLOW_MAX_SPEC_BYTES,
    WORKFLOW_NAME_RE,
    WORKFLOW_SPEC_DIR,
)
from cli_agent_orchestrator.models.workflow import (
    ValidationResult,
    WorkflowIndexRow,
    WorkflowSpec,
)
from cli_agent_orchestrator.models.workflow import validate_only as _model_validate_only

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


def _safe_spec_path(path: str, base_dir: Optional[str] = None) -> str:
    """Canonicalize a spec FILE path and bind it to a CONFIGURED base directory.

    The single guarded entry for turning a user/agent-supplied spec path into a
    real path safe to stat/open. Two stages, mirroring the shared working-dir
    validator (the CodeQL ``py/path-injection`` two-state model — see
    ``tmux.py``):

    1. ``os.path.realpath`` canonicalizes the path (resolves symlinks + ``..``).
       This is the PathNormalization step.
    2. ``_safe_dir`` policy-checks the base directory (``base_dir`` if given,
       else ``WORKFLOW_SPEC_DIR``) against the blocked-system-directory
       frozenset, then we assert the resolved file lies INSIDE that validated
       base via ``startswith`` — the SafeAccessCheck that clears the normalized
       path for the filesystem ops downstream.

    The base is a SEPARATELY-derived configured root, NOT the file's own parent —
    so the containment check is load-bearing: a spec must resolve inside the
    workflow directory (or the caller-supplied ``scan_dir``). A path whose
    realpath escapes that base (e.g. a symlink pointing out, ``..`` traversal, or
    an arbitrary external path) is rejected rather than silently followed.

    Raises:
        ValueError: the base directory is blocked, or the resolved file escapes
            that validated base directory.
    """
    real_path = os.path.realpath(os.path.abspath(path))
    safe_base = _safe_dir(base_dir)  # None -> WORKFLOW_SPEC_DIR; realpath + blocked-dir guard
    if real_path != safe_base and not real_path.startswith(safe_base + os.sep):
        raise ValueError(f"workflow spec path '{path}' escapes its validated directory")
    return real_path


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
    # Canonicalize + bind the file to its configured base directory (rejects a
    # blocked base or a path that escapes it) before any stat/open.
    real_path = _safe_spec_path(path, base_dir)

    if not os.path.isfile(real_path):
        raise FileNotFoundError(f"workflow spec not found: {path}")

    # Single read: byte-cap, decode, then reuse the text for BOTH validation and
    # construction so they see byte-identical content (closes the TOCTOU window).
    with open(real_path, "rb") as fh:
        raw = fh.read()
    if len(raw) > WORKFLOW_MAX_SPEC_BYTES:
        raise ValueError(f"spec is {len(raw)} bytes (max {WORKFLOW_MAX_SPEC_BYTES})")
    text = raw.decode("utf-8")

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
    real_path = _safe_spec_path(path, base_dir)
    try:
        with open(real_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        logger.debug("validate_only: could not read spec %s: %s", real_path, exc)
        return ValidationResult(status="fail", errors=[f"could not read spec: {exc}"])
    if len(raw) > WORKFLOW_MAX_SPEC_BYTES:
        return ValidationResult(
            status="fail",
            errors=[f"spec is {len(raw)} bytes (max {WORKFLOW_MAX_SPEC_BYTES})"],
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


def upsert_index(spec: WorkflowSpec, source_path: str) -> None:
    """Idempotently materialize a spec into ``workflow_index`` (C2, FR-2.3).

    Keyed by ``name`` (ON CONFLICT DO UPDATE) so re-authoring the same spec
    updates the row in place rather than duplicating. ``source_path`` is the
    ``realpath`` of the canonical YAML; ``indexed_at`` is derived bookkeeping
    (ISO-8601 Z), never an ordering key (B2-BR-3 orders by ``name``).
    """
    row = WorkflowIndexRow(
        name=spec.name,
        source_path=os.path.realpath(source_path),
        mode=spec.mode,
        step_count=len(spec.steps),
        description=spec.description,
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
    """Full-rebuild ``workflow_index`` from the YAML files in ``scan_dir`` (C1a).

    The index is disposable: DELETE everything, then re-materialize from the
    files in a **stable** (case-sensitive filename) sort so the resulting listing
    is byte-identical across drop+relist (B2-BR-3). An unparseable spec is
    SKIPPED and logged — it never appears in the listing in either run, so
    identity is preserved.

    Returns the number of rows rebuilt.
    """
    safe_dir = _safe_dir(scan_dir)
    paths = sorted(
        glob.glob(os.path.join(safe_dir, "*.yaml")) + glob.glob(os.path.join(safe_dir, "*.yml"))
    )
    with _connect() as conn:
        conn.execute("DELETE FROM workflow_index")
        conn.commit()
    rows = 0
    for path in paths:
        try:
            # Bind containment to the SAME dir we globbed from (not WORKFLOW_SPEC_DIR)
            # so a caller-supplied scan_dir resolves its own specs.
            spec = load_and_validate(path, base_dir=safe_dir)
        except (ValueError, FileNotFoundError) as e:
            logger.warning("rebuild: skipping unparseable spec %s: %s", path, e)
            continue
        upsert_index(spec, path)
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


def get_workflow(name_or_path: str, scan_dir: Optional[str] = None) -> WorkflowSpec:
    """Return the parsed/validated spec for a workflow name or a file path (C2).

    If ``name_or_path`` points at an existing file, it is loaded directly;
    otherwise it is treated as an indexed workflow name and resolved to its
    canonical source path. Raises ``KeyError`` for an unknown name (-> 404),
    ``FileNotFoundError`` / ``ValueError`` as ``load_and_validate`` does.
    """
    # A path-like argument is canonicalized + bound to its configured base
    # directory BEFORE the stat (never stat raw user input); a bare name falls
    # through to the index lookup. A blocked/escaping path raises ValueError.
    if os.sep in name_or_path or (os.altsep and os.altsep in name_or_path):
        safe_path = _safe_spec_path(name_or_path, scan_dir)
        if os.path.isfile(safe_path):
            return load_and_validate(safe_path, base_dir=scan_dir)
    # The resolved source_path lives under scan_dir (the index was rebuilt from
    # it), so bind containment to that same dir on load.
    source_path = _resolve_source_path(name_or_path, scan_dir)
    return load_and_validate(source_path, base_dir=scan_dir)


def delete_workflow(name: str, scan_dir: Optional[str] = None) -> None:
    """Delete a workflow's canonical YAML file and its index row (FR-2.4, B2-BR-4).

    Files are canonical, so removing the YAML is the authoritative act; the index
    row removal is bookkeeping (rebuild would also drop it). An unknown name
    raises ``KeyError`` -> 404; a repeat delete of an already-removed name is a
    404, not a silent success (the unknown name is surfaced, not masked). Delete
    never removes anything outside the validated spec directory (the source path
    came from the policy-checked rebuild).
    """
    source_path = _resolve_source_path(name, scan_dir)
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
