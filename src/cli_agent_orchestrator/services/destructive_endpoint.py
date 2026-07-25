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
from datetime import datetime, timedelta, timezone
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
DUAL_EXIT_PROOF_SCHEMA = "cao-dual-exit-proof-v1"

# Server-side effect classification: whether an effect kind requires the
# proven containment composition is derived from the fork's own effect
# table — NEVER from a caller-supplied request bit.
CONTAINMENT_REQUIRED_KINDS = frozenset({"terminal-teardown"})


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


def dual_exit_proof_path(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    return Path(companion_dir) / terminal_id / generation / "dual-exit.json"


def write_dual_exit_proof(
    companion_dir: Path,
    *,
    terminal_id: str,
    generation: str,
    reservation_id: str,
    attempt_id: str,
    fencing_token_id: str,
    provider_exit: dict[str, Any],
    bridge_exit: dict[str, Any],
) -> Path:
    """Publish the durable dual-exit proof for a dead generation (P-IMM-style).

    The proof asserts that BOTH the provider process and the bridge
    exited (the two halves that could still be heartbeating), bound to
    the exact generation identity and fencing token.  It is the only
    evidence under which the destructive endpoint may act on a heartbeat
    reading that is missing, stale, skewed, malformed, or mismatched.
    Written once; a second write for the same generation is refused.
    """
    if not provider_exit or not bridge_exit:
        raise DestructiveError("a dual-exit proof requires both exit observations")
    path = dual_exit_proof_path(companion_dir, terminal_id, generation)
    if path.exists():
        raise DestructiveError(f"dual-exit proof already exists: {path}")
    record = {
        "schema": DUAL_EXIT_PROOF_SCHEMA,
        "terminal_id": terminal_id,
        "generation": generation,
        "reservation_id": reservation_id,
        "attempt_id": attempt_id,
        "fencing_token_id": fencing_token_id,
        "provider_exit": provider_exit,
        "bridge_exit": bridge_exit,
        "recorded_at": _rfc3339_now(),
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
    bound_at: Optional[str] = None,
    execution_mode: Optional[str] = None,
) -> Path:
    """Publish the fork-owned immutable binding record for a generation.

    Written once at bind time (P-IMM-style: the record is immutable; a
    resume writes a new generation's record, never rewrites this one).
    ``bound_at`` is supplied by the journaled bind intent so a reconciled
    retry reproduces byte-identical record content.

    ``execution_mode`` tags the record with the branch the generation was
    bound on, so this published evidence cannot be read as satisfying the
    other mode's admission.  It is *omitted* rather than written as null
    when absent: a record published before the execution-mode contract
    existed has no such key, and a reconciling retry must reproduce those
    exact bytes.  A reader therefore applies the same rule as for a
    pre-contract database row — key absent means legacy ACP, and it must
    never be read as native.
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
        "bound_at": bound_at or _rfc3339_now(),
    }
    if execution_mode is not None:
        record["execution_mode"] = execution_mode
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
        containment_required: Optional[Callable[[str], bool]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._dir = Path(companion_dir)
        self._containment_proven = containment_proven
        # Effect classification is server-side: the caller's request can
        # never downgrade a containment-requiring effect class.
        self._containment_required = containment_required or (
            lambda kind: kind in CONTAINMENT_REQUIRED_KINDS
        )
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

    def _verify_dual_exit(self, intent: DestructiveIntent) -> None:
        """Require the durable, server-verified dual-exit proof.

        The proof must exist, carry the exact generation identity, and
        bind the same fencing token as the fork-owned binding record.
        Anything less fails closed.
        """
        record = _read_json(dual_exit_proof_path(self._dir, intent.terminal_id, intent.generation))
        if record is None or record.get("schema") != DUAL_EXIT_PROOF_SCHEMA:
            raise DestructiveRefused(
                "no durable dual-exit proof for this generation; a missing, "
                "stale, skewed, malformed, or mismatched heartbeat fails closed"
            )
        binding = (
            _read_json(binding_record_path(self._dir, intent.terminal_id, intent.generation)) or {}
        )
        expected = {
            "terminal_id": intent.terminal_id,
            "generation": intent.generation,
            "reservation_id": intent.reservation_id,
            "attempt_id": intent.attempt_id,
            "fencing_token_id": binding.get("fencing_token_id"),
        }
        mismatches = [key for key, value in expected.items() if record.get(key) != value]
        if mismatches:
            raise DestructiveRefused(f"dual-exit proof identity mismatch: {sorted(mismatches)}")
        if not isinstance(record.get("provider_exit"), dict) or not isinstance(
            record.get("bridge_exit"), dict
        ):
            raise DestructiveRefused("dual-exit proof is incomplete (needs both exits)")

    def _check_heartbeat(self, intent: DestructiveIntent) -> None:
        """Fail closed on every heartbeat reading that is not provably dead.

        An ACTIVE reading always refuses with zero mutation — a missing
        callback never deletes an actively heartbeating generation.  Every
        other reading (missing, stale, regressed, skewed, malformed,
        wrong-identity, fencing-mismatch) is untrustworthy rather than
        dead: the effect may proceed only against a durable, complete,
        server-verified dual-exit proof.
        """
        path = heartbeat_store.heartbeat_path(self._dir, intent.terminal_id, intent.generation)
        record: Optional[dict[str, Any]] = None
        malformed: Optional[str] = None
        try:
            record = heartbeat_store._read_json(path)  # noqa: SLF001 - same package seam
            if record is not None:
                heartbeat_store.validate_schema(record)
        except heartbeat_store.HeartbeatSchemaError as exc:
            malformed = str(exc)
        if malformed is None and record is not None:
            if (
                record.get("reservation_id") != intent.reservation_id
                or record.get("terminal_id") != intent.terminal_id
                or record.get("generation") != intent.generation
                or record.get("attempt_id") != intent.attempt_id
            ):
                # Wrong identity is not death; require the dual-exit proof.
                self._verify_dual_exit(intent)
                return
            registered = heartbeat_store.current_fencing_token(self._dir, intent.terminal_id)
            token = record.get("fencing_token") or {}
            if registered is None or registered.id != token.get("id"):
                self._verify_dual_exit(intent)
                return
            expires = heartbeat_store._parse_time(record["lease_expires_at"])  # noqa: SLF001
            observed = heartbeat_store._parse_time(record["observed_at"])  # noqa: SLF001
            now = self._clock()
            if observed > now + timedelta(seconds=heartbeat_store.SKEW_TOLERANCE_SECONDS):
                # A future-dated record is untrustworthy, never permission.
                self._verify_dual_exit(intent)
                return
            capped = observed.timestamp() + min(
                int(record["lease_ttl_s"]), heartbeat_store.MAX_LEASE_TTL_S
            )
            if now < min(expires, datetime.fromtimestamp(capped, tz=timezone.utc)):
                raise DestructiveRefused(
                    "generation heartbeat reads ACTIVE; a missing callback never "
                    "deletes an actively heartbeating generation"
                )
            # Stale/expired lease: not proof of death on its own.
            self._verify_dual_exit(intent)
            return
        # Missing or malformed record: fail closed absent the dual-exit proof.
        self._verify_dual_exit(intent)

    def execute(
        self,
        intent: DestructiveIntent,
        effect: Callable[[], T],
    ) -> dict[str, Any]:
        """Verify, consume, and run one destructive effect under lock 4.

        The final generation/fence critical section (the fence lock, then
        the heartbeat lock, then the destructive-intent lock, in that
        fixed order) is held through the effect itself, so no concurrent
        beat or fence install can land between the last verification and
        the external effect.  Returns the endpoint receipt.  Re-issuing
        the *same* intent id after a crash re-drives a pending effect
        (effects must be idempotent) or returns the stored receipt; a
        *distinct* intent id is a new single-use token.
        """
        from cli_agent_orchestrator.services import generation_fence

        directory = self._dir / intent.terminal_id / intent.generation
        with generation_fence._generation_lock(directory):  # noqa: SLF001 - shared lock 4
            with heartbeat_store._generation_lock(  # noqa: SLF001 - shared lock 4
                self._dir, intent.terminal_id, intent.generation
            ):
                with _generation_lock(directory):
                    return self._execute_locked(intent, effect)

    def _execute_locked(
        self,
        intent: DestructiveIntent,
        effect: Callable[[], T],
    ) -> dict[str, Any]:
        if self._containment_required(intent.kind) and not self._containment_proven():
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
            receipt: dict[str, Any] = entry["receipt"]
            return receipt  # idempotent re-issue
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
