"""Image attachment staging and the attachments manifest (Lane C, §8.4).

Named "image attachments" throughout: ``services/native_attachment.py``
already owns the word "attachment" for provider-session ownership (§17 B8),
and one word meaning two things in the same server is how a reader deletes
the wrong record.

What lives here:

- **Staged files** at ``CAO_HOME_DIR/attachments/{terminal_id}/{attachment_id}.{ext}``
  — server-generated names only; the client-supplied filename is display
  metadata, never a path component.  Files are mode ``0600``, written
  temp-then-renamed like every other CAO store.
- **The manifest** ``CAO_HOME_DIR/attachments.json`` under exactly the D5
  discipline (``schema_version``, exclusive flock, ``mkstemp`` + fsync +
  ``os.replace`` at ``0600``, corrupt-file quarantine), mirroring
  ``services/macro_store.py``.
- **The typed state machine** ``staging → ready | failed``,
  ``ready → removed | submitted``.  Only ``ready`` attachments may be bound
  by a first submit; the ``ready → submitted(operation_id)`` transition
  happens under the manifest lock; an identical replay for the same
  ``operation_id`` reads the existing ``submitted`` binding, and a different
  operation referencing a ``submitted`` attachment is refused
  ``attachment-not-ready`` (the one binding rule pinned now — the SQLite/CAS
  ledger is §17 backlog, deferred per the owner speed guard).
- **Content validation**: magic-byte sniff plus structure/dimension decode
  (PNG signature + IHDR; GIF logical screen descriptor; JPEG SOF scan; WebP
  VP8/VP8L/VP8X headers).  The client filename and declared MIME type are
  never trusted — content decides the format.  Limits: ≤ 5 MiB and
  ≤ 8000×8000 px per image (the tightest documented downstream limit,
  design Appendix A.9), ≤ 4 images per operator message (§8.3).
- **The sweep** (§8.4 + §17 B7 — the mechanism, named): runs at server
  startup (the API lifespan) and opportunistically on every staging/binding
  mutation.  It deletes orphan files (crashed uploads), ``removed`` records,
  stale ``staging`` records (upload crashed between file and manifest),
  ``failed`` records past TTL, and ``submitted`` records past the pinned
  24 h retention (the provider must be able to read the path mid-turn).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, cast

SCHEMA_VERSION = 1

#: §8.3 pinned limits.  5 MiB / 8000×8000 match the tightest documented
#: downstream limit (Anthropic vision, design Appendix A.9); CAO pins its
#: own because no provider CLI documents one (F9).
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_WIDTH = 8000
MAX_IMAGE_HEIGHT = 8000
MAX_ATTACHMENTS_PER_MESSAGE = 4

#: The one format every advertised provider accepts (§8.4: PNG mandatory).
#: JPEG/GIF/WebP decode exists for providers with documented support
#: (claude_code, Appendix A.9); the per-provider allowlist is the registry's
#: ``image.formats`` block, enforced by the operator-message service.
_T = TypeVar("_T")

FORMAT_PNG = "png"
FORMAT_JPEG = "jpeg"
FORMAT_GIF = "gif"
FORMAT_WEBP = "webp"
KNOWN_FORMATS = frozenset({FORMAT_PNG, FORMAT_JPEG, FORMAT_GIF, FORMAT_WEBP})

#: File extensions are derived from the sniffed content format, never from
#: the client filename.
_FORMAT_EXTENSIONS = {
    FORMAT_PNG: ".png",
    FORMAT_JPEG: ".jpg",
    FORMAT_GIF: ".gif",
    FORMAT_WEBP: ".webp",
}

STATE_STAGING = "staging"
STATE_READY = "ready"
STATE_FAILED = "failed"
STATE_REMOVED = "removed"
STATE_SUBMITTED = "submitted"
ATTACHMENT_STATES = frozenset(
    {STATE_STAGING, STATE_READY, STATE_FAILED, STATE_REMOVED, STATE_SUBMITTED}
)

#: How long a submitted image stays readable for the provider mid-turn (§8.4).
SUBMITTED_TTL_SECONDS = 24 * 3600
#: Failed uploads leave no file; their records persist this long so a
#: reloaded dashboard can still show the operator what happened.
FAILED_TTL_SECONDS = 24 * 3600
#: A record still ``staging`` after this long belongs to a crashed upload.
STAGING_TTL_SECONDS = 3600

#: Refusal reason codes this module raises (bound to outcomes by the
#: operator-message service's vocabulary).
REASON_ATTACHMENT_TOO_LARGE = "attachment-too-large"
REASON_ATTACHMENT_TYPE_UNSUPPORTED = "attachment-type-unsupported"
REASON_ATTACHMENT_UNKNOWN = "attachment-unknown"
REASON_ATTACHMENT_NOT_READY = "attachment-not-ready"

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DISPLAY_FILENAME_MAX = 128
_SAFE_DISPLAY_CHARS = re.compile(r"[^\w.\- ()\[\]#]+", re.UNICODE)


def _utc_isots() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_isots(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class AttachmentStoreError(RuntimeError):
    """Base error for the image-attachment store."""


class AttachmentValidationError(AttachmentStoreError):
    """The uploaded bytes failed content validation; nothing was staged.

    Carries the typed refusal reason the operator message vocabulary
    reuses, so the API layer answers one honest shape everywhere.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


