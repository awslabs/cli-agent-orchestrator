"""Workflow-journal retention, redaction & deletion policy (issue #504, U7).

The security/retention posture for the durable workflow journal — realizing the
six binding Q3 security rules (NFR-SEC-1..6) and backing the FR-11 per-run delete:

- **Metadata-only default (NFR-SEC-1/2).** Output capture is OFF by default. With
  capture off, only always-on execution metadata (events, timings, states,
  structured error kinds, terminal + artifact references) is journaled — never
  prompt text or full step output. The event log holds no step output text at ALL:
  the emission path stores only a content-digest REFERENCE
  (``output_reference``) in the event's ``output_ref`` column, regardless of the
  capture setting. Captured output text reaches an operator solely through the
  diagnostic bundle's ``excerpts`` section, which re-reads the capture gate at
  export time — so turning capture off actually stops payloads from shipping.
- **Bounded, sanitized capture (NFR-SEC-4/6).** When capture is explicitly enabled,
  retained free-text funnels through ``sanitize_output`` — which REUSES the
  ``audit_log`` cap-and-mark idiom (``audit_log._sanitize_for_log`` + the
  ``_sanitize_field_value``-style byte-cap + the ``"[…truncated]"`` marker). There is
  NO second/parallel sanitization policy (NFR-SEC-6, the deciding rule): the
  audit_log choke point is the single path.

  ``sanitize_output`` is size-limiting + control-character hygiene, NOT secret
  redaction — it does not detect or remove credentials, and a token in the text
  passes through verbatim. Surfaces that expose it are protected by a scope gate
  instead; do not describe its output as "redacted".
- **Age + run-count retention (NFR-SEC-3).** ``sweep_runs`` prunes runs older than an
  age default AND beyond a most-recent run-count default (whichever bound is hit
  first prunes); both are settings-overridable. Each pruned run is removed via U1's
  ``workflow_journal.delete_run`` cascade — this module does NOT reimplement it.
  Scheduled from the API lifespan at startup
  (``api.main._sweep_workflow_runs_at_startup``); without that call site the sweep
  is dead code and the journal grows unbounded.

Config provenance (BR-1/BR-2):

- ``RETENTION_DAYS_DEFAULT = 30`` is grounded in
  ``audit_log.sweep_old_audit_logs(retention_days=30)``.
- ``RETENTION_COUNT_DEFAULT = 100`` has NO existing precedent — a reasonable
  starting default for a local developer tool, stated as such, configurable.
- ``OUTPUT_CAP_BYTES = 8192`` (8 KiB) diverges above audit_log's 4 KiB
  ``PER_FIELD_CAP_BYTES`` because a worker step's output is materially larger than a
  single audit field; stated here rather than diverging silently, configurable
  (mirroring the ``audit_log_day_cap_bytes`` override pattern).

Settings are read through ``settings_service.get_memory_settings().get(<key>,
<default>)`` and written through ``settings_service.set_memory_setting(key, value)``
(the four keys are wired additively into that function's allow-list).
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from cli_agent_orchestrator.services import audit_log, workflow_journal
from cli_agent_orchestrator.services.settings_service import get_memory_settings

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Constants & setting keys (Step 1 — provenance stated inline above)
# -----------------------------------------------------------------------------

# Setting keys (namespaced under the "memory" settings block, like the audit_log
# ``audit_log_day_cap_bytes`` precedent). Read with an explicit default so an
# unset key transparently falls back to the constant below.
CAPTURE_OUTPUT_KEY = "workflow_journal_capture_output"
OUTPUT_CAP_BYTES_KEY = "workflow_journal_output_cap_bytes"
RETENTION_DAYS_KEY = "workflow_journal_retention_days"
RETENTION_COUNT_KEY = "workflow_journal_retention_count"

# Defaults (BR-1/BR-2, provenance in the module docstring).
CAPTURE_OUTPUT_DEFAULT = False  # NFR-SEC-2: no prompt/output retention unless opted in
OUTPUT_CAP_BYTES = 8 * 1024  # 8 KiB — above audit_log's 4 KiB PER_FIELD_CAP_BYTES (BR-2)
RETENTION_DAYS_DEFAULT = 30  # grounded in audit_log.sweep_old_audit_logs (BR-1)
RETENTION_COUNT_DEFAULT = 100  # no precedent — a reasonable local-tool default (BR-1)

# The truncation marker. MUST stay byte-identical to audit_log.py's
# ``_sanitize_field_value`` marker (audit_log.py:158) — U7 reuses the ONE
# cap-and-mark idiom (NFR-SEC-6); it does not introduce a second marker.
_TRUNCATION_MARKER = "[…truncated]"


# -----------------------------------------------------------------------------
# Settings adapters — read-only; never raise out (fail to the safe default)
# -----------------------------------------------------------------------------


def capture_enabled() -> bool:
    """Return whether opt-in output capture is enabled (default False, NFR-SEC-2).

    Fail-closed: any unreadable setting falls back to ``CAPTURE_OUTPUT_DEFAULT``
    (no capture) so a misconfiguration never silently starts retaining free-text.
    """
    try:
        return bool(get_memory_settings().get(CAPTURE_OUTPUT_KEY, CAPTURE_OUTPUT_DEFAULT))
    except Exception:  # noqa: BLE001 — a settings read must never fault the caller
        return CAPTURE_OUTPUT_DEFAULT


def output_cap_bytes() -> int:
    """Return the per-output byte cap (default 8 KiB, NFR-SEC-4), configurable.

    A non-positive or unreadable value degrades to ``OUTPUT_CAP_BYTES`` — a zero
    cap would truncate everything to just the marker, which is never intended.
    """
    try:
        v = int(get_memory_settings().get(OUTPUT_CAP_BYTES_KEY, OUTPUT_CAP_BYTES))
        return v if v > 0 else OUTPUT_CAP_BYTES
    except Exception:  # noqa: BLE001
        return OUTPUT_CAP_BYTES


def retention_days() -> int:
    """Return the retention age bound in days (default 30, BR-1), configurable.

    ``0`` means the age bound is DISABLED (unlimited), not "expire everything" —
    see ``sweep_runs``. A negative persisted value falls back to the default.
    """
    try:
        v = int(get_memory_settings().get(RETENTION_DAYS_KEY, RETENTION_DAYS_DEFAULT))
        return v if v >= 0 else RETENTION_DAYS_DEFAULT
    except Exception:  # noqa: BLE001
        return RETENTION_DAYS_DEFAULT


def retention_count() -> int:
    """Return the retention run-count bound (default 100, BR-1), configurable.

    ``0`` means the count bound is DISABLED (unlimited), not "keep zero runs" —
    see ``sweep_runs``. A negative persisted value falls back to the default.
    """
    try:
        v = int(get_memory_settings().get(RETENTION_COUNT_KEY, RETENTION_COUNT_DEFAULT))
        return v if v >= 0 else RETENTION_COUNT_DEFAULT
    except Exception:  # noqa: BLE001
        return RETENTION_COUNT_DEFAULT


# -----------------------------------------------------------------------------
# Step 2 — capture gating + sanitize (NFR-SEC-1/2/4/6)
# -----------------------------------------------------------------------------


def sanitize_output(text: str) -> str:
    """Size-limit + neutralize retained free-text through the audit_log idiom.

    The SINGLE sanitization path. Mirrors ``audit_log._sanitize_field_value``
    exactly, at U7's own ``output_cap_bytes()`` cap:

    1. Base clean via ``audit_log._sanitize_for_log`` — strips ANSI / C0 controls,
       escapes newlines, drops Unicode line separators (the shared choke point).
       Referenced as a module attribute so a spy/patch on
       ``audit_log._sanitize_for_log`` proves the funnel (BR-SEC-6).
    2. Byte-cap + the SAME ``"[…truncated]"`` marker on the encoded result.

    There is NO parallel truncation implementation — a second policy would
    fail NFR-SEC-6 (and the BR-SEC-6 test, which spies this funnel).

    NOT SECRET REDACTION. This is transport/log hygiene: it makes text safe to
    write on one line and bounds its size. It does NOT detect or remove
    credentials — a token or API key in the input passes through verbatim (only
    escaped and possibly truncated). Callers must not describe its output as
    "redacted"; treat anything funnelled through it as still-sensitive and gate
    the surface that exposes it on a scope instead.
    """
    cap = output_cap_bytes()
    # (1) Base cap-and-mark clean — the audit_log choke point (attribute access so a
    #     monkeypatch on audit_log._sanitize_for_log is observed here, BR-SEC-6).
    cleaned = audit_log._sanitize_for_log(str(text), max_len=cap)
    # (2) Byte-cap + marker, identical in structure to _sanitize_field_value (L157-158).
    encoded = cleaned.encode("utf-8")
    if len(encoded) > cap:
        cleaned = encoded[:cap].decode("utf-8", errors="ignore") + _TRUNCATION_MARKER
    return cleaned


# NOTE (PR #526 review round 3): ``resolve_captured_output`` was REMOVED here.
# It was described as "the U7-owned attachment point" for the capture gate, but it
# never acquired a production caller — every reference was in its own tests. Its
# docstring therefore over-claimed: it read as though it gated the write path, while
# the code that actually decides what is retained is ``capture_enabled()`` consulted
# at the diagnostics bundle, plus ``sanitize_output`` at the excerpt boundary.
# Keeping a dead gate that claims to be THE gate is worse than having none: a future
# author wires a payload through it and believes the payload is capture-gated. If a
# write-path gate is wanted, add it at the call site with a test that proves it fires.


def output_reference(text: Optional[str]) -> Optional[str]:
    """A stable, non-revealing REFERENCE to a step output, for ``output_ref``.

    ``output_ref`` feeds the reference-level surfaces — compare's per-step
    ``a_refs``/``b_refs`` diff and ``BundleReferences.artifacts`` — whose
    contract is "references, not payloads". Storing the captured text there
    exported full payloads through fields documented as references, and did so
    even when the bundle's own ``capture_enabled`` flag read false (the flag is
    re-read at export time, so a payload written while capture was ON still
    shipped after it was turned OFF).

    So the column gets a content digest instead: ``sha256:<16 hex>``. It is
    stable (equal outputs produce equal refs, so the compare diff still detects
    "this step produced something different"), reveals nothing about the
    content, and is a fixed 23 characters regardless of output size. ``None``
    for ``None`` — an absent output has no reference.

    Deliberately NOT capture-gated: a digest is not free-text retention, so the
    reference surfaces stay useful with capture off (which is the default).
    """
    if text is None:
        return None
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest[:16]}"


# -----------------------------------------------------------------------------
# Step 3 — retention sweep (NFR-SEC-3): age + run-count, whichever hits first
# -----------------------------------------------------------------------------

# Aliases captured before ``sweep_runs`` shadows these names with its keyword
# parameters (the plan pins the signature ``sweep_runs(*, retention_days=None,
# retention_count=None)``). ``sweep_runs`` resolves its ``None`` defaults through
# these aliases so the setting-backed defaults still apply.
_default_retention_days = retention_days
_default_retention_count = retention_count


def _age_cutoff(days: int) -> str:
    """The ISO-8601 Z cutoff string; a run whose ``started_at`` sorts BEFORE it is
    older than ``days`` (started_at is stored in the same ``%Y-%m-%dT%H:%M:%SZ``
    format, so a lexicographic compare matches a chronological one)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def sweep_runs(
    *, retention_days: Optional[int] = None, retention_count: Optional[int] = None
) -> int:
    """Prune runs beyond the age AND run-count bounds; return the count pruned (NFR-SEC-3).

    A run is pruned if it is older than ``retention_days`` OR beyond the most-recent
    ``retention_count`` runs — whichever bound is hit first triggers pruning (the
    union of the two prune sets). Both bounds default from settings when ``None``;
    passing an explicit value overrides (proving BR-SEC-3's "both configurable").

    Row-based (the journal is SQLite rows, not day-partitioned files), so this is its
    OWN capping — NOT ``audit_log.sweep_old_audit_logs`` (that sweeps day-files).
    Enumeration reuses ``workflow_journal.list_run_ids_by_age`` (run_id + started_at,
    most-recent first). Each pruned run_id is removed via U1's
    ``workflow_journal.delete_run`` cascade (run + step + event + seq rows) — U7 does
    NOT reimplement the cascade.

    Best-effort per run: a ``delete_run`` failure on one run is logged and the sweep
    continues (a maintenance sweep must not abort on a single bad row); only
    successful deletes are counted. Read/enumeration failures degrade to a no-op sweep
    (0) rather than raising.

    **``0`` DISABLES a bound — it does not mean "keep nothing"** (PR #526 review).
    Read naively, ``retention_days=0`` puts the age cutoff at *now*, so every run
    that ever started is older than it; and ``retention_count=0`` makes the count
    slice ``rows[0:]``, i.e. every row. Either therefore DELETED THE ENTIRE
    JOURNAL — and because this sweep runs automatically at startup, a persisted 0
    would have wiped every run on boot with no confirmation. 0 is a reachable
    value: the settings validator admits ``>= 0`` for both keys. Treating it as
    "this bound is off" is chosen over rejecting it, because a startup sweep must
    not crash the server on an already-persisted 0 and because "0 = unlimited" is
    the conventional reading of a retention bound. Both 0 -> the sweep is a no-op.
    To delete runs deliberately, use the explicit ``DELETE /workflows/runs/{id}``
    route; retention is a bound, not a purge tool.
    """
    days = retention_days if retention_days is not None else _default_retention_days()
    count = retention_count if retention_count is not None else _default_retention_count()

    try:
        rows = workflow_journal.list_run_ids_by_age()
    except Exception as e:  # noqa: BLE001 — a maintenance sweep must not raise on a read failure
        logger.warning("workflow retention sweep: run enumeration failed (skipped): %s", e)
        return 0

    to_prune: set[str] = set()
    # Age bound: started_at strictly before the cutoff. days == 0 disables the
    # bound (a cutoff of *now* would match every run ever started), so the age
    # pass is SKIPPED rather than run with a degenerate cutoff.
    if days > 0:
        cutoff = _age_cutoff(days)
        for run_id, started_at in rows:
            if started_at and started_at < cutoff:
                to_prune.add(run_id)
    # Count bound: everything beyond the most-recent ``count`` runs (rows are
    # most-recent-first, so index >= count is "beyond the window"). count == 0
    # disables the bound — the slice must never be allowed to become rows[0:],
    # which is every row.
    if count > 0:
        for run_id, _started in rows[count:]:
            to_prune.add(run_id)

    pruned = 0
    # Iterate the enumerated order for determinism; delete those in the prune set.
    for run_id, _started in rows:
        if run_id not in to_prune:
            continue
        try:
            workflow_journal.delete_run(run_id)
            pruned += 1
        except Exception as e:  # noqa: BLE001 — best-effort: log and continue the sweep
            logger.warning("workflow retention sweep: delete_run('%s') failed: %s", run_id, e)
    return pruned
