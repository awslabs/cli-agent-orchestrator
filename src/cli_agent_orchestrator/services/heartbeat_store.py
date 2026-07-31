"""Fenced provider-activity heartbeat producer (schema-2 record).

A durable, provider-native activity lease exists per exact terminal
generation and active turn.  Heartbeats only ever *extend* life; their
absence never *proves* death.  The producer is the process that owns the
provider-native event stream for the generation (the managed provider
bridge, or the Claude hook receiver); pane text, TUI footers, token
counts, dashboard status, pipe-pane growth, generic process liveness,
and file mtimes are never beat sources.

Invariant: every write runs under the per-generation lock — re-read the
current record, refuse if it carries a different fencing token with a
higher fence number or if ``(epoch, seq)`` would regress, then publish
through the P-MUT fenced CAS.  A superseded producer (a resume created a
new attempt) has its token revoked and its writes are refused at the
compare step.

Failure mode prevented: without writer fencing, a stale or superseded
producer can keep a dead generation looking alive (or tear a live
record), and a deadman consumer can mistake that for proof of life —
the exact false-death/false-life pair that caused wrongful collection.

Why this guard exists: heartbeat-driven deletion suppression is the one
recovery-adjacent behavior usable before the containment/route gates go
green, and it is only sound if a missing callback can never delete an
actively heartbeating generation while a stale writer can never forge
activity.

Store: ``<COMPANION_DIR>/<terminal_id>/<generation>/heartbeat.json``
(0600, P-MUT).  The per-terminal fencing registry is
``<COMPANION_DIR>/<terminal_id>/fencing.json`` (0600, P-MUT, strictly
increasing fence numbers).  No secrets, prompt text, or transcript
content is ever stored; the launch nonce appears only as a digest.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from cli_agent_orchestrator.constants import COMPANION_DIR
from cli_agent_orchestrator.services.durable_publish import (
    ABSENT,
    PublicationConflict,
    PublicationError,
    publish_mutable,
)

HEARTBEAT_SCHEMA_VERSION = 2
PROTOCOL_VINTAGE = "v2"
COALESCE_SECONDS = 20
DEFAULT_LEASE_TTL_S = 90
MAX_LEASE_TTL_S = 300
SKEW_TOLERANCE_SECONDS = 5

EVIDENCE_KINDS = ("app_server_event", "acp_update", "hook_event")
TURN_STATES = ("active", "terminal", "unknown")


class HeartbeatError(RuntimeError):
    """Base error for heartbeat producer failures."""


class FencingRefused(HeartbeatError):
    """A superseded or regressive writer was refused at the compare step."""


class HeartbeatSchemaError(HeartbeatError):
    """A heartbeat record failed schema or identity validation."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise HeartbeatSchemaError("timestamp is not a string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HeartbeatSchemaError(f"timestamp is not RFC3339: {value!r}") from exc
    if parsed.tzinfo is None:
        raise HeartbeatSchemaError("timestamp lacks a timezone")
    return parsed.astimezone(timezone.utc)


def _generation_dir(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    return Path(companion_dir) / terminal_id / generation


def heartbeat_path(companion_dir: Path, terminal_id: str, generation: str) -> Path:
    return _generation_dir(companion_dir, terminal_id, generation) / "heartbeat.json"


def _fencing_path(companion_dir: Path, terminal_id: str) -> Path:
    return Path(companion_dir) / terminal_id / "fencing.json"


@contextmanager
def _generation_lock(companion_dir: Path, terminal_id: str, generation: str) -> Iterator[None]:
    """The per-generation fork lock (lock class 4) for heartbeat writes."""
    directory = _generation_dir(companion_dir, terminal_id, generation)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".heartbeat.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def _terminal_lock(companion_dir: Path, terminal_id: str) -> Iterator[None]:
    """The per-terminal lock serializing fencing-token issuance.

    The fencing registry is terminal-scoped, so issuance must serialize on
    a terminal-scoped lock — a per-generation lock lets two generations of
    one terminal read the same prior fence number and both publish
    ``fence_no = prior + 1``, breaking strict monotonicity across
    generations.
    """
    directory = Path(companion_dir) / terminal_id
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".fencing.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass(frozen=True)
class FencingToken:
    """Immutable producer fencing token: UUID + per-terminal fence number."""

    id: str
    fence_no: int

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "fence_no": self.fence_no}


