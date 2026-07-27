"""Durable wake-receipt sidecars for unmanaged idle receivers.

One JSON record per ``(terminal_id, message_id)``, written by the
:class:`~cli_agent_orchestrator.services.inbox_service.InboxService` wake
watcher after an unmanaged paste.  An unmanaged receiver has no
provider-native model-turn acknowledgement (its pane runs a TUI, not a
bridge), so a paste that the provider never started a turn from reads as
``delivered`` forever under the inbox's pre-send marking.  This sidecar is
the truthful replacement for that false close: a *wake* receipt that records
whether the receiver's status transitioned out of IDLE after the paste, not
whether the model consumed the bytes.

It is honest about what it is not.  A status transition is the strongest
unmanaged evidence available, but it is not a provider-native
``terminal_queued -> submitted`` acknowledgement; the conductor keeps this
receipt in the ``submitted_receipt`` slot with ``source: status-transition``,
and no document calls it provider-native.  cond-0072's provider-native
acknowledgement surface remains open.

The record is the truth and the idempotency boundary.  ``ensure_watching``
is a no-op once any record exists for the key, so the four delivery sites
(post, event loop, poller, reconcile) funnelling through one
``deliver_pending`` produce exactly one watcher and at most one nudge across
them, across restarts, and across the reconcile sweep.  A restart loses only
the in-memory task registry; the durable record decides whether to re-arm
(observation only, never a second nudge once ``nudge_intent_at`` exists) or
to finalize ``wake_unconfirmed`` for a record whose deadline passed while the
nudge decision did not survive.

Mirrors :mod:`companion_receipts` for atomic write + fsync and exclusive
flock, but keyed by message id rather than generation and carrying the wake
state machine rather than provider-native acks.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from cli_agent_orchestrator.constants import WAKE_RECEIPT_DIR

logger = logging.getLogger(__name__)

#: One record per message, versioned so a reader can refuse a shape it does
#: not know rather than guessing at missing fields.
SCHEMA = "cao-unmanaged-wake-receipt-v1"
_SCHEMA_VERSION = 1

WATCHING = "watching"
WAKE_CONFIRMED = "wake_confirmed"
WAKE_UNCONFIRMED = "wake_unconfirmed"
STATES = frozenset({WATCHING, WAKE_CONFIRMED, WAKE_UNCONFIRMED})

#: Provenance: this is status-transition evidence, never provider-native.
SOURCE = "status-transition"


def utcnow() -> str:
    """An ISO-8601 UTC timestamp.  Passed in by callers in tests; produced
    here in production so the module never depends on a monkeypatched clock
    the caller forgot to set."""
    return datetime.now(timezone.utc).isoformat()


def parse_iso_timestamp(iso: Optional[str]) -> Optional[float]:
    """A POSIX timestamp for ``iso``, or None when it is absent or unparseable."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def deadline_iso(start_iso: str, seconds: float) -> str:
    """An ISO deadline ``seconds`` after ``start_iso`` (used by the watcher)."""
    ts = parse_iso_timestamp(start_iso)
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts + seconds, timezone.utc).isoformat()


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(value))


def _record_path(terminal_id: str, message_id: str) -> Path:
    return WAKE_RECEIPT_DIR / f"{_safe(terminal_id)}-{_safe(message_id)}.json"


def _default_record() -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "schema_version": _SCHEMA_VERSION,
        "message_id": None,
        "terminal_id": None,
        "native_session_id": None,
        "delivery_identity": None,
        "baseline_status": None,
        "delivered_at": None,
        "state": WATCHING,
        "deadline_at": None,
        "nudge_intent_at": None,
        "nudge_sent_at": None,
        "observed": None,
        "note": None,
        "source": SOURCE,
    }


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        record = _default_record()
        return record
    if not isinstance(data, dict):
        data = {}
    # Merge onto defaults so a record written by an older/newer writer still
    # exposes every field a reader asks for; a missing field is its default,
    # never a KeyError that would surface as an unhandled error.
    record = _default_record()
    record.update({k: v for k, v in data.items() if k in record})
    return record


