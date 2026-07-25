"""The per-run execution-mode manifest the orchestration lane consumes.

This is the fork's answer to one question, asked about a whole run rather
than a single reservation: *which generations in this run are native-TUI
and which are ACP, and what proves it?*

It exists because the admission and recovery policy that consumes these
facts lives in a different repository.  That consumer cannot be asked to
re-derive a mode by joining tables it does not own, and it must never
infer one from a provider name or from the absence of a field — the
inference "no mode recorded, so probably native" is exactly how a
historical ACP generation would get treated as an attachable native
session.  So the fork publishes the mode of record, the precedence level
that produced it, and whether the row predates the contract at all.

Two properties make this consumable as a contract rather than as a
convenience:

* **Every entry carries a concrete mode.**  A row written before the
  execution-mode contract existed projects as ACP with source
  ``legacy``, never as ``None``.  A consumer never has to decide what a
  null means.
* **The digest is stable and content-only.**  ``manifest_digest`` covers
  the entries and the run identity, and deliberately excludes the
  generation timestamp, so the same durable state digests identically on
  every call.  A digest that changed once per second could not be used
  to pin anything.

The manifest is a projection, never a source of truth: it reads
committed rows and computes nothing that is not already durable.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import native_attachment

MANIFEST_SCHEMA = "cao-run-execution-manifest-v1"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _parse_json(raw: Optional[str]) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None


def _attachment_projection(provider: str, native_session_id: Optional[str]) -> Optional[dict]:
    """The attachment ownership record for a bound native session, if any.

    Returns ``None`` both when the generation has no native session yet
    and when nothing holds it.  Those are different situations, and the
    entry's own ``native_session_id`` distinguishes them — this field
    answers only "who holds it", so an absent attachment reads as absent
    rather than as an owner with empty fields.
    """
    if not native_session_id:
        return None
    held = native_attachment.get(provider, native_session_id)
    if held is None:
        return None
    return {
        "state": held["state"],
        "owner_terminal_id": held["owner"]["terminal_id"],
        "owner_generation": held["owner"]["generation"],
        "owner_execution_mode": held["owner"]["execution_mode"],
        "ambiguity_reason": held["ambiguity_reason"],
        "epoch": held["epoch"],
    }


def _entry(row: Any) -> dict[str, Any]:
    mode_record = {
        "execution_mode": getattr(row, "execution_mode", None),
        "execution_mode_source": getattr(row, "execution_mode_source", None),
    }
    binding = _parse_json(row.binding_json)
    native_session_id = binding.get("native_session_id") if isinstance(binding, dict) else None
    mode = em.mode_of_record(mode_record)
    entry: dict[str, Any] = {
        "reservation_id": row.reservation_id,
        "terminal_id": row.terminal_id,
        "generation": row.generation,
        "provider": row.provider,
        "agent_profile": row.agent_profile,
        "state": row.state,
        "execution_mode": mode,
        "execution_mode_source": em.source_of_record(mode_record),
        "is_legacy_execution_mode": em.is_legacy_row(mode_record),
        "native_session_id": native_session_id,
        # The mode recorded on the binding receipt, kept separate from the
        # row's mode on purpose: if these ever disagree, that disagreement
        # is the fact worth seeing, and collapsing them into one field
        # would hide it.
        "binding_execution_mode": (
            binding.get("execution_mode") if isinstance(binding, dict) else None
        ),
        "attachment": _attachment_projection(row.provider, native_session_id),
    }
    return entry


def build(run_id: str) -> dict[str, Any]:
    """Project every v2 generation in ``run_id`` with its mode of record."""
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    with database.SessionLocal() as db:
        rows = (
            db.query(database.ManagedLaunchV2ReservationModel)
            .filter(database.ManagedLaunchV2ReservationModel.run_id == run_id)
            # Ordered by the immutable reservation id rather than by a
            # timestamp: two reservations created in the same clock tick
            # would otherwise order arbitrarily and change the digest.
            .order_by(database.ManagedLaunchV2ReservationModel.reservation_id)
            .all()
        )
        entries = [_entry(row) for row in rows]
    counts: dict[str, int] = {mode: 0 for mode in sorted(em.EXECUTION_MODES)}
    for entry in entries:
        counts[entry["execution_mode"]] += 1
    return {
        "schema": MANIFEST_SCHEMA,
        "run_id": run_id,
        "entries": entries,
        "counts_by_execution_mode": counts,
        "legacy_entry_count": sum(1 for e in entries if e["is_legacy_execution_mode"]),
        "generated_at": _now(),
    }


def manifest_digest(manifest: dict[str, Any]) -> str:
    """A stable digest over the manifest's content, excluding its clock.

    ``generated_at`` is excluded so the digest identifies *durable state*
    rather than the moment it was read.  A consumer can therefore pin
    this value and compare it across calls to detect real change.
    """
    content = {key: value for key, value in manifest.items() if key != "generated_at"}
    return hashlib.sha256(_canonical(content).encode()).hexdigest()
