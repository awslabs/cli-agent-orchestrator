"""Non-forgeable worker-provenance actor broker.

The generation-private bridge is the actor broker: it verifies kernel
peer identity (macOS ``LOCAL_PEERCRED``/``LOCAL_PEERPID``; Linux
``SO_PEERCRED``) and live provider-tree lineage under the generation
lock, and issues one-use, short-lived, signed assertions binding the
report digest/path, project/task/run/obligation/attempt, terminal
generation, native session, launch nonce digest, and route-chain head.

Invariant: an assertion is issuable only to a process inside the
provider's own live process tree for this exact generation; it is
usable exactly once; it expires; and it fails the binding on any
stale/resumed generation.  The HMAC signing key lives in broker memory
only — never in a pathname, argv, env, or any file.

Failure mode prevented: without kernel-verified provenance, a same-UID
collector, reconciler, or sibling process could mint or replay a
"worker-authored" attestation for a report it never wrote — the report-
synthesis path this design removes structurally.

Why this guard exists: report acceptance requires proof that the exact
sealed generation's own provider tree authored the report; platform
inability to prove that must fail closed (``actor-unavailable``), never
degrade to bearer files or path-based trust.  The trust model is honest
anti-accident scope, not a security boundary.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from cli_agent_orchestrator.services.canonical_json import encode_canonical
from cli_agent_orchestrator.services.durable_publish import (
    ABSENT,
    PublicationError,
    publish_mutable,
)

ASSERTION_SCHEMA = "cao-actor-assertion-v1"
ASSERTION_TTL_SECONDS = 120

ASSERTION_FIELD_ORDER = (
    "schema",
    "assertion_id",
    "report_sha256",
    "report_path",
    "project",
    "task_id",
    "run_id",
    "obligation_generation",
    "attempt_id",
    "terminal_generation",
    "native_session_id",
    "launch_nonce_digest",
    "route_chain_head",
    "peer_pid",
    "issued_at",
    "expires_at",
)


class ActorBrokerError(RuntimeError):
    """Base error for actor-broker operations."""


class ActorUnavailable(ActorBrokerError):
    """The platform cannot prove peer identity or lineage: fail closed."""


class ActorRefused(ActorBrokerError):
    """The peer is outside the provider tree for this generation."""


class AssertionInvalid(ActorBrokerError):
    """An assertion is forged, replayed, expired, or generation-stale."""


@dataclass(frozen=True)
class PeerCredentials:
    pid: int
    uid: int


def peer_credentials(conn: socket.socket) -> PeerCredentials:
    """Kernel-verified peer identity for a Unix-domain connection.

    Linux: ``SO_PEERCRED`` (struct ucred: pid, uid, gid).  macOS:
    ``LOCAL_PEERPID`` (pid) + ``LOCAL_PEERCRED`` (struct xucred: version,
    uid, ngroups, groups…).  Any other platform, or any failure, is
    ``ActorUnavailable`` — provenance fails closed, never assumed.
    """
    if sys.platform.startswith("linux"):
        data = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        pid, uid, _gid = struct.unpack("3i", data)
        return PeerCredentials(pid=pid, uid=uid)
    if sys.platform == "darwin":
        local_peerpid = 0x002
        local_peercred = 0x001
        pid = struct.unpack("i", conn.getsockopt(0, local_peerpid, 4))[0]
        # xucred: u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t[16]
        data = conn.getsockopt(0, local_peercred, 4 + 4 + 2 + 2 + 16 * 4)
        _version, uid = struct.unpack("II", data[:8])
        return PeerCredentials(pid=pid, uid=uid)
    raise ActorUnavailable(f"kernel peer credentials are not implemented for {sys.platform!r}")


def _default_lineage_checker(provider_pids: frozenset[int]) -> Callable[[int], bool]:
    """Walk the peer's parent chain to the provider process tree.

    Implemented with ``ps`` (portable across Linux/macOS); any inability
    to read the chain answers False, which the broker treats as refusal —
    lineage is proven, never assumed.
    """

    def is_in_tree(pid: int) -> bool:
        seen = set()
        current = pid
        for _ in range(64):
            if current in provider_pids:
                return True
            if current in seen or current <= 1:
                return False
            seen.add(current)
            try:
                out = subprocess.run(
                    ["ps", "-o", "ppid=", "-p", str(current)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            text = out.stdout.strip()
            if not text:
                return False
            try:
                current = int(text)
            except ValueError:
                return False
        return False

    return is_in_tree


class ActorBroker:
    """Issues and consumes one-use signed actor assertions for one generation."""

    def __init__(
        self,
        *,
        state_dir: Path,
        terminal_generation: str,
        provider_pids: frozenset[int],
        lineage_checker: Optional[Callable[[int], bool]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        signing_key: Optional[bytes] = None,
        generation_current: Callable[[], bool] = lambda: True,
    ) -> None:
        self._dir = Path(state_dir)
        self._generation = terminal_generation
        self._lineage = lineage_checker or _default_lineage_checker(provider_pids)
        self._clock = clock
        # The signing key is memory-only by construction: it is generated
        # per broker instance and never written to any file, pathname,
        # argv, or environment.
        self._key = signing_key if signing_key is not None else secrets.token_bytes(32)
        self._generation_current = generation_current

    def _consumed_path(self) -> Path:
        return self._dir / "actor-assertions.json"

    @contextmanager
    def _consumption_lock(self) -> Iterator[None]:
        """The cross-process one-use transaction lock.

        ``verify_and_consume`` is a check-then-write transaction: without
        a shared lock, two broker instances (separate processes or
        objects over one state dir) can both load, both see the assertion
        unconsumed, and both accept — consuming one assertion twice.
        flock serializes the load/check/persist across processes.
        """
        self._dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._dir / ".actor.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load_consumed(self) -> dict[str, Any]:
        try:
            raw = self._consumed_path().read_bytes()
        except FileNotFoundError:
            return {"schema": "cao-actor-consumed-v1", "consumed": [], "updated_seq": 0}
        try:
            parsed = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ActorBrokerError("consumed-assertion store is not valid JSON") from exc
        if not isinstance(parsed, dict) or not isinstance(parsed.get("consumed"), list):
            raise ActorBrokerError("consumed-assertion store has an unknown shape")
        return parsed

    def verify_peer_lineage(self, conn: socket.socket) -> PeerCredentials:
        """Kernel peer identity + live provider-tree lineage, without issuing.

        The same gate ``issue`` enforces (kernel credentials, then lineage
        against the live provider process tree), exposed so the bridge can
        authenticate the provider-originated channel itself. Refusal is
        fail-closed: ``ActorUnavailable`` on kernel inability,
        ``ActorRefused`` for any peer outside the provider tree.
        """
        try:
            credentials = peer_credentials(conn)
        except (OSError, struct.error) as exc:
            raise ActorUnavailable(f"kernel peer identity unavailable: {exc}") from exc
        if credentials.pid <= 0:
            raise ActorUnavailable("kernel reported an invalid peer pid")
        if not self._lineage(credentials.pid):
            raise ActorRefused(
                "peer is outside the live provider process tree for this "
                "generation (same-UID collector/reconciler/sibling refused)"
            )
        return credentials

    def issue(
        self,
        conn: socket.socket,
        *,
        report_sha256: str,
        report_path: str,
        project: str,
        task_id: Optional[str],
        run_id: str,
        obligation_generation: str,
        attempt_id: str,
        native_session_id: str,
        launch_nonce_digest: str,
        route_chain_head: str,
        peer: Optional[PeerCredentials] = None,
    ) -> dict[str, Any]:
        """Issue a one-use assertion to a kernel-verified in-tree peer.

        ``peer`` may be supplied directly by a caller that already
        performed the kernel getsockopt (tests, or the bridge's accept
        path); otherwise it is read from ``conn``.
        """
        if not self._generation_current():
            raise AssertionInvalid(
                "this generation has been superseded; stale generations issue nothing"
            )
        try:
            credentials = peer if peer is not None else peer_credentials(conn)
        except (OSError, struct.error) as exc:
            raise ActorUnavailable(f"kernel peer identity unavailable: {exc}") from exc
        if credentials.pid <= 0:
            raise ActorUnavailable("kernel reported an invalid peer pid")
        if not self._lineage(credentials.pid):
            raise ActorRefused(
                "peer is outside the live provider process tree for this "
                "generation (same-UID collector/reconciler/sibling refused)"
            )
        now = self._clock()
        assertion = {
            "schema": ASSERTION_SCHEMA,
            "assertion_id": str(uuid.uuid4()),
            "report_sha256": report_sha256,
            "report_path": report_path,
            "project": project,
            "task_id": task_id,
            "run_id": run_id,
            "obligation_generation": obligation_generation,
            "attempt_id": attempt_id,
            "terminal_generation": self._generation,
            "native_session_id": native_session_id,
            "launch_nonce_digest": launch_nonce_digest,
            "route_chain_head": route_chain_head,
            "peer_pid": credentials.pid,
            "issued_at": now.isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(seconds=ASSERTION_TTL_SECONDS))
            .isoformat()
            .replace("+00:00", "Z"),
        }
        assertion["signature"] = self._sign(assertion)
        return assertion

    def _sign(self, assertion: dict[str, Any]) -> str:
        body = {field: assertion.get(field) for field in ASSERTION_FIELD_ORDER}
        return hmac.new(self._key, encode_canonical(body), hashlib.sha256).hexdigest()

    def verify_and_consume(self, assertion: dict[str, Any]) -> None:
        """Verify an assertion and mark its one use, atomically durable.

        Replay (a captured assertion presented again), expiry, signature
        mismatch, wrong generation, or a superseded (resumed) generation
        all fail the binding.
        """
        if not isinstance(assertion, dict) or assertion.get("schema") != ASSERTION_SCHEMA:
            raise AssertionInvalid("unknown assertion schema")
        if assertion.get("terminal_generation") != self._generation:
            raise AssertionInvalid("assertion is bound to a different terminal generation")
        if not self._generation_current():
            raise AssertionInvalid("this generation has been superseded (resumed)")
        expected = self._sign(assertion)
        presented = assertion.get("signature")
        if not isinstance(presented, str) or not hmac.compare_digest(expected, presented):
            raise AssertionInvalid("assertion signature does not verify")
        expires = assertion.get("expires_at")
        try:
            expiry = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError as exc:
            raise AssertionInvalid("assertion expiry is not RFC3339") from exc
        if self._clock() >= expiry:
            raise AssertionInvalid("assertion expired")
        assertion_id = assertion.get("assertion_id")
        if not isinstance(assertion_id, str):
            raise AssertionInvalid("assertion lacks an id")
        with self._consumption_lock():
            store = self._load_consumed()
            if assertion_id in store["consumed"]:
                raise AssertionInvalid("assertion replay: one-use assertion already consumed")
            store["consumed"] = store["consumed"] + [assertion_id]
            store["updated_seq"] = int(store.get("updated_seq") or 0) + 1
            path = self._consumed_path()
            try:
                old = path.read_bytes() if path.exists() and not path.is_symlink() else None
                publish_mutable(
                    path,
                    json.dumps(store, sort_keys=True).encode() + b"\n",
                    expected_old_sha256=(
                        hashlib.sha256(old).hexdigest() if old is not None else ABSENT
                    ),
                )
            except PublicationError as exc:
                raise ActorBrokerError(f"could not persist assertion consumption: {exc}") from exc

    def check(self, assertion: dict[str, Any]) -> bool:
        """Non-consuming validity check (signature/binding/expiry only)."""
        try:
            if assertion.get("terminal_generation") != self._generation:
                return False
            if not self._generation_current():
                return False
            if not hmac.compare_digest(
                self._sign(assertion), str(assertion.get("signature") or "")
            ):
                return False
            expiry = datetime.fromisoformat(str(assertion.get("expires_at")).replace("Z", "+00:00"))
            return self._clock() < expiry
        except (ValueError, TypeError, AssertionInvalid):
            return False

    @staticmethod
    def assertion_digest(assertion: dict[str, Any]) -> str:
        """The digest a report envelope binds as actor provenance."""
        body = {field: assertion.get(field) for field in ASSERTION_FIELD_ORDER}
        body["signature"] = assertion.get("signature")
        return hashlib.sha256(encode_canonical(body)).hexdigest()


def platform_supported() -> bool:
    """Whether kernel peer credentials are implementable on this platform."""
    return sys.platform.startswith("linux") or sys.platform == "darwin"