@dataclass(frozen=True)
class HeartbeatIdentity:
    """The immutable identity every beat of one generation binds."""

    project: str
    task_id: Optional[str]
    run_id: str
    obligation_generation: str
    reservation_id: str
    launch_nonce_digest: str
    terminal_id: str
    generation: str
    attempt_id: str
    provider: str
    provider_version: str
    native_session_id: str
    assigned_policy_sha256: str
    segment_hash: str

    def identity_fields(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "obligation_generation": self.obligation_generation,
            "reservation_id": self.reservation_id,
            "launch_nonce_digest": self.launch_nonce_digest,
            "terminal_id": self.terminal_id,
            "generation": self.generation,
            "attempt_id": self.attempt_id,
            "provider": self.provider,
            "provider_version": self.provider_version,
            "native_session_id": self.native_session_id,
            "route": {
                "assigned_policy_sha256": self.assigned_policy_sha256,
                "segment_hash": self.segment_hash,
            },
        }


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        parsed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HeartbeatSchemaError(f"record at {path} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise HeartbeatSchemaError(f"record at {path} is not a JSON object")
    return parsed


def _sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def issue_fencing_token(
    companion_dir: Path,
    terminal_id: str,
    generation: str,
    attempt_id: str,
) -> FencingToken:
    """Issue the next fencing token for a terminal, superseding any prior.

    Issued at reservation (and again at each resume attempt): the fence
    number strictly increases per terminal, so a superseded producer's
    token compares stale and its writes are refused.  Issuance is
    serialized on the *terminal*-scoped lock (the registry is per
    terminal) and then runs under the target generation's lock, so
    concurrent issuance for two generations of one terminal still yields
    strictly increasing fence numbers.
    """
    with _terminal_lock(companion_dir, terminal_id):
        with _generation_lock(companion_dir, terminal_id, generation):
            path = _fencing_path(companion_dir, terminal_id)
            current = _read_json(path)
            prior_no = 0
            if current is not None:
                token = current.get("current_token") or {}
                prior_no = int(token.get("fence_no") or 0)
            new_token = FencingToken(id=str(uuid.uuid4()), fence_no=prior_no + 1)
            record = {
                "schema": "cao-producer-fencing-v1",
                "terminal_id": terminal_id,
                "generation": generation,
                "attempt_id": attempt_id,
                "current_token": new_token.as_dict(),
                "updated_seq": new_token.fence_no,
                "issued_at": _rfc3339(_utcnow()),
            }
            old_sha = _sha(path)
            publish_mutable(
                path,
                json.dumps(record, sort_keys=True).encode() + b"\n",
                expected_old_sha256=old_sha if old_sha is not None else ABSENT,
            )
            return new_token


def current_fencing_record(companion_dir: Path, terminal_id: str) -> Optional[dict[str, Any]]:
    """The full fencing registry record (token + bound generation), or None."""
    return _read_json(_fencing_path(companion_dir, terminal_id))


@contextmanager
def successor_critical_section(companion_dir: Path, terminal_id: str) -> Iterator[None]:
    """Serialize provider admission with successor/resume token issuance."""
    with _terminal_lock(companion_dir, terminal_id):
        yield


def assert_current_fencing_binding(
    companion_dir: Path,
    *,
    terminal_id: str,
    generation: str,
    attempt_id: str,
    fencing_token_id: str,
) -> None:
    """Revalidate the exact current producer token while successor lock is held."""
    record = current_fencing_record(companion_dir, terminal_id)
    token = (record or {}).get("current_token") or {}
    expected = {
        "terminal_id": terminal_id,
        "generation": generation,
        "attempt_id": attempt_id,
        "token_id": fencing_token_id,
    }
    observed = {
        "terminal_id": (record or {}).get("terminal_id"),
        "generation": (record or {}).get("generation"),
        "attempt_id": (record or {}).get("attempt_id"),
        "token_id": token.get("id"),
    }
    if observed != expected:
        raise FencingRefused(
            "provider admission lost the current successor/heartbeat fencing token"
        )


def current_fencing_token(companion_dir: Path, terminal_id: str) -> Optional[FencingToken]:
    record = _read_json(_fencing_path(companion_dir, terminal_id))
    if record is None:
        return None
    token = record.get("current_token") or {}
    token_id = token.get("id")
    fence_no = token.get("fence_no")
    if not isinstance(token_id, str) or not isinstance(fence_no, int):
        raise HeartbeatSchemaError("fencing registry carries a malformed token")
    return FencingToken(id=token_id, fence_no=fence_no)


def validate_schema(record: dict[str, Any]) -> None:
    """Validate the schema-2 record shape; identity binding is separate."""
    if record.get("schema_version") != HEARTBEAT_SCHEMA_VERSION:
        raise HeartbeatSchemaError(
            f"schema_version must be {HEARTBEAT_SCHEMA_VERSION}: {record.get('schema_version')!r}"
        )
    if record.get("protocol_vintage") != PROTOCOL_VINTAGE:
        raise HeartbeatSchemaError("protocol_vintage must be v2")
    for field_name in (
        "project",
        "run_id",
        "obligation_generation",
        "reservation_id",
        "launch_nonce_digest",
        "terminal_id",
        "generation",
        "attempt_id",
        "provider",
        "provider_version",
        "native_session_id",
        "observed_at",
        "lease_expires_at",
    ):
        if not isinstance(record.get(field_name), str) or not record[field_name]:
            raise HeartbeatSchemaError(f"missing or invalid field: {field_name}")
    if record.get("task_id") is not None and not isinstance(record.get("task_id"), str):
        raise HeartbeatSchemaError("task_id must be a string or null")
    route = record.get("route")
    if not isinstance(route, dict) or not all(
        isinstance(route.get(key), str) and len(route[key]) == 64
        for key in ("assigned_policy_sha256", "segment_hash")
    ):
        raise HeartbeatSchemaError("route must bind assigned_policy_sha256 and segment_hash")
    token = record.get("fencing_token")
    if (
        not isinstance(token, dict)
        or not isinstance(token.get("id"), str)
        or not isinstance(token.get("fence_no"), int)
        or token["fence_no"] < 1
    ):
        raise HeartbeatSchemaError("fencing_token must carry an id and a positive fence_no")
    if not isinstance(record.get("epoch"), int) or record["epoch"] < 1:
        raise HeartbeatSchemaError("epoch must be a positive integer")
    if not isinstance(record.get("seq"), int) or record["seq"] < 1:
        raise HeartbeatSchemaError("seq must be a positive integer")
    turn = record.get("turn")
    if not isinstance(turn, dict) or turn.get("state") not in TURN_STATES:
        raise HeartbeatSchemaError(f"turn.state must be one of {TURN_STATES}")
    evidence = record.get("evidence")
    if not isinstance(evidence, dict) or evidence.get("kind") not in EVIDENCE_KINDS:
        raise HeartbeatSchemaError(f"evidence.kind must be one of {EVIDENCE_KINDS}")
    if not isinstance(evidence.get("id"), str) or not evidence["id"]:
        raise HeartbeatSchemaError("evidence.id must be a non-empty string")
    ttl = record.get("lease_ttl_s")
    if not isinstance(ttl, int) or ttl < 1:
        raise HeartbeatSchemaError("lease_ttl_s must be a positive integer")
    _parse_time(record["observed_at"])
    _parse_time(record["lease_expires_at"])


class HeartbeatProducer:
    """The fenced producer for one exact terminal generation."""

    def __init__(
        self,
        *,
        companion_dir: Path,
        identity: HeartbeatIdentity,
        token: FencingToken,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._dir = Path(companion_dir)
        self._identity = identity
        self._token = token
        self._clock = clock
        self._epoch = token.fence_no
        self._seq = 0
        self._last_write: Optional[datetime] = None

    def _rehydrate(self, path: Path) -> None:
        """Restore durable producer position for a reconstructed producer.

        A producer is not a singleton: the bridge or a restarted process
        may construct a fresh instance for the same generation and token.
        Under the generation lock, a current record written by *this*
        token rehydrates ``(epoch, seq)`` and the coalescing watermark so
        the sequence continues instead of restarting at zero (which the
        fencing compare step would — correctly — refuse as a regression).
        A record from a different token is left to ``_check_writer``.
        """
        try:
            current = _read_json(path)
        except HeartbeatSchemaError:
            return  # malformed: the P-MUT CAS/fence check governs
        if current is None:
            return
        theirs = current.get("fencing_token") or {}
        if theirs.get("id") != self._token.id:
            return
        self._epoch = max(self._epoch, int(current.get("epoch") or 0))
        self._seq = max(self._seq, int(current.get("seq") or 0))
        if self._last_write is None and isinstance(current.get("observed_at"), str):
            try:
                self._last_write = _parse_time(current["observed_at"])
            except HeartbeatSchemaError:
                pass

    def _check_writer(self, current: Optional[dict[str, Any]]) -> None:
        """Refuse a superseded or regressive writer with zero mutation."""
        if current is None:
            return
        theirs = current.get("fencing_token") or {}
        if (
            theirs.get("id") != self._token.id
            and int(theirs.get("fence_no") or 0) > self._token.fence_no
        ):
            raise FencingRefused(
                "current record carries a different fencing token with a higher "
                "fence number; this producer is superseded"
            )
        position = (int(current.get("epoch") or 0), int(current.get("seq") or 0))
        if (self._epoch, self._seq + 1) <= position:
            raise FencingRefused(
                f"(epoch, seq) would regress: current {position}, next {(self._epoch, self._seq + 1)}"
            )

    def beat(
        self,
        *,
        turn_state: str,
        provider_turn_id: str,
        evidence_kind: str,
        evidence_id: str,
        lease_ttl_s: int = DEFAULT_LEASE_TTL_S,
        force: bool = False,
    ) -> Optional[dict[str, Any]]:
        """Emit one heartbeat; returns the record, or None when coalesced.

        Cadence: at most one durable write per COALESCE_SECONDS while a
        turn is active; a turn-terminal event always writes a final beat.
        """
        if turn_state not in TURN_STATES:
            raise HeartbeatSchemaError(f"turn_state must be one of {TURN_STATES}")
        if evidence_kind not in EVIDENCE_KINDS:
            raise HeartbeatSchemaError(f"evidence_kind must be one of {EVIDENCE_KINDS}")
        now = self._clock()
        if (
            not force
            and turn_state == "active"
            and self._last_write is not None
            and (now - self._last_write) < timedelta(seconds=COALESCE_SECONDS)
        ):
            return None
        ttl = min(lease_ttl_s, MAX_LEASE_TTL_S)
        if ttl < 1:
            raise HeartbeatSchemaError("lease_ttl_s must be positive")
        identity = self._identity
        with _generation_lock(self._dir, identity.terminal_id, identity.generation):
            registered = current_fencing_token(self._dir, identity.terminal_id)
            if registered is None or registered.id != self._token.id:
                raise FencingRefused(
                    "this producer's fencing token is not the currently registered "
                    "one (revoked or never issued)"
                )
            path = heartbeat_path(self._dir, identity.terminal_id, identity.generation)
            self._rehydrate(path)
            if (
                not force
                and turn_state == "active"
                and self._last_write is not None
                and (now - self._last_write) < timedelta(seconds=COALESCE_SECONDS)
            ):
                # The durable watermark (rehydrated above) coalesces beats
                # from a reconstructed producer exactly as for a live one.
                return None
            record = {
                "schema_version": HEARTBEAT_SCHEMA_VERSION,
                "protocol_vintage": PROTOCOL_VINTAGE,
                **identity.identity_fields(),
                "fencing_token": self._token.as_dict(),
                "epoch": self._epoch,
                "seq": self._seq + 1,
                "turn": {"state": turn_state, "provider_turn_id": provider_turn_id},
                "evidence": {"kind": evidence_kind, "id": evidence_id},
                "observed_at": _rfc3339(now),
                "lease_ttl_s": ttl,
                "lease_expires_at": _rfc3339(now + timedelta(seconds=ttl)),
            }
            validate_schema(record)
            old_sha = _sha(path)
            try:
                publish_mutable(
                    path,
                    json.dumps(record, sort_keys=True).encode() + b"\n",
                    expected_old_sha256=old_sha if old_sha is not None else ABSENT,
                    fence_check=self._check_writer,
                    seq_field=None,
                )
            except PublicationConflict as exc:
                raise FencingRefused(str(exc)) from exc
            except PublicationError as exc:
                raise HeartbeatError(str(exc)) from exc
            self._seq += 1
            self._last_write = now
            return record

    def terminal_beat(self, *, provider_turn_id: str, evidence_kind: str, evidence_id: str):
        """The final beat for a turn-terminal provider-native event."""
        return self.beat(
            turn_state="terminal",
            provider_turn_id=provider_turn_id,
            evidence_kind=evidence_kind,
            evidence_id=evidence_id,
            force=True,
        )


# ------------------------------------------------------------- consumer side
#
# The authoritative consumer is the conductor sentinel (a separate
# repository).  This evaluator is the shared, strict validator both sides
# derive from: a reader computes ACTIVE only as specified, and every
# other outcome is unknown ⇒ fail closed.

READING_ACTIVE = "ACTIVE"
READING_STALE = "STALE"
READING_MISSING = "MISSING"
READING_MALFORMED = "MALFORMED"
READING_WRONG_IDENTITY = "WRONG-IDENTITY"
READING_FENCING_REFUSED = "FENCING-REFUSED"
READING_REGRESSED = "REGRESSED"
READING_SKEW = "SKEW-REJECTED"


@dataclass(frozen=True)
class HeartbeatReading:
    status: str
    reason: str = ""
    record: Optional[dict[str, Any]] = field(default=None, compare=False)


def read_heartbeat(
    companion_dir: Path,
    *,
    identity: HeartbeatIdentity,
    high_water: Optional[tuple[int, int]] = None,
    now: Optional[datetime] = None,
) -> HeartbeatReading:
    """Evaluate the durable record under the bounded-expiry consumer rules.

    ACTIVE requires: schema-valid record; exact identity match against
    the run's task/run/obligation/reservation/terminal generation and
    native session; the currently registered fencing token;
    ``(epoch, seq)`` at or above the caller's durable high-water;
    ``observed_at`` not in the future beyond the skew tolerance; and an
    unexpired lease with the TTL capped at MAX_LEASE_TTL_S regardless of
    the record's claim.  Everything else is a distinct fail-closed
    status — a reader must never treat any of them as death, and must
    never treat them as life.
    """
    moment = now or _utcnow()
    path = heartbeat_path(companion_dir, identity.terminal_id, identity.generation)
    try:
        record = _read_json(path)
    except HeartbeatSchemaError as exc:
        return HeartbeatReading(READING_MALFORMED, str(exc))
    if record is None:
        return HeartbeatReading(READING_MISSING)
    try:
        validate_schema(record)
    except HeartbeatSchemaError as exc:
        return HeartbeatReading(READING_MALFORMED, str(exc))

    expected = identity.identity_fields()
    mismatches = [
        key for key, value in expected.items() if key not in ("route",) and record.get(key) != value
    ]
    if mismatches or record.get("route") != expected["route"]:
        return HeartbeatReading(
            READING_WRONG_IDENTITY, f"identity mismatch: {sorted(mismatches)}", record
        )

    registered = current_fencing_token(companion_dir, identity.terminal_id)
    token = record["fencing_token"]
    if registered is None or registered.id != token["id"]:
        return HeartbeatReading(
            READING_FENCING_REFUSED, "record token is not the registered token", record
        )

    position = (record["epoch"], record["seq"])
    if high_water is not None and position < high_water:
        return HeartbeatReading(
            READING_REGRESSED,
            f"(epoch, seq) {position} below consumer high-water {high_water}",
            record,
        )

    observed_at = _parse_time(record["observed_at"])
    if observed_at > moment + timedelta(seconds=SKEW_TOLERANCE_SECONDS):
        return HeartbeatReading(READING_SKEW, "observed_at is beyond the skew tolerance", record)

    expires_at = _parse_time(record["lease_expires_at"])
    capped_expiry = observed_at + timedelta(seconds=min(record["lease_ttl_s"], MAX_LEASE_TTL_S))
    effective_expiry = min(expires_at, capped_expiry)
    if moment >= effective_expiry:
        return HeartbeatReading(READING_STALE, "lease expired", record)

    return HeartbeatReading(READING_ACTIVE, record=record)
