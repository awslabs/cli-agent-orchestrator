"""Conditional destructive endpoint (fork side of the destructive boundary).

Every destructive fork effect — terminal teardown, provider cleanup,
worktree-lease-adjacent removal — is issued only through this endpoint.
The conductor persists its own destructive intent under its journal lock
first; the fork then verifies *fork-owned facts only* under the
per-generation lock (lock class 4) and commits a final CAS before any
effect runs.

Invariant: the endpoint consumes the single-use intent id, verifies the
exact binding set (reservation/terminal/generation/attempt/fencing
token) against the fork's durable binding record, and refuses with zero
mutation when the generation's heartbeat reads ACTIVE or any identity
mismatches.  Effects whose safety depends on the containment
composition are refused while containment is unproven.

Failure mode prevented: without a single conditional endpoint, a stale
conductor decision (missing callback, superseded generation, replayed
intent) can delete an actively working generation — the false-death
deletion class this plane exists to eliminate.  A missing callback must
never delete an actively heartbeating generation.

Why this guard exists: the fork is the only side that can observe the
generation-private heartbeat, fencing registry, and binding record, so
the final pre-effect check must happen here, under the generation lock,
at the last possible instant.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TypeVar

from cli_agent_orchestrator.services import heartbeat_store
from cli_agent_orchestrator.services.durable_publish import (
    ABSENT,
    PublicationError,
    publish_mutable,
)

T = TypeVar("T")

INTENT_STATE_SCHEMA = "cao-destructive-intent-v1"
BINDING_RECORD_SCHEMA = "cao-generation-binding-v1"


class DestructiveError(RuntimeError):
    """Base error for destructive-endpoint operations."""


class DestructiveRefused(DestructiveError):
    """The conditional check failed; zero mutation occurred."""


@dataclass(frozen=True)
class DestructiveIntent:
    """One single-use destructive intent issued by the conductor."""

    intent_id: str
    kind: str
    terminal_id: str
    generation: str
    reservation_id: str
    attempt_id: str
    fencing_token_id: str
    requires_containment: bool = True


@contextmanager
def _generation_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".destructive.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _rfc3339_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def binding_record_path(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    return Path(companion_dir) / terminal_id / generation / "binding.json"


def write_binding_record(
    companion_dir: Path,
    *,
    terminal_id: str,
    generation: str,
    reservation_id: str,
    attempt_id: str,
    launch_nonce_digest: str,
    fencing_token_id: str,
    provider: str,
    native_session_id: str,
    assigned_policy_sha256: Optional[str] = None,
    route_payload_sha256: Optional[str] = None,
) -> Path:
    """Publish the fork-owned immutable binding record for a generation.

    Written once at bind time (P-IMM-style: the record is immutable; a
    resume writes a new generation's record, never rewrites this one).
    """
    path = binding_record_path(companion_dir, terminal_id, generation)
    if path.exists():
        raise DestructiveError(f"binding record already exists: {path}")
    record = {
        "schema": BINDING_RECORD_SCHEMA,
        "reservation_id": reservation_id,
        "terminal_id": terminal_id,
        "generation": generation,
        "attempt_id": attempt_id,
        "launch_nonce_digest": launch_nonce_digest,
        "fencing_token_id": fencing_token_id,
        "provider": provider,
        "native_session_id": native_session_id,
        "assigned_policy_sha256": assigned_policy_sha256,
        "route_payload_sha256": route_payload_sha256,
        "bound_at": _rfc3339_now(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        publish_mutable(
            path,
            json.dumps(record, sort_keys=True).encode() + b"\n",
            expected_old_sha256=ABSENT,
        )
    except PublicationError as exc:
        raise DestructiveError(str(exc)) from exc
    return path


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DestructiveError(f"record at {path} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise DestructiveError(f"record at {path} is not a JSON object")
    return parsed


class DestructiveEndpoint:
    """The single conditional endpoint for fork destructive effects."""

    def __init__(
        self,
        *,
        companion_dir: Path,
        containment_proven: Callable[[], bool] = lambda: False,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._dir = Path(companion_dir)
        self._containment_proven = containment_proven
        self._clock = clock

    def _intents_path(self, terminal_id: str, generation: str) -> Path:
        return self._dir / terminal_id / generation / "destructive-intents.json"

    def _load_intents(self, path: Path) -> dict[str, Any]:
        record = _read_json(path)
        if record is None:
            return {"schema": INTENT_STATE_SCHEMA, "intents": {}, "updated_seq": 0}
        if record.get("schema") != INTENT_STATE_SCHEMA or not isinstance(
            record.get("intents"), dict
        ):
            raise DestructiveError("destructive-intent store has an unknown schema")
        return record

    def _save_intents(self, path: Path, store: dict[str, Any]) -> None:
        store["updated_seq"] = int(store.get("updated_seq") or 0) + 1
        old = path.read_bytes() if path.exists() and not path.is_symlink() else None
        try:
            publish_mutable(
                path,
                json.dumps(store, sort_keys=True).encode() + b"\n",
                expected_old_sha256=(
                    hashlib.sha256(old).hexdigest() if old is not None else ABSENT
                ),
            )
        except PublicationError as exc:
            raise DestructiveError(str(exc)) from exc

    def _verify_binding(self, intent: DestructiveIntent) -> None:
        record = _read_json(binding_record_path(self._dir, intent.terminal_id, intent.generation))
        if record is None or record.get("schema") != BINDING_RECORD_SCHEMA:
            raise DestructiveRefused("no fork-owned binding record for this generation")
        expected = {
            "reservation_id": intent.reservation_id,
            "terminal_id": intent.terminal_id,
            "generation": intent.generation,
            "attempt_id": intent.attempt_id,
            "fencing_token_id": intent.fencing_token_id,
        }
        mismatches = [key for key, value in expected.items() if record.get(key) != value]
        if mismatches:
            raise DestructiveRefused(f"destructive binding mismatch: {sorted(mismatches)}")

    def _check_heartbeat(self, intent: DestructiveIntent) -> None:
        """Refuse when the generation may still be alive.

        ACTIVE, wrong-identity, malformed, and fencing-refused readings
        are all treated as possibly-alive and refuse; only readings that
        carry no evidence of life (missing, stale, regressed, skew) allow
        the caller's own dual proof to govern.
        """
        path = heartbeat_store.heartbeat_path(self._dir, intent.terminal_id, intent.generation)
        try:
            record = heartbeat_store._read_json(path)  # noqa: SLF001 - same package seam
        except heartbeat_store.HeartbeatSchemaError as exc:
            raise DestructiveRefused(f"heartbeat record malformed: {exc}") from exc
        if record is None:
            return  # no evidence of life
        try:
            heartbeat_store.validate_schema(record)
        except heartbeat_store.HeartbeatSchemaError as exc:
            raise DestructiveRefused(f"heartbeat record malformed: {exc}") from exc
        binding = (
            _read_json(binding_record_path(self._dir, intent.terminal_id, intent.generation)) or {}
        )
        if (
            record.get("reservation_id") != intent.reservation_id
            or record.get("terminal_id") != intent.terminal_id
            or record.get("generation") != intent.generation
            or record.get("attempt_id") != intent.attempt_id
        ):
            raise DestructiveRefused("heartbeat record carries a wrong identity")
        registered = heartbeat_store.current_fencing_token(self._dir, intent.terminal_id)
        token = record.get("fencing_token") or {}
        if registered is None or registered.id != token.get("id"):
            raise DestructiveRefused("heartbeat fencing token is not the registered one")
        expires = heartbeat_store._parse_time(record["lease_expires_at"])  # noqa: SLF001
        observed = heartbeat_store._parse_time(record["observed_at"])  # noqa: SLF001
        now = self._clock()
        capped = observed.timestamp() + min(
            int(record["lease_ttl_s"]), heartbeat_store.MAX_LEASE_TTL_S
        )
        from datetime import timedelta

        if now < min(expires, datetime.fromtimestamp(capped, tz=timezone.utc)) and now >= (
            observed - timedelta(seconds=heartbeat_store.SKEW_TOLERANCE_SECONDS)
        ):
            raise DestructiveRefused(
                "generation heartbeat reads ACTIVE; a missing callback never "
                "deletes an actively heartbeating generation"
            )

    def execute(
        self,
        intent: DestructiveIntent,
        effect: Callable[[], T],
    ) -> dict[str, Any]:
        """Verify, consume, and run one destructive effect under lock 4.

        Returns the endpoint receipt.  Re-issuing the *same* intent id
        after a crash re-drives a pending effect (effects must be
        idempotent) or returns the stored receipt; a *distinct* intent id
        is a new single-use token.
        """
        directory = self._dir / intent.terminal_id / intent.generation
        with _generation_lock(directory):
            if intent.requires_containment and not self._containment_proven():
                raise DestructiveRefused(
                    "effect class requires the containment composition, which is "
                    "unproven; the path stays preserved/alert-only"
                )
            self._verify_binding(intent)
            self._check_heartbeat(intent)
            path = self._intents_path(intent.terminal_id, intent.generation)
            store = self._load_intents(path)
            entry = store["intents"].get(intent.intent_id)
            if entry is not None and entry.get("state") == "effect-completed":
                return entry["receipt"]  # idempotent re-issue
            if entry is None:
                store["intents"][intent.intent_id] = {
                    "state": "effect-pending",
                    "kind": intent.kind,
                    "consumed_at": _rfc3339_now(),
                }
                self._save_intents(path, store)  # single-use consumed pre-effect
            result = effect()
            receipt = {
                "schema": "cao-destructive-receipt-v1",
                "intent_id": intent.intent_id,
                "kind": intent.kind,
                "terminal_id": intent.terminal_id,
                "generation": intent.generation,
                "outcome": "completed",
                "result": result if isinstance(result, (str, int, bool, type(None))) else None,
                "completed_at": _rfc3339_now(),
            }
            store = self._load_intents(path)
            store["intents"][intent.intent_id] = {
                "state": "effect-completed",
                "kind": intent.kind,
                "consumed_at": store["intents"][intent.intent_id]["consumed_at"],
                "receipt": receipt,
            }
            self._save_intents(path, store)
            return receipt
