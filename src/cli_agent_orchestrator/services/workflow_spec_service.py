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
import stat
import tempfile
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

# Mode applied to a NEWLY created spec file (issue #583, Bolt 3, SR-3A2-6).
# A workflow spec is ordinary user-authored source, so a CAO-written file should be
# indistinguishable from a hand-written one. This constant is required rather than
# optional: ``tempfile.mkstemp`` creates 0600 and ``os.replace`` preserves the temp
# file's mode, so WITHOUT an explicit chmod every CAO-created spec would silently be
# owner-only. An UPDATE preserves whatever mode the existing file had instead.
_SPEC_FILE_CREATE_MODE = 0o644


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


def _write_contained_spec_bytes(
    path: Union[str, Path], data: bytes, base_dir: Optional[str] = None
) -> str:
    """Resolve + contain + atomically WRITE a spec file, guard colocated with the sinks.

    The single guarded entry that writes bytes to a spec path, and the only one
    (issue #583, Bolt 3, ADR-583-11). Companion to ``_read_contained_spec_bytes``
    and ``_contained_spec_file`` above, and it exists for the same reason they do:
    CodeQL's ``py/path-injection`` barrier for ``str.startswith`` is
    **flow-sensitive and function-local**, so the "contained" state
    ``_safe_spec_path`` establishes is NOT carried across its ``return``. Every
    filesystem sink below therefore sits in THIS function, after the containment
    ``startswith`` check written HERE — alerts 166/167/168 were caused by exactly
    the alternative.

    Three shapes are forbidden because they defeat the query rather than because
    they are logically wrong (see the block comment above ``_resolve_contained_spec_path``):

    - a COMPOUND ``!= base and not startswith`` guard, which leaves the
      ``real_path == base`` branch reaching a sink un-guarded;
    - wrapping the checked string in ``Path(...)`` before any sink or on the
      return, which the query does not track through;
    - delegating the check to a helper and trusting its return.

    Ordering is load-bearing and every step is placed deliberately:

    1. **Reject a symlink target BEFORE resolution** (SR-3A2-2). ``realpath``
       collapses links, so a check after it can never see one — and following a
       link on a WRITE means the caller's bytes land in a spec it did not name,
       which containment cannot catch because both paths are inside the base.
       This is the one operation here that legitimately reads the caller's own
       string rather than the resolved path; it neither opens nor writes.
    2. **Containment guard.** The SafeAccessCheck, dominating every sink below.
    3. **Size cap BEFORE any file is created** (SR-3A2-3). An oversized payload
       must not leave a temp file behind, and the bound is the SAME constant the
       read path enforces so this cannot write a spec ``load_and_validate``
       would then refuse.
    4. **Read the existing mode while the original inode still exists**
       (SR-3A2-6). ``mkstemp`` creates 0600, so without re-applying the previous
       mode every CAO write would silently make a spec owner-only.
    5. **Temp file inside the validated base** (BR-3A2-7) — required so
       ``os.replace`` is same-filesystem (hence atomic) and so the intermediate
       artefact stays inside the containment argument. Its name comes from
       ``mkstemp`` with a LEADING-DOT prefix, which cannot match the
       ``*.yaml``/``*.yml``/``*.py`` globs ``rebuild_index_from_files`` runs on
       every list/get/delete — a matching name would be indexed while
       half-written (SR-3A2-5).
    6. **flush + fsync before the rename** (TS-3A2-4). The index is a derived
       projection rebuilt from the files (B2-BR-3), so a crash leaving an index
       row pointing at content that never reached disk inverts the
       file-is-canonical invariant.
    7. **``os.replace``**, never in-place ``open(target, "w")`` (which truncates
       and exposes an empty file) and never ``shutil.move`` (which copies across
       filesystems, non-atomically).
    8. **Unlink the temp file on ANY failure**, or a chmod/disk-full error
       orphans it indefinitely.

    NOT done here, both deliberate with named owners (SR-3A2-8): no grammar
    validation — the caller validates the in-memory text and passes THOSE bytes,
    keeping validate-and-write on one read (the TOCTOU window
    ``load_and_validate`` closed); and no redaction — a spec is source the user
    expects back verbatim, and NFR-1's redaction obligation attaches to the
    manifest, not to user source.

    Lost updates are NOT prevented (SR-3A2-7): ``os.replace`` stops a reader
    seeing a partial file, but two writers that both read v1 and both write leave
    the last one, silently. The mitigation is the caller's expected-source-hash
    check, not a lock here.

    Returns:
        The resolved, contained realpath ``str`` the bytes were written to — the
        same bare-``str`` shape the read helpers return.

    Raises:
        ValueError: the base directory is blocked, the resolved path escapes it,
            the target is a symlink, or the payload exceeds the cap.
        OSError: the write itself failed (the temp file is cleaned up first).
    """
    if not path or (isinstance(path, str) and not path.strip()):
        raise ValueError("workflow spec path is required")

    safe_base = _safe_dir(base_dir)

    # (1) Symlink check on the CALLER's path, before realpath collapses it.
    user_path = os.fspath(path)
    if os.path.islink(user_path):
        raise ValueError(f"workflow spec path '{path}' is a symlink; refusing to write through it")

    real_path = _resolve_contained_spec_path(path, safe_base)
    # (2) SafeAccessCheck — single positive containment guard, colocated with every
    # sink below. A spec FILE is always strictly UNDER its base dir.
    if not real_path.startswith(safe_base + os.sep):
        raise ValueError(f"workflow spec path '{path}' escapes its validated directory")

    # (3) Bound the payload before anything touches the filesystem.
    if len(data) > WORKFLOW_MAX_SPEC_BYTES:
        raise ValueError(f"spec exceeds {WORKFLOW_MAX_SPEC_BYTES} bytes (max)")

    # (4) Capture the existing mode while the original inode is still there.
    #
    # CORRECTED during this unit's own tests: doing nothing here does NOT yield the
    # process umask. ``mkstemp`` creates 0600 and ``os.replace`` preserves the temp
    # file's mode, so a freshly-created spec would land owner-only — the exact
    # outcome SR-3A2-6 rejects. A new file therefore gets an EXPLICIT mode.
    #
    # 0644 is used rather than a umask-derived value deliberately: reading the umask
    # requires the ``os.umask(0)``-then-restore idiom, and ``os.umask`` is
    # process-global, so that read-restore window is a genuine race inside a
    # FastAPI server. A deterministic mode is worth more than honouring a
    # restrictive umask here, and the trade is recorded rather than hidden.
    existing_mode: int = _SPEC_FILE_CREATE_MODE
    if os.path.isfile(real_path):
        existing_mode = stat.S_IMODE(os.stat(real_path).st_mode)

    # (5) Temp file inside the validated base, with a name no index glob can match.
    fd, tmp_name = tempfile.mkstemp(prefix=f".{os.path.basename(real_path)}.", dir=safe_base)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())  # (6) durable before the name points at it
        os.chmod(tmp_name, existing_mode)
        os.replace(tmp_name, real_path)  # (7) atomic for readers
    except BaseException:
        # (8) Never orphan the temp file.
        try:
            os.unlink(tmp_name)
        except OSError:
            logger.debug("could not remove temp spec file after a failed write")
        raise
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
    """Open a short-lived SQLite connection to the shared DB file.

    Every connection carries an explicit ``busy_timeout`` (BR-3A1-1). At the
    configured value this pragma is a runtime NO-OP — CPython's
    ``sqlite3.connect()`` already applies a 5000 ms busy timeout through its
    ``timeout=5.0`` default, so this module's effective posture was ALREADY
    identical to the journal's before this statement existed. It is set anyway
    for the two reasons ``workflow_journal._connect`` records for its own: the
    value gets one named home that can be revised without editing this module,
    and the guarantee survives a future caller passing ``timeout=0`` or a change
    to that stdlib default. Neither is a present defect; both are regressions
    this makes impossible.

    The timeout is interpolated from ``WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS`` and
    from nothing else (BR-3A1-2, SR-3A1-1): SQLite accepts no bound parameter for
    ``PRAGMA busy_timeout``, so this is this module's one interpolated statement
    and its source must stay a trusted constant. There is deliberately no
    environment override — one would restore the ``timeout=0`` hole this closes.

    A failing pragma DEGRADES rather than denying service (SR-3A1-3): it is logged
    at ``warning`` and the connection is returned anyway, because the connection is
    still usable and still carries the stdlib default. Propagating would convert an
    unobserved pragma failure into a hard failure of every ``list`` / ``get`` /
    ``delete`` and index write in this module — a self-inflicted outage in exchange
    for defending a guarantee the connection still has. Only the pragma is guarded;
    a ``connect`` that fails is a real failure and still propagates.

    WAL is NOT set here (BR-3A1-5). ``busy_timeout`` is per-connection, but WAL is a
    property of the shared database file and would change the journal mode for every
    other CAO subsystem using it (ADR-583-10 defers it to ``nfr-design``).

    This factory runs NO migrators, so the migrator-memoisation half of ADR-583-10
    does not transfer (BR-3A1-6): there is nothing to memoise and no per-call DDL to
    remove.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import (
        DATABASE_FILE,
        WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS,
    )

    conn = sqlite3.connect(str(DATABASE_FILE))
    try:
        conn.execute(f"PRAGMA busy_timeout = {WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS}")
    except sqlite3.Error as exc:
        # Degrade, never deny (SR-3A1-3). The message names the failure and the
        # intended value only — never the database path or arbitrary context
        # (SR-3A1-6).
        logger.warning(
            "could not set busy_timeout=%d on the workflow spec-index connection: %s",
            WORKFLOW_SPEC_INDEX_BUSY_TIMEOUT_MS,
            exc,
        )
    return conn


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


def _validate_write_target(name: str, scan_dir: Optional[str]) -> tuple[str, str]:
    """Validate a write target and return ``(safe_base, target_path)``.

    Shared preconditions of ``create_workflow`` and ``update_workflow``, in the order
    the design fixes (issue #583, Bolt 3, unit 3). NOTHING here touches the
    filesystem for writing, so every rejection below leaves it byte-identical.

    ``name`` is a BARE name, never a path (TS-3A3-2): it is what
    ``WORKFLOW_NAME_RE`` constrains, what the index is keyed on, and what the
    tier-collision check needs. The ``.py`` extension is supplied HERE and never by
    the caller, which is what makes the Python-only rule enforceable rather than
    advisory.

    Raises:
        ValueError: the name is invalid, or names a tier this service cannot write.
    """
    # A name carrying an extension is a caller asking for a tier. Answer that
    # question explicitly rather than letting _validate_name reject the dot as a
    # charset error (BR-3A3-2): an agent that reasonably tried YAML should learn the
    # rule, not debug a message that reads like a typo.
    lowered = name.lower()
    if lowered.endswith((".yaml", ".yml")):
        raise ValueError(
            f"workflow '{name}': YAML specs cannot be created or updated through this "
            "service (issue #583 scope is Python workflows only). YAML specs remain "
            "readable, listable and deletable."
        )
    if lowered.endswith(".py"):
        raise ValueError(
            f"workflow '{name}': pass a bare workflow name without the '.py' extension"
        )

    _validate_name(name)  # BR-3A3-1 — never reimplemented
    safe_base = _safe_dir(scan_dir)
    return safe_base, os.path.join(safe_base, f"{name}.py")


def _validated_script_spec(name: str, source: str, target_path: str) -> ScriptSpec:
    """Lint + parse ``source`` into a ``ScriptSpec``, refusing anything unrunnable.

    The write path's validation gate (BR-3A3-5, SR-3A3-3). Reuses the read path's own
    tools verbatim (TS-3A3-5) so the write path agrees with the RUN path about what is
    runnable: a spec whose lint ``status == "fail"`` is **loadable but unrunnable**
    (the load-time lint is informational, ``script_runner`` is fail-closed), and Bolt 1
    made ``missing-recovery-policy`` an ERROR — so a ``step()`` without ``recovery=``
    lands in exactly that state. CAO must not write a spec it would refuse to run.

    Unlike ``_read_script_spec``, this does NOT skip ``_extract_inputs`` on a syntax
    finding: that graceful degradation exists so a broken file on disk still loads with
    its finding intact, and it is unreachable here because a syntax error is an ERROR
    finding that the gate refuses first (TS-3A3-5).

    Raises:
        ValueError: a lint ERROR, or a malformed ``INPUTS`` literal. Lint findings
            travel on the message rather than in a new exception type (TS-3A3-6).
    """
    result = lint_script(source, target_path)
    if result.status == "fail":
        errors = "; ".join(
            f"{f.rule_id} (line {f.line})" for f in result.findings if f.severity == "error"
        )
        raise ValueError(f"workflow '{name}' has lint errors and would not be runnable: {errors}")
    inputs = _extract_inputs(source)  # AST-only; never executes the module (SR-3A3-8)
    return ScriptSpec(
        name=name,
        path=target_path,
        source=source,
        content_hash=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        findings=result.findings,
        inputs=inputs,
    )


def _persist_spec(spec: ScriptSpec, target_path: str, safe_base: str) -> ScriptSpec:
    """Write ``spec.source`` through the guarded writer, then index it.

    The bytes written are the bytes validated (BR-3A3-8, SR-3A3-7) — the same in-memory
    text, encoded once, never re-read. That is what makes ``spec.content_hash`` describe
    exactly what landed, which is the contract unit 4's stale-update check depends on.

    A failed index upsert is logged and the operation still SUCCEEDS (BR-3A3-12,
    SR-3A3-9): the file is canonical and ``workflow_index`` is a derived, droppable
    projection rebuilt from the files on every ``list_workflows``, so the failure is
    self-healing. Raising would report failure for an operation that succeeded, and
    deleting the just-written file to force atomicity would destroy the user's content
    because a cache write failed.
    """
    real_path = _write_contained_spec_bytes(
        target_path, spec.source.encode("utf-8"), base_dir=safe_base
    )
    written = spec.model_copy(update={"path": real_path})
    try:
        # real_path is passed through UNMODIFIED — upsert_index requires the resolved
        # realpath with no re-derivation (:344-349, BR-3A3-11).
        upsert_index(written, real_path)
    except Exception as exc:
        logger.warning(
            "workflow '%s' was written but its index row could not be updated (%s); "
            "the next list/get will rebuild it from disk",
            spec.name,
            exc,
        )
    return written


def create_workflow(name: str, source: str, scan_dir: Optional[str] = None) -> ScriptSpec:
    """Create a NEW Python workflow spec, refusing to overwrite an existing one (FR-10).

    Separate from :func:`update_workflow` rather than a mode flag because the two have
    genuinely different preconditions (TS-3A3-1): an agent that means "make a new
    workflow" must not silently clobber one.

    Order is load-bearing — every check precedes any write, so a refusal leaves the
    filesystem byte-identical (SR-3A3-2):

    1. name + tier (``_validate_write_target``);
    2. the target must NOT already exist;
    3. no cross-tier collision — ``_check_tier_collision`` runs at ACCESS time and at
       scan time but NEVER at write time, so without this a ``foo.py`` created beside an
       existing ``foo.yaml`` succeeds and then every ``get_workflow("foo")`` raises
       ``TierCollisionError``: a file unreachable the moment it lands (BR-3A3-4,
       SR-3A3-4);
    4. lint + ``INPUTS`` validation;
    5. write, then index.

    The existence check in step 2 is a ``stat`` and is therefore racy — a concurrent
    writer could create the file between the check and the write. ``O_EXCL`` would make
    it atomic and was deliberately declined at unit 2 to keep that finished primitive's
    surface closed. This narrows a LIKELY mistake to a RARE one; it does not eliminate
    it. Lost updates are a separate problem, mitigated by the caller's
    expected-source-hash check, not here.

    Args:
        name: bare workflow name, no extension and no path separators.
        source: the spec's Python source, written verbatim.
        scan_dir: target directory. Internal/test use only — the pass 3B CLI and MCP
            surfaces MUST NOT expose this to an agent (SR-3A3-5).

    Returns:
        The parsed ``ScriptSpec``, carrying any WARNING-level findings and the
        ``content_hash`` of exactly what was written.

    Raises:
        ValueError: invalid name, unwritable tier, lint errors, or malformed ``INPUTS``.
        FileExistsError: a spec with that name already exists.
        TierCollisionError: a same-stem sibling exists in the other tier.
    """
    safe_base, target_path = _validate_write_target(name, scan_dir)
    if os.path.exists(target_path):
        raise FileExistsError(f"workflow '{name}' already exists; use update to change it")
    _check_tier_collision(name, safe_base)  # -> TierCollisionError (409)
    spec = _validated_script_spec(name, source, target_path)
    return _persist_spec(spec, target_path, safe_base)


def update_workflow(name: str, source: str, scan_dir: Optional[str] = None) -> ScriptSpec:
    """Update an EXISTING Python workflow spec, refusing to create one (FR-10).

    The mirror of :func:`create_workflow`: same ordered preconditions, opposite
    existence requirement, so an agent that means "edit this workflow" cannot silently
    create one. See that function for why the existence check is racy and why that is
    accepted.

    Args:
        name: bare workflow name, no extension and no path separators.
        source: the spec's new Python source, written verbatim.
        scan_dir: target directory. Internal/test use only (SR-3A3-5).

    Returns:
        The parsed ``ScriptSpec`` for the new content.

    Raises:
        ValueError: invalid name, unwritable tier, lint errors, or malformed ``INPUTS``.
        FileNotFoundError: no spec with that name exists.
        TierCollisionError: a same-stem sibling exists in the other tier.
    """
    safe_base, target_path = _validate_write_target(name, scan_dir)
    if not os.path.exists(target_path):
        raise FileNotFoundError(f"workflow '{name}' does not exist; use create to add it")
    _check_tier_collision(name, safe_base)
    spec = _validated_script_spec(name, source, target_path)
    return _persist_spec(spec, target_path, safe_base)


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
