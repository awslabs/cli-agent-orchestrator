"""Durable-publication primitives P-IMM and P-MUT.

Two distinct primitives exist because one no-replace primitive cannot
serve both immutable content-addressed artifacts and fixed-path mutable
state (a heartbeat's second write would fail ``EEXIST``).  Every durable
recovery artifact is assigned to exactly one primitive.

P-IMM — immutable content-addressed no-replace publication:
same-dir ``O_CREAT|O_EXCL|O_NOFOLLOW`` temporary; write; re-read and
verify digest; ``fchmod`` 0400 before ``fsync(fd)``; no-replace
publication via ``link(2)`` + ``unlink(2)``; content-addressed
equal-reuse and different-bytes refusal; parent-dir ``fsync`` before any
externally visible acceptance.

P-MUT — mutable fenced CAS replacement at a fixed path:
under the caller's owning lock, read the current file and verify
``expected_old_sha256`` (or explicit-absent on first write) and the
writer's fence authority; same-dir exclusive temporary; write; re-read
and byte-verify; ``fchmod`` 0600; ``fsync(fd)``; atomic ``rename(2)``
over the fixed path; parent-dir ``fsync``.  Wrong old digest, a stale
fence, or a concurrent winner refuses with zero mutation.  New bytes
carry a strictly increasing sequence field so replayed old bytes are
detectable.

Invariant: a kill or power loss at any syscall boundary leaves the
visible path holding either the complete old bytes or the complete new
bytes — never a torn or absent record where one was durable.

Failure mode prevented: write→rename→chmod publication exposes
world-readable or partially written records and silently replaces
immutable evidence; an unlocked read-modify-replace lets a superseded
writer clobber a newer record.

Why this guard exists: recovery evidence (receipts, heartbeats, fences)
is only as trustworthy as its publication boundary, and every downstream
digest check assumes these exact durability semantics.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Any, Callable, Optional

# Explicit-absent sentinel for the first P-MUT write at a fixed path.
ABSENT = "absent"


class PublicationError(RuntimeError):
    """Base error for durable publication failures."""


class PublicationConflict(PublicationError):
    """CAS identity check failed (wrong old digest, stale fence, race)."""


class PublicationRefused(PublicationError):
    """The target is unsafe to publish to (e.g. a symlink substitution)."""


# Test-only crash-injection seam.  Production code never sets this; tests
# assign a callable that receives a step name and may raise, simulating a
# kill at that exact syscall boundary.  The step names are part of the
# crash-window test contract.
crash_hook: Optional[Callable[[str], None]] = None


def _step(name: str) -> None:
    if crash_hook is not None:
        crash_hook(name)


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _refuse_if_symlink(path: Path) -> None:
    """Refuse symlink substitution at any publication target.

    A symlink at the fixed path would redirect the write outside the
    owning directory, defeating both the content addressing and the
    permission guarantees; ``lstat`` (never ``stat``) is the check.
    """
    if path.is_symlink():
        raise PublicationRefused(f"refusing to publish through a symlink: {path}")


def _open_exclusive_temp(directory: Path, prefix: str) -> tuple[int, Path]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for _ in range(32):
        candidate = directory / f".{prefix}-{secrets.token_hex(8)}.part"
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        return fd, candidate
    raise PublicationError(f"could not allocate an exclusive temporary in {directory}")


def _sweep_stray_temps(directory: Path, prefix: str) -> None:
    """Remove leftover temporaries from an earlier kill.

    A temp that was never linked/renamed is by construction invisible to
    readers, so removing it cannot destroy durable state; leaving it
    would eventually exhaust temp-name entropy and clutter evidence
    directories.
    """
    for entry in directory.glob(f".{prefix}-*.part"):
        try:
            if not entry.is_symlink():
                entry.unlink()
        except FileNotFoundError:
            pass


def publish_immutable(
    directory: Path,
    name_for_digest: Callable[[str], str],
    data: bytes,
) -> Path:
    """Publish ``data`` immutably, content-addressed, in ``directory``.

    ``name_for_digest`` maps the full hex digest to the final file name.
    Equal bytes at the content address are reused idempotently; different
    bytes at the same address are a collision/tamper and are refused.
    Returns the final path.
    """
    if not data:
        raise PublicationError("refusing to publish an empty immutable artifact")
    directory.mkdir(parents=True, exist_ok=True)
    _step("pimm.begin")
    digest = hashlib.sha256(data).hexdigest()
    final = directory / name_for_digest(digest)
    _refuse_if_symlink(final)
    if final.exists():
        existing = final.read_bytes()
        if existing == data:
            return final  # content-addressed equal-reuse
        raise PublicationConflict(
            f"content address {digest} is already bound to different bytes: {final}"
        )

    _sweep_stray_temps(directory, "pimm")
    fd, temp = _open_exclusive_temp(directory, "pimm")
    try:
        _step("pimm.temp-opened")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            _step("pimm.written")
        # Re-read and verify before publication: the bytes accepted for
        # addressing are exactly the bytes on disk.
        reread = temp.read_bytes()
        if reread != data or hashlib.sha256(reread).hexdigest() != digest:
            raise PublicationError("immutable temporary failed its re-read digest verification")
        os.chmod(temp, 0o400)
        _step("pimm.chmod")
        fd2 = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(fd2)
        finally:
            os.close(fd2)
        _step("pimm.fsync")
        try:
            os.link(temp, final)
        except FileExistsError:
            # A concurrent publisher won.  Re-check equal-reuse vs conflict.
            existing = final.read_bytes()
            if existing == data:
                return final
            raise PublicationConflict(f"content address {digest} raced to different bytes: {final}")
        _step("pimm.linked")
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    _fsync_dir(directory)
    _step("pimm.dir-fsynced")
    return final


def _read_current(path: Path) -> Optional[bytes]:
    _refuse_if_symlink(path)
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def publish_mutable(
    path: Path,
    data: bytes,
    *,
    expected_old_sha256: Optional[str],
    fence_check: Optional[Callable[[Optional[dict[str, Any]]], None]] = None,
    seq_field: Optional[str] = "updated_seq",
) -> None:
    """Replace the fixed path ``path`` with ``data`` under a fenced CAS.

    The caller must hold the owning domain lock for the whole call.
    ``expected_old_sha256`` is the hex digest of the current bytes, or the
    ``ABSENT`` sentinel for a first write.  ``fence_check`` receives the
    parsed current record (or ``None``) and must raise to refuse a
    superseded writer.  When ``seq_field`` is set and both the old and new
    records carry it, the new value must be strictly greater, so replayed
    old bytes are detectable as regressions.
    """
    if not data:
        raise PublicationError("refusing to publish an empty mutable record")
    path.parent.mkdir(parents=True, exist_ok=True)
    _step("pmut.begin")
    _sweep_stray_temps(path.parent, "pmut")

    current = _read_current(path)
    current_record: Optional[dict[str, Any]] = None
    if current is None:
        if expected_old_sha256 != ABSENT:
            raise PublicationConflict(f"expected existing bytes at {path} but the path is absent")
    else:
        if expected_old_sha256 == ABSENT:
            raise PublicationConflict(f"expected an absent path but bytes exist: {path}")
        actual = hashlib.sha256(current).hexdigest()
        if actual != expected_old_sha256:
            raise PublicationConflict(f"current bytes at {path} do not match expected_old_sha256")
        try:
            parsed = json.loads(current)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationConflict(f"current record at {path} is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise PublicationConflict(f"current record at {path} is not a JSON object")
        current_record = parsed

    _step("pmut.verified-old")
    if fence_check is not None:
        # A superseded writer (e.g. a stale fencing token) is refused here
        # with zero mutation, before any new bytes touch the directory.
        fence_check(current_record)
    _step("pmut.fence-ok")

    if seq_field is not None and current_record is not None:
        try:
            new_record = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublicationError("new mutable record is not valid JSON") from exc
        old_seq = current_record.get(seq_field)
        new_seq = new_record.get(seq_field) if isinstance(new_record, dict) else None
        if isinstance(old_seq, int) and isinstance(new_seq, int) and new_seq <= old_seq:
            raise PublicationConflict(
                f"{seq_field} must strictly increase ({old_seq} -> {new_seq}); "
                "replayed old bytes are refused"
            )

    fd, temp = _open_exclusive_temp(path.parent, "pmut")
    try:
        _step("pmut.temp-opened")
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            _step("pmut.written")
        if temp.read_bytes() != data:
            raise PublicationError("mutable temporary failed its re-read verification")
        os.chmod(temp, 0o600)
        _step("pmut.chmod")
        fd2 = os.open(temp, os.O_RDONLY)
        try:
            os.fsync(fd2)
        finally:
            os.close(fd2)
        _step("pmut.fsync")
        os.replace(temp, path)
        _step("pmut.renamed")
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass
    _fsync_dir(path.parent)
    _step("pmut.dir-fsynced")