def _atomic_write(path: Path, record: dict[str, Any]) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".wake-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _mutate(terminal_id: str, message_id: str, mutator) -> dict[str, Any]:
    """Read-modify-write one message's record under an exclusive flock."""
    path = _record_path(terminal_id, message_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with open(lock_path, "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            record = _load(path)
            record["terminal_id"] = terminal_id
            record["message_id"] = message_id
            mutator(record)
            _atomic_write(path, record)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return record


# --- producers (InboxService wake watcher only) ---------------------------


def ensure_watching(
    terminal_id: str,
    message_id: str,
    *,
    native_session_id: Optional[str],
    delivered_at: str,
    deadline_at: str,
    delivery_identity: Optional[dict[str, Any]] = None,
    baseline_status: Optional[str] = None,
) -> dict[str, Any]:
    """Idempotently open a ``watching`` record for this message.

    Does nothing — and returns the existing record — when any record already
    exists for the key, so the one delivery that reaches a pane produces one
    watcher and one nudge across every delivery site and every re-arrival.
    A v1 terminal exposes no native session: recorded as explicit ``None``,
    never invented.
    """

    def mutate(record: dict[str, Any]) -> None:
        # First writer only.  A later arrival for the same key is a no-op:
        # the watcher it would arm already exists (or already finalized), and
        # starting a second would duplicate the nudge this seam exists to bound.
        if path.exists():
            return
        record.update(
            {
                "native_session_id": native_session_id,
                "delivery_identity": delivery_identity,
                "baseline_status": baseline_status,
                "delivered_at": delivered_at,
                "state": WATCHING,
                "deadline_at": deadline_at,
            }
        )

    path = _record_path(terminal_id, message_id)
    return _mutate(terminal_id, message_id, mutate)


def record_nudge_intent(terminal_id: str, message_id: str, *, at: str) -> dict[str, Any]:
    """Durably record the intent to nudge BEFORE the Enter is sent.

    A crash after this point but before :func:`record_nudge_sent` is recovered
    at startup as ``wake_unconfirmed`` with the nudge never re-sent, mirroring
    the steer/control-input intent-before-effect rule.
    """

    def mutate(record: dict[str, Any]) -> None:
        # Idempotent: re-arrival with the intent already recorded is a no-op.
        if record.get("nudge_intent_at") is None:
            record["nudge_intent_at"] = at

    return _mutate(terminal_id, message_id, mutate)


def record_nudge_sent(terminal_id: str, message_id: str, *, at: str) -> dict[str, Any]:
    def mutate(record: dict[str, Any]) -> None:
        if record.get("nudge_sent_at") is None:
            record["nudge_sent_at"] = at

    return _mutate(terminal_id, message_id, mutate)


def record_wake_confirmed(
    terminal_id: str,
    message_id: str,
    *,
    observed: dict[str, Any],
    note: Optional[str] = None,
) -> dict[str, Any]:
    """The receiver transitioned out of IDLE: the wake is confirmed."""

    def mutate(record: dict[str, Any]) -> None:
        # Terminal once reached: a later transition does not reopen it.
        if record.get("state") == WAKE_CONFIRMED:
            return
        record["state"] = WAKE_CONFIRMED
        record["observed"] = observed
        if note is not None:
            record["note"] = note

    return _mutate(terminal_id, message_id, mutate)


def record_wake_unconfirmed(
    terminal_id: str,
    message_id: str,
    *,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """The deadline elapsed without a wake transition."""

    def mutate(record: dict[str, Any]) -> None:
        if record.get("state") == WAKE_CONFIRMED:
            # A transition that landed outranks a concurrent deadline expiry.
            return
        record["state"] = WAKE_UNCONFIRMED
        if note is not None:
            record["note"] = note

    return _mutate(terminal_id, message_id, mutate)


# --- readers --------------------------------------------------------------


def get(terminal_id: str, message_id: str) -> Optional[dict[str, Any]]:
    """The record for this exact message, or None.

    Bound to the exact message id; a corrupt or unreadable record fails closed
    to None (no observation) rather than an invented one.
    """
    path = _record_path(terminal_id, message_id)
    if not path.exists():
        return None
    record = _load(path)
    record["terminal_id"] = terminal_id
    record["message_id"] = message_id
    return record


def iter_records() -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Every stored record, as ``(terminal_id, message_id, record)``.

    Used by startup load to re-arm or finalize watchers without a second
    nudge.  Filenames are sanitised derivations, so the original ids are read
    back out of the record body rather than parsed off the filename.
    """
    for path in sorted(WAKE_RECEIPT_DIR.glob("*.json")):
        record = _load(path)
        terminal_id = record.get("terminal_id")
        message_id = record.get("message_id")
        if terminal_id and message_id:
            yield terminal_id, message_id, record
