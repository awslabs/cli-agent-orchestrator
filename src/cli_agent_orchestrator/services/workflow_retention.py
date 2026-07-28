"""Workflow-journal retention, redaction & deletion policy (issue #504, U7).

The security/retention posture for the durable workflow journal — realizing the
six binding Q3 security rules (NFR-SEC-1..6) and backing the FR-11 per-run delete:

- **Metadata-only default (NFR-SEC-1/2).** Output capture is OFF by default. With
  capture off, only always-on execution metadata (events, timings, states,
  structured error kinds, terminal + artifact references) is journaled — never
  prompt text or full step output. The emission path (``workflow_service``) already
  routes NO prompt/free-text output into the journal, so this posture holds by
  construction; this module owns the gate a future capture-enabling feature calls.
- **Bounded, sanitized capture (NFR-SEC-4/6).** When capture is explicitly enabled,
  retained free-text funnels through ``sanitize_output`` — which REUSES the
  ``audit_log`` cap-and-mark idiom (``audit_log._sanitize_for_log`` + the
  ``_sanitize_field_value``-style byte-cap + the ``"[…truncated]"`` marker). There is
  NO second/parallel redaction policy (NFR-SEC-6, the deciding rule): the audit_log
  choke point is the single redaction path.
- **Age + run-count retention (NFR-SEC-3).** ``sweep_runs`` prunes runs older than an
  age default AND beyond a most-recent run-count default (whichever bound is hit
  first prunes); both are settings-overridable. Each pruned run is removed via U1's
  ``workflow_journal.delete_run`` cascade — this module does NOT reimplement it.

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
    """Return the retention age bound in days (default 30, BR-1), configurable."""
    try:
        v = int(get_memory_settings().get(RETENTION_DAYS_KEY, RETENTION_DAYS_DEFAULT))
        return v if v >= 0 else RETENTION_DAYS_DEFAULT
    except Exception:  # noqa: BLE001
        return RETENTION_DAYS_DEFAULT


def retention_count() -> int:
    """Return the retention run-count bound (default 100, BR-1), configurable."""
    try:
        v = int(get_memory_settings().get(RETENTION_COUNT_KEY, RETENTION_COUNT_DEFAULT))
        return v if v >= 0 else RETENTION_COUNT_DEFAULT
    except Exception:  # noqa: BLE001
        return RETENTION_COUNT_DEFAULT


# -----------------------------------------------------------------------------
# Step 2 — capture gating + sanitize (NFR-SEC-1/2/4/6)
# -----------------------------------------------------------------------------


def sanitize_output(text: str) -> str:
    """Redact + size-limit retained free-text through the audit_log idiom (NFR-SEC-6).

    The SINGLE redaction path. Mirrors ``audit_log._sanitize_field_value`` exactly,
    at U7's own ``output_cap_bytes()`` cap:

    1. Base clean via ``audit_log._sanitize_for_log`` — strips ANSI / C0 controls,
       escapes newlines, drops Unicode line separators (the shared choke point).
       Referenced as a module attribute so a spy/patch on
       ``audit_log._sanitize_for_log`` proves the funnel (BR-SEC-6).
    2. Byte-cap + the SAME ``"[…truncated]"`` marker on the encoded result.

    There is NO parallel truncation/redaction implementation — a second policy would
    fail NFR-SEC-6 (and the BR-SEC-6 test, which spies this funnel).
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


def resolve_captured_output(text: Optional[str]) -> Optional[str]:
    """Capture gate (business-logic-model Algorithm 1) — the U7-owned attachment point.

    The single decision an emission/write site consults for whether a free-text
    output is retained:

    - capture OFF (default) or ``text is None`` -> ``None``: metadata-only, NO
      prompt/output text is retained (NFR-SEC-1/2).
    - capture ON -> the sanitized, size-limited text (``sanitize_output``, NFR-SEC-4/6).

    This module owns the gate so the drive-loop emission sequence (SEAM #1) is never
    restructured to add capture logic. The current default-off posture already retains
    no free-text (the emission path journals only metadata + a NULL ``output_ref``),
    so no emission-site call is required today; this helper is the designated,
    fully-tested extension point a future capture-enabling feature calls.
    """
    if text is None or not capture_enabled():
        return None
    return sanitize_output(text)


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
    """
    days = retention_days if retention_days is not None else _default_retention_days()
    count = retention_count if retention_count is not None else _default_retention_count()

    try:
        rows = workflow_journal.list_run_ids_by_age()
    except Exception as e:  # noqa: BLE001 — a maintenance sweep must not raise on a read failure
        logger.warning("workflow retention sweep: run enumeration failed (skipped): %s", e)
        return 0

    to_prune: set[str] = set()
    # Age bound: started_at strictly before the cutoff.
    cutoff = _age_cutoff(days)
    for run_id, started_at in rows:
        if started_at and started_at < cutoff:
            to_prune.add(run_id)
    # Count bound: everything beyond the most-recent ``count`` runs (rows are
    # most-recent-first, so index >= count is "beyond the window").
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