class AttachmentBindingError(AttachmentStoreError):
    """A submit referenced an attachment it may not use (zero bytes)."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail


class AttachmentNotFoundError(AttachmentStoreError):
    """No record for this id on this terminal (or none at all)."""


# --- Paths (resolved at call time so isolated state roots work) -------------


def attachments_root() -> Path:
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    return Path(CAO_HOME_DIR) / "attachments"


def manifest_path() -> Path:
    from cli_agent_orchestrator.constants import CAO_HOME_DIR

    return Path(CAO_HOME_DIR) / "attachments.json"


def _lock_path() -> Path:
    return manifest_path().parent / f"{manifest_path().name}.lock"


def staged_file_path(terminal_id: str, attachment_id: str, image_format: str) -> Path:
    return attachments_root() / terminal_id / f"{attachment_id}{_FORMAT_EXTENSIONS[image_format]}"


# --- Content validation ------------------------------------------------------


def _decode_png(data: bytes) -> Tuple[int, int]:
    if len(data) < 33 or not data.startswith(_PNG_SIGNATURE):
        raise ValueError("not a PNG: bad or truncated signature")
    ihdr_len = int.from_bytes(data[8:12], "big")
    if data[12:16] != b"IHDR" or ihdr_len != 13:
        raise ValueError("not a PNG: the first chunk is not a well-formed IHDR")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    return width, height


def _decode_gif(data: bytes) -> Tuple[int, int]:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        raise ValueError("not a GIF: bad or truncated header")
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    return width, height


_JPEG_STANDALONE_MARKERS = frozenset({0x01, 0xD8, 0xD9, *range(0xD0, 0xD8)})
_JPEG_SOF_MARKERS = frozenset(
    marker for marker in range(0xC0, 0xD0) if marker not in (0xC4, 0xC8, 0xCC)
)


def _decode_jpeg(data: bytes) -> Tuple[int, int]:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG: missing SOI magic")
    offset = 2
    # A SOF marker lives in the headers before any scan data; bound the
    # walk so a crafted length field cannot make validation unbounded.
    while offset + 4 <= len(data) and offset < 256 * 1024:
        if data[offset] != 0xFF:
            offset += 1
            continue
        marker = data[offset + 1]
        if marker == 0xFF:  # fill byte
            offset += 1
            continue
        if marker in _JPEG_STANDALONE_MARKERS:
            offset += 2
            continue
        if offset + 4 > len(data):
            break
        segment_length = int.from_bytes(data[offset + 2 : offset + 4], "big")
        if segment_length < 2:
            raise ValueError("not a JPEG: a header segment has an impossible length")
        if marker in _JPEG_SOF_MARKERS:
            if offset + 9 > len(data):
                raise ValueError("not a JPEG: truncated SOF segment")
            height = int.from_bytes(data[offset + 5 : offset + 7], "big")
            width = int.from_bytes(data[offset + 7 : offset + 9], "big")
            return width, height
        offset += 2 + segment_length
    raise ValueError("not a JPEG: no start-of-frame header found")


def _decode_webp(data: bytes) -> Tuple[int, int]:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise ValueError("not a WebP: bad RIFF/WEBP header")
    fourcc = data[12:16]
    if fourcc == b"VP8X":
        if len(data) < 30:
            raise ValueError("not a WebP: truncated VP8X chunk")
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if fourcc == b"VP8L":
        if len(data) < 25 or data[20] != 0x2F:
            raise ValueError("not a WebP: bad VP8L signature")
        b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
        width = 1 + (((b1 & 0x3F) << 8) | b0)
        height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
        return width, height
    if fourcc == b"VP8 ":
        if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
            raise ValueError("not a WebP: bad lossy frame header")
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    raise ValueError(f"not a WebP: unknown first chunk {fourcc!r}")


def sniff_image(data: bytes) -> Tuple[str, int, int]:
    """The content's own answer to "what are you and how big".

    Returns ``(format, width, height)``.  Raises ``ValueError`` when the
    bytes match no known format or the structure does not decode — the
    caller maps that to ``attachment-type-unsupported``; a file whose name
    or MIME claims one type while its bytes say another is decided by the
    bytes, which is the entire point of sniffing.
    """
    if data.startswith(_PNG_SIGNATURE):
        width, height = _decode_png(data)
        return FORMAT_PNG, width, height
    if data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = _decode_gif(data)
        return FORMAT_GIF, width, height
    if data[:2] == b"\xff\xd8":
        width, height = _decode_jpeg(data)
        return FORMAT_JPEG, width, height
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        width, height = _decode_webp(data)
        return FORMAT_WEBP, width, height
    raise ValueError("bytes match no known image format (PNG, JPEG, GIF, WebP)")


def validate_image(data: bytes) -> Tuple[str, int, int]:
    """Content validation with the typed refusal reasons attached."""
    if not data:
        raise AttachmentValidationError(
            REASON_ATTACHMENT_TYPE_UNSUPPORTED, "the upload carries no bytes"
        )
    if len(data) > MAX_IMAGE_BYTES:
        raise AttachmentValidationError(
            REASON_ATTACHMENT_TOO_LARGE,
            f"image is {len(data)} bytes, over the {MAX_IMAGE_BYTES}-byte limit",
        )
    try:
        image_format, width, height = sniff_image(data)
    except ValueError as exc:
        raise AttachmentValidationError(
            REASON_ATTACHMENT_TYPE_UNSUPPORTED,
            f"the uploaded content is not a valid PNG, JPEG, GIF, or WebP image: {exc}",
        ) from exc
    if width < 1 or height < 1 or width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
        raise AttachmentValidationError(
            REASON_ATTACHMENT_TOO_LARGE,
            f"image is {width}×{height}px, outside the 1×1–"
            f"{MAX_IMAGE_WIDTH}×{MAX_IMAGE_HEIGHT}px limit",
        )
    return image_format, width, height


def sanitize_display_filename(filename: Optional[str]) -> str:
    """Display metadata, never a path component.

    Basename-only, control characters and shell/hostile punctuation
    collapsed, length-capped.  The staged file name is the server-minted
    attachment id; this string only ever renders in the dashboard.
    """
    if not filename:
        return "image"
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if ord(char) >= 0x20 and ord(char) != 0x7F)
    name = _SAFE_DISPLAY_CHARS.sub("_", name).strip(". ")
    if not name:
        return "image"
    return name[:_DISPLAY_FILENAME_MAX]


# --- Manifest discipline (D5, mirroring macro_store) --------------------------


def _empty_document() -> Dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "attachments": []}


def _quarantine_whole_file(reason: str) -> None:
    """Move an unreadable manifest aside; never silently drop it."""
    import logging

    path = manifest_path()
    if not path.exists():
        return
    stamp = _utc_isots().replace(":", "-")
    quarantine = path.parent / f"attachments.quarantine-{stamp}.json"
    os.replace(str(path), str(quarantine))
    logging.getLogger(__name__).warning(
        "attachments manifest quarantined to %s: %s", quarantine, reason
    )


def _validate_record(record: Any) -> Dict[str, Any]:
    if not isinstance(record, dict):
        raise ValueError("attachment record must be an object")
    for field in ("attachment_id", "terminal_id", "state"):
        if not isinstance(record.get(field), str) or not record[field]:
            raise ValueError(f"attachment record requires a non-empty string {field!r}")
    if record["state"] not in ATTACHMENT_STATES:
        raise ValueError(f"unknown attachment state {record['state']!r}")
    return record


def _load_document() -> Dict[str, Any]:
    path = manifest_path()
    if not path.exists():
        return _empty_document()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("attachments"), list):
            raise ValueError("manifest top level must be an object with an 'attachments' list")
        version = document.get("schema_version")
        if not isinstance(version, int):
            raise ValueError("manifest schema_version must be an integer")
        if version > SCHEMA_VERSION:
            raise ValueError(
                f"manifest schema_version {version} is newer than this server ({SCHEMA_VERSION})"
            )
        document["attachments"] = [_validate_record(record) for record in document["attachments"]]
        return document
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        _quarantine_whole_file(f"unreadable manifest: {exc}")
        return _empty_document()


def _atomic_write(path: Path, document: Dict[str, Any]) -> None:
    """mkstemp + os.replace at mode 0600 (wake_receipts precedent)."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".attachments-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(path))
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def _mutate(mutator: Callable[[Dict[str, Any]], _T]) -> _T:
    """Run one read-modify-write under the exclusive flock (§5.1 discipline)."""
    _lock_path().parent.mkdir(parents=True, exist_ok=True)
    with open(_lock_path(), "a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            document = _load_document()
            result = mutator(document)
            _atomic_write(manifest_path(), document)
            return result
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _find_record(
    document: Dict[str, Any], terminal_id: str, attachment_id: str
) -> Optional[Dict[str, Any]]:
    for record in document["attachments"]:
        if record["attachment_id"] == attachment_id and record["terminal_id"] == terminal_id:
            return cast(Dict[str, Any], record)
    return None


# --- Staging lifecycle ---------------------------------------------------------


def stage_upload(
    terminal_id: str,
    *,
    display_filename: Optional[str],
    content: bytes,
    allowed_formats: frozenset,
) -> Dict[str, Any]:
    """Validate, stage, and manifest one uploaded image.

    ``allowed_formats`` is the provider's advertised ``image.formats`` —
    content decides the format, the registry decides whether this provider
    may receive it (PNG-only kimi refusing a JPEG is an honest
    ``attachment-type-unsupported``, never a silent conversion).

    On validation failure a durable ``failed`` record is written (no file)
    so a reloaded dashboard can still show the operator what happened, and
    the :class:`AttachmentValidationError` propagates for the typed reply.
    """
    sweep_attachments()
    attachment_id = uuid.uuid4().hex
    now = _utc_isots()
    base_record: Dict[str, Any] = {
        "attachment_id": attachment_id,
        "terminal_id": terminal_id,
        "state": STATE_STAGING,
        "format": None,
        "content_type": None,
        "width": None,
        "height": None,
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "display_filename": sanitize_display_filename(display_filename),
        "staged_path": None,
        "bound_operation_id": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        image_format, width, height = validate_image(content)
    except AttachmentValidationError as exc:
        failed = {
            **base_record,
            "state": STATE_FAILED,
            "error": {"reason_code": exc.reason_code, "detail": exc.detail},
        }

        def record_failure(document: Dict[str, Any]) -> Dict[str, Any]:
            document["attachments"].append(failed)
            return failed

        _mutate(record_failure)
        # The durable failed record travels with the refusal so the API
        # layer can hand it back for the dashboard's failure chip.
        exc.record = failed  # type: ignore[attr-defined]
        raise

    if image_format not in allowed_formats:
        detail = (
            f"content is {image_format}, which this provider does not advertise "
            f"(allowed: {sorted(allowed_formats)}); unproven formats are refused "
            "rather than converted"
        )
        failed = {
            **base_record,
            "state": STATE_FAILED,
            "format": image_format,
            "width": width,
            "height": height,
            "error": {
                "reason_code": REASON_ATTACHMENT_TYPE_UNSUPPORTED,
                "detail": detail,
            },
        }

        def record_format_failure(document: Dict[str, Any]) -> Dict[str, Any]:
            document["attachments"].append(failed)
            return failed

        _mutate(record_format_failure)
        refusal = AttachmentValidationError(REASON_ATTACHMENT_TYPE_UNSUPPORTED, detail)
        refusal.record = failed  # type: ignore[attr-defined]
        raise refusal

    # Content is valid and admitted.  Stage the file first under a temp
    # name, then transition the manifest record staging → ready under the
    # lock; a crash anywhere in between leaves either a swept orphan file
    # or a swept stale staging record — never a ready record without bytes.
    target = staged_file_path(terminal_id, attachment_id, image_format)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{attachment_id}-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        record = {
            **base_record,
            "format": image_format,
            "content_type": f"image/{image_format}",
            "width": width,
            "height": height,
            "staged_path": str(target.relative_to(attachments_root().parent)),
        }

        def record_staging(document: Dict[str, Any]) -> Dict[str, Any]:
            document["attachments"].append(record)
            return record

        _mutate(record_staging)
        os.replace(tmp, str(target))
        tmp = ""

        def mark_ready(document: Dict[str, Any]) -> Dict[str, Any]:
            stored = _find_record(document, terminal_id, attachment_id)
            if stored is None:
                raise AttachmentStoreError(
                    f"staged record {attachment_id} vanished before it was ready"
                )
            stored["state"] = STATE_READY
            stored["updated_at"] = _utc_isots()
            return dict(stored)

        return _mutate(mark_ready)
    finally:
        if tmp:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)


def list_attachments(terminal_id: str) -> List[Dict[str, Any]]:
    """Live records for one terminal (``removed`` records are gone)."""
    document = _load_document()
    return [
        dict(record)
        for record in document["attachments"]
        if record["terminal_id"] == terminal_id and record["state"] != STATE_REMOVED
    ]


def get_attachment(terminal_id: str, attachment_id: str) -> Dict[str, Any]:
    record = _find_record(_load_document(), terminal_id, attachment_id)
    if record is None:
        raise AttachmentNotFoundError(
            f"no image attachment {attachment_id!r} for terminal {terminal_id!r}"
        )
    return dict(record)


def remove_attachment(terminal_id: str, attachment_id: str) -> Dict[str, Any]:
    """``ready``/``staging``/``failed`` → ``removed``; the file is deleted.

    ``submitted`` records are read-only for their retention TTL (§8.4): the
    provider may still be reading the staged path mid-turn, so removing one
    is a conflict the caller must hear about, not a delete to honor quietly.
    """
    existing = get_attachment(terminal_id, attachment_id)
    if existing["state"] == STATE_SUBMITTED:
        raise AttachmentBindingError(
            REASON_ATTACHMENT_NOT_READY,
            f"image attachment {attachment_id} is submitted to operation "
            f"{existing.get('bound_operation_id')}; submitted images are retained "
            f"read-only for {SUBMITTED_TTL_SECONDS // 3600}h so the provider can "
            "still read the staged path mid-turn",
        )
    if existing["state"] == STATE_REMOVED:
        return existing

    def mark_removed(document: Dict[str, Any]) -> Dict[str, Any]:
        stored = _find_record(document, terminal_id, attachment_id)
        if stored is None:
            raise AttachmentNotFoundError(
                f"no image attachment {attachment_id!r} for terminal {terminal_id!r}"
            )
        stored["state"] = STATE_REMOVED
        stored["updated_at"] = _utc_isots()
        return dict(stored)

    record = _mutate(mark_removed)
    _delete_staged_file(record)
    return record


def bind_for_submit(
    terminal_id: str, operation_id: str, attachment_ids: List[str]
) -> List[Dict[str, Any]]:
    """The pinned ``ready → submitted(operation_id)`` binding, under the lock.

    Every referenced attachment must be ``ready`` — or already ``submitted``
    to *this same* operation, which is the identical-replay case and simply
    reads the existing binding back.  A different operation referencing a
    ``submitted`` image is refused ``attachment-not-ready``; an unknown id
    is ``attachment-unknown``.  The check and the transition are one locked
    mutation, so two submits can never bind the same ready image.
    """
    if not attachment_ids:
        return []
    sweep_attachments()

    def bind(document: Dict[str, Any]) -> List[Dict[str, Any]]:
        bound: List[Dict[str, Any]] = []
        for attachment_id in attachment_ids:
            record = None
            for candidate in document["attachments"]:
                if candidate["attachment_id"] == attachment_id:
                    record = candidate
                    break
            if record is None or record["terminal_id"] != terminal_id:
                raise AttachmentBindingError(
                    REASON_ATTACHMENT_UNKNOWN,
                    f"no image attachment {attachment_id!r} is staged for terminal "
                    f"{terminal_id!r}; nothing was submitted",
                )
            if record["state"] == STATE_SUBMITTED:
                if record.get("bound_operation_id") == operation_id:
                    bound.append(dict(record))
                    continue
                raise AttachmentBindingError(
                    REASON_ATTACHMENT_NOT_READY,
                    f"image attachment {attachment_id} is already submitted to "
                    f"operation {record.get('bound_operation_id')}; a different "
                    "operation may not reference it",
                )
            if record["state"] != STATE_READY:
                raise AttachmentBindingError(
                    REASON_ATTACHMENT_NOT_READY,
                    f"image attachment {attachment_id} is {record['state']}, not "
                    "ready; only a validated, fully staged image may be submitted",
                )
            record["state"] = STATE_SUBMITTED
            record["bound_operation_id"] = operation_id
            record["updated_at"] = _utc_isots()
            bound.append(dict(record))
        return bound

    return _mutate(bind)


def staged_absolute_path(record: Dict[str, Any]) -> Path:
    """The absolute staged path a provider reference names.

    The manifest stores the path relative to ``CAO_HOME_DIR`` so a relocated
    state root does not strand records; the provider reference is always the
    absolute form of the same file.
    """
    staged = record.get("staged_path")
    if not staged:
        raise AttachmentBindingError(
            REASON_ATTACHMENT_NOT_READY,
            f"image attachment {record.get('attachment_id')} has no staged file "
            "(it never finished staging); it cannot be referenced",
        )
    return attachments_root().parent / str(staged)


def _delete_staged_file(record: Dict[str, Any]) -> None:
    staged = record.get("staged_path")
    if not staged:
        return
    with contextlib.suppress(FileNotFoundError):
        os.unlink(staged_absolute_path(record))


# --- Sweep (startup + opportunistic; §17 B7 names the mechanism) -------------


def _sweep_cutoffs(now: datetime) -> Dict[str, float]:
    epoch = now.timestamp()
    return {
        STATE_SUBMITTED: epoch - SUBMITTED_TTL_SECONDS,
        STATE_FAILED: epoch - FAILED_TTL_SECONDS,
        STATE_STAGING: epoch - STAGING_TTL_SECONDS,
    }


def sweep_attachments(*, now: Optional[datetime] = None) -> Dict[str, int]:
    """Delete orphans, removals, and expired records; report the counts.

    Runs at server startup (the API lifespan) and at the top of every
    staging/binding mutation.  Never touches ``ready`` records: an
    unsubmitted image is the operator's draft content, not garbage.
    """
    now = now or datetime.now(timezone.utc)
    cutoffs = _sweep_cutoffs(now)
    counts = {"records_purged": 0, "files_deleted": 0, "orphans_deleted": 0}

    expired_states = {STATE_SUBMITTED, STATE_FAILED, STATE_STAGING}

    def purge(document: Dict[str, Any]) -> List[Dict[str, Any]]:
        kept: List[Dict[str, Any]] = []
        expired: List[Dict[str, Any]] = []
        for record in document["attachments"]:
            state = record["state"]
            if state == STATE_REMOVED:
                expired.append(record)
                continue
            if state in expired_states:
                try:
                    updated = _parse_isots(record["updated_at"]).timestamp()
                except (ValueError, KeyError):
                    updated = 0.0
                if updated < cutoffs[state]:
                    expired.append(record)
                    continue
            kept.append(record)
        document["attachments"] = kept
        return expired

    root = attachments_root()
    if not root.exists() and not manifest_path().exists():
        return counts
    expired_records = _mutate(purge)
    for record in expired_records:
        counts["records_purged"] += 1
        if record.get("staged_path"):
            try:
                os.unlink(staged_absolute_path(record))
                counts["files_deleted"] += 1
            except FileNotFoundError:
                pass

    # Orphan files: anything under the staging tree the manifest no longer
    # names — crashed uploads (temp ``.part`` files) and files whose record
    # was just purged above.
    if root.exists():
        live_paths = set()
        for record in _load_document()["attachments"]:
            staged = record.get("staged_path")
            if staged:
                live_paths.add(str(staged_absolute_path(record)))
        for directory, _subdirs, files in os.walk(root):
            for name in files:
                path = os.path.join(directory, name)
                if path not in live_paths:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)
                        counts["orphans_deleted"] += 1
    return counts
