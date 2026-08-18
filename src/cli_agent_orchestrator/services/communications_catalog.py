"""Bounded reader for the conductor's communications catalog projection.

The conductor publishes a versioned per-project index plus immutable content
objects under a fixed state root.  This module reads those files, never the
conductor database, and enforces the same confinement posture as the annotation
reader:

* no caller-supplied path, root, or project_dir;
* bounded project fan-out and record counts;
* identifier validation;
* symlink and non-regular-file refusal;
* digest verification before any bytes are served;
* typed coverage and bounded reason codes that never echo paths or content.
"""

from __future__ import annotations

import base64
import errno
import hashlib
import json
import logging
import os
import re
import stat
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The wire schema of THIS route's response envelope.  The publisher's own
#: ``schema`` field is ``cao-communications-index-v1``; the fork's response
#: reuses that identifier because the index is the contract it serves.
RESPONSE_SCHEMA = "cao-communications-index-v1"

#: The document the publisher writes, one per conductor project state directory.
COMMUNICATIONS_INDEX_FILENAME = "communications.json"

#: Content objects live under ``<project-state-dir>/communications/content/<blob-id>``.
CONTENT_OBJECTS_DIR = "communications" + os.sep + "content"

# ── bounds ─────────────────────────────────────────────────────────────────

#: Most project directories scanned in one request.  Mirrors the annotation
#: reader's ``MAX_SOURCES``.
MAX_PROJECTS = 64

#: Communications returned in one list page.  Smaller than the publisher's
#: ``INDEX_LIMIT`` so the API paginates, and small enough to keep metadata
#: responses bounded.
PAGE_SIZE = 50

#: Largest index file read from any one project.  The publisher caps at
#: ``INDEX_LIMIT`` communications, so this is a safety rail, not a tuning knob.
MAX_INDEX_BYTES = 8 * 1024 * 1024

#: Largest content object served.  Matches the conductor's per-document budget.
MAX_CONTENT_BYTES = 1 * 1024 * 1024

#: Ceiling on opaque identifiers passed by callers.
MAX_ID_LEN = 128

# ── coverage / reason vocabulary ────────────────────────────────────────────

COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_TRUNCATED = "truncated"
COVERAGE_UNAVAILABLE = "unavailable"

REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_MALFORMED = "malformed"
REASON_OVERSIZE = "oversize"
REASON_NOT_REGULAR = "not-a-regular-file"
REASON_SYMLINK_REFUSED = "symlink-refused"
REASON_OUTSIDE_ROOT = "outside-root"
REASON_PROJECT_LIMIT = "project-limit"
REASON_IDENTIFIER_INVALID = "identifier-invalid"
REASON_CONTENT_DIGEST_MISMATCH = "content-digest-mismatch"
REASON_CONTENT_MISSING = "content-missing"
REASON_CONTENT_QUARANTINED = "content-quarantined"
REASON_CONTENT_UNREADABLE = "content-unreadable"

ROOT_SOURCE_LABEL = "conductor-state-root"

# ── validation ──────────────────────────────────────────────────────────────

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class CommunicationsCatalogError(RuntimeError):
    code = "communications-catalog-error"


class CommunicationsCatalogInvalid(CommunicationsCatalogError):
    code = "communications-catalog-invalid"


class CommunicationsCatalogNotFound(CommunicationsCatalogError):
    code = "communications-catalog-not-found"


class CommunicationsCatalogUnavailable(CommunicationsCatalogError):
    code = "communications-catalog-unavailable"


def catalog_root() -> str:
    """The fixed, non-configurable, conductor-owned catalog root.

    No parameter, no request state, and no CAO or conductor configuration
    variable.  ``CAO_STATE_ROOT`` deliberately does not move it: that knob
    relocates *CAO's* state, and this directory belongs to the conductor.
    """
    return os.path.expanduser("~/.local/state/cao-conductor")


def _require_identifier(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise CommunicationsCatalogInvalid(f"{field} must be a non-empty string")
    if len(value) > MAX_ID_LEN:
        raise CommunicationsCatalogInvalid(f"{field} must be at most {MAX_ID_LEN} characters")
    if _ID_RE.fullmatch(value) is None:
        raise CommunicationsCatalogInvalid(f"{field} is not a well-formed identifier")
    return value


def _safe_source_name(name: str) -> str:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    return "".join(ch for ch in name if ch in allowed)[:64] or "unnamed"


def _normalize_recorded_at(value: str) -> str:
    """The publisher's total order normalises whole-second legacy stamps."""
    text = value.strip() if isinstance(value, str) else ""
    if text.endswith("Z"):
        body = text[:-1]
        if "." not in body:
            body = body + ".000000"
        return body + "Z"
    return text


# ── cursor ──────────────────────────────────────────────────────────────────


def _cursor_encode(recorded_at: str, communication_id: str) -> str:
    payload = json.dumps(
        {"r": _normalize_recorded_at(recorded_at), "c": communication_id},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8").rstrip("=")


def _cursor_decode(cursor: str) -> Tuple[str, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(cursor + padding)
        data = json.loads(payload)
        if not isinstance(data, dict) or "r" not in data or "c" not in data:
            raise ValueError
        return str(data["r"]), str(data["c"])
    except Exception:
        raise CommunicationsCatalogInvalid("cursor is not a valid opaque cursor") from None


# ── filesystem confinement ──────────────────────────────────────────────────


def _open_nested(base_dir: str, rel_parts: Tuple[str, ...]) -> Tuple[Optional[int], Optional[str]]:
    """Open a file under ``base_dir`` without following symlinks at any component.

    Returns ``(fd, reason)``.  ``O_NONBLOCK`` is load-bearing for the same
    reason as in ``services/annotations.py``: a FIFO in the content directory
    would otherwise park the shared executor worker forever.
    """
    dir_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        dfd = os.open(base_dir, dir_flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, REASON_MISSING
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return None, REASON_SYMLINK_REFUSED
        if exc.errno == errno.ENOTDIR:
            return None, REASON_NOT_REGULAR
        return None, REASON_UNREADABLE

    try:
        for part in rel_parts[:-1]:
            try:
                ndfd = os.open(part, dir_flags, dir_fd=dfd)
            except OSError as exc:
                if exc.errno == errno.ENOENT:
                    return None, REASON_MISSING
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    return None, REASON_SYMLINK_REFUSED
                if exc.errno == errno.ENOTDIR:
                    return None, REASON_NOT_REGULAR
                return None, REASON_UNREADABLE
            os.close(dfd)
            dfd = ndfd

        file_flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            fd = os.open(rel_parts[-1], file_flags, dir_fd=dfd)
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                return None, REASON_MISSING
            if exc.errno in (errno.ELOOP, errno.EMLINK):
                return None, REASON_SYMLINK_REFUSED
            if exc.errno in (errno.ENXIO, errno.ENODEV):
                return None, REASON_NOT_REGULAR
            return None, REASON_UNREADABLE
        finally:
            os.close(dfd)
        return fd, None
    except Exception:
        try:
            os.close(dfd)
        except OSError:
            pass
        raise


def _read_index_bytes(directory: str, root: str) -> Tuple[Optional[bytes], Optional[str]]:
    """Read one project's index file.  ``(payload, reason)``."""
    path = os.path.join(directory, COMMUNICATIONS_INDEX_FILENAME)
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None, REASON_UNREADABLE
    confined_root = root.rstrip(os.sep) + os.sep
    if not resolved.startswith(confined_root):
        return None, REASON_OUTSIDE_ROOT

    fd, reason = _open_nested(directory, (COMMUNICATIONS_INDEX_FILENAME,))
    if fd is None:
        return None, reason
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, REASON_NOT_REGULAR
        if info.st_size > MAX_INDEX_BYTES:
            return None, REASON_OVERSIZE
        if hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        chunks: List[bytes] = []
        total = 0
        while total <= MAX_INDEX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
    except OSError:
        return None, REASON_UNREADABLE
    finally:
        os.close(fd)
    if len(payload) > MAX_INDEX_BYTES:
        return None, REASON_OVERSIZE
    return payload, None


def _project_dirs(
    root: str,
) -> Tuple[List[Tuple[str, str]], Optional[str], bool, List[Dict[str, str]]]:
    """Child directories of the root.  Symlinked children are refused."""
    try:
        with os.scandir(root) as scan:
            entries = sorted(scan, key=lambda e: e.name)
    except FileNotFoundError:
        return [], REASON_MISSING, False, []
    except OSError:
        return [], REASON_UNREADABLE, False, []

    out: List[Tuple[str, str]] = []
    refusals: List[Dict[str, str]] = []
    truncated = False
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_symlink():
                if entry.is_dir(follow_symlinks=True):
                    refusals.append(
                        {"source": _safe_source_name(entry.name), "reason": REASON_SYMLINK_REFUSED}
                    )
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            refusals.append({"source": _safe_source_name(entry.name), "reason": REASON_UNREADABLE})
            continue
        if len(out) >= MAX_PROJECTS:
            truncated = True
            break
        out.append((entry.name, entry.path))
    return out, None, truncated, refusals


# ── index parsing ───────────────────────────────────────────────────────────


def _read_project_index(
    directory: str,
    root: str,
    reasons: List[Dict[str, str]],
    label: str,
) -> Optional[Tuple[List[Any], Dict[str, Any], bool, int]]:
    """``(communications, content_objects, truncated, bytes_read)`` for one project."""
    payload, reason = _read_index_bytes(directory, root)
    if reason == REASON_MISSING:
        return None
    if payload is None:
        reasons.append({"source": label, "reason": reason or REASON_UNREADABLE})
        return None
    try:
        document = json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001
        reasons.append({"source": label, "reason": REASON_MALFORMED})
        return None
    if not isinstance(document, dict):
        reasons.append({"source": label, "reason": REASON_MALFORMED})
        return None
    if document.get("schema") != RESPONSE_SCHEMA:
        reasons.append({"source": label, "reason": REASON_MALFORMED})
        return None

    envelope = document.get("envelope") or {}
    communications = document.get("communications")
    if not isinstance(communications, list):
        reasons.append({"source": label, "reason": REASON_MALFORMED})
        return None
    content_objects = document.get("content_objects") or {}
    if not isinstance(content_objects, dict):
        reasons.append({"source": label, "reason": REASON_MALFORMED})
        return None

    truncated = envelope.get("coverage") == COVERAGE_TRUNCATED
    return communications, content_objects, truncated, len(payload)


# ── content objects ─────────────────────────────────────────────────────────


def _read_content_bytes(
    project_state_dir: str,
    blob_id: str,
    expected_size: int,
    expected_sha256: Optional[str],
    root: str,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Read and verify one content object.  ``(payload, reason)``.

    The blob id is the sha256 of the content, so the object is self-verifying.
    The document's own ``sha256`` is checked too when the index supplies it.
    """
    if _SHA256_RE.fullmatch(blob_id) is None:
        return None, REASON_CONTENT_MISSING

    path = os.path.join(project_state_dir, CONTENT_OBJECTS_DIR, blob_id)
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None, REASON_CONTENT_UNREADABLE
    confined_root = root.rstrip(os.sep) + os.sep
    if not resolved.startswith(confined_root):
        return None, REASON_SYMLINK_REFUSED

    fd, reason = _open_nested(project_state_dir, ("communications", "content", blob_id))
    if fd is None:
        if reason == REASON_MISSING:
            return None, REASON_CONTENT_MISSING
        return None, reason or REASON_CONTENT_UNREADABLE
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, REASON_NOT_REGULAR
        if info.st_size > MAX_CONTENT_BYTES:
            return None, REASON_OVERSIZE
        if hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        chunks: List[bytes] = []
        total = 0
        while total <= MAX_CONTENT_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
    except OSError:
        return None, REASON_CONTENT_UNREADABLE
    finally:
        os.close(fd)
    if len(payload) > MAX_CONTENT_BYTES:
        return None, REASON_OVERSIZE
    if len(payload) != expected_size:
        return None, REASON_CONTENT_DIGEST_MISMATCH

    digest = hashlib.sha256(payload).hexdigest()
    if digest != blob_id:
        return None, REASON_CONTENT_DIGEST_MISMATCH
    if expected_sha256 is not None and digest != expected_sha256:
        return None, REASON_CONTENT_DIGEST_MISMATCH
    return payload, None


def _serve_document_content(
    doc: Dict[str, Any],
    project_dir: str,
    content_objects: Dict[str, Any],
    root: str,
) -> Tuple[Optional[bytes], Optional[str]]:
    """Return ``(bytes, reason)`` for a single document entry.

    A quarantined document is a tombstone: bytes are never returned.  A digest
    mismatch is treated as an unavailable object rather than as a successful
    read with corrupted bytes.
    """
    state = doc.get("content_state")
    if state == "content-quarantined":
        return None, REASON_CONTENT_QUARANTINED

    blob_id = doc.get("blob_id")
    if not isinstance(blob_id, str) or not blob_id:
        return None, REASON_CONTENT_MISSING

    meta = content_objects.get(blob_id)
    if isinstance(meta, dict) and meta.get("content_state") == "quarantined":
        return None, REASON_CONTENT_QUARANTINED

    if state != "present":
        return None, REASON_CONTENT_MISSING

    expected_size = doc.get("byte_size")
    expected_sha256 = doc.get("sha256")
    if not isinstance(expected_size, int) or expected_size < 0:
        return None, REASON_CONTENT_MISSING

    payload, reason = _read_content_bytes(
        project_dir, blob_id, expected_size, expected_sha256, root
    )
    if reason == REASON_CONTENT_DIGEST_MISMATCH:
        # Refuse to serve corrupted bytes; signal unavailability rather than
        # returning a tombstone that could be mistaken for legitimate content.
        raise CommunicationsCatalogUnavailable(REASON_CONTENT_DIGEST_MISMATCH)
    if payload is None:
        return None, reason or REASON_CONTENT_MISSING
    return payload, None


# ── response shaping ────────────────────────────────────────────────────────


def _prepare_list_item(entry: Dict[str, Any]) -> Dict[str, Any]:
    from cli_agent_orchestrator.models.communications_catalog import CommunicationListItem

    return CommunicationListItem.model_validate(entry).model_dump()


def _build_list_response(
    coverage: str,
    reasons: List[Dict[str, str]],
    communications: List[Dict[str, Any]],
    next_cursor: Optional[str],
    total: int,
) -> Dict[str, Any]:
    return {
        "schema": RESPONSE_SCHEMA,
        "coverage": coverage,
        "reasons": reasons[:MAX_PROJECTS],
        "communications": communications,
        "next_cursor": next_cursor,
        "total": total,
    }


def _empty_list_response(coverage: str, reasons: List[Dict[str, str]]) -> Dict[str, Any]:
    return _build_list_response(coverage, reasons, [], None, 0)


# ── public API ─────────────────────────────────────────────────────────────-


def list_communications(task_occurrence_id: str, cursor: Optional[str] = None) -> Dict[str, Any]:
    task_occurrence_id = _require_identifier(task_occurrence_id, field="task_occurrence_id")

    cursor_at: Optional[str] = None
    cursor_id: Optional[str] = None
    if cursor is not None and cursor != "":
        cursor_at, cursor_id = _cursor_decode(cursor)

    root = catalog_root()
    try:
        root = os.path.realpath(root)
    except OSError:
        return _empty_list_response(
            COVERAGE_UNAVAILABLE,
            [{"source": ROOT_SOURCE_LABEL, "reason": REASON_UNREADABLE}],
        )

    directories, root_reason, truncated, refusals = _project_dirs(root)
    if root_reason is not None:
        return _empty_list_response(
            COVERAGE_UNAVAILABLE,
            [{"source": ROOT_SOURCE_LABEL, "reason": root_reason}],
        )

    reasons: List[Dict[str, str]] = list(refusals)
    all_items: List[Tuple[str, str, Dict[str, Any]]] = []

    for name, directory in directories:
        label = _safe_source_name(name)
        read = _read_project_index(directory, root, reasons, label)
        if read is None:
            continue
        communications, _, project_truncated, _ = read
        if project_truncated:
            truncated = True
        for entry in communications:
            if not isinstance(entry, dict):
                continue
            if entry.get("task_occurrence_id") != task_occurrence_id:
                continue
            communication_id = entry.get("communication_id")
            recorded_at = entry.get("recorded_at")
            if not isinstance(communication_id, str) or not isinstance(recorded_at, str):
                continue
            all_items.append((_normalize_recorded_at(recorded_at), communication_id, entry))

    # Total order: recorded_at DESC, communication_id ASC.  Stable sort gives
    # the ascending id tie-break for free after a primary descending key sort.
    all_items.sort(key=lambda item: item[1])
    all_items.sort(key=lambda item: item[0], reverse=True)

    if cursor_at is not None and cursor_id is not None:
        all_items = [
            item
            for item in all_items
            if item[0] < cursor_at or (item[0] == cursor_at and item[1] < cursor_id)
        ]

    total = len(all_items)
    page = all_items[:PAGE_SIZE]
    next_cursor: Optional[str] = None
    if total > PAGE_SIZE:
        last = page[-1]
        next_cursor = _cursor_encode(last[0], last[1])

    out_items = [_prepare_list_item(entry) for _, _, entry in page]

    coverage = COVERAGE_COMPLETE
    if reasons:
        coverage = COVERAGE_PARTIAL
    if truncated or total > PAGE_SIZE:
        coverage = COVERAGE_TRUNCATED
    if truncated and not any(r["reason"] == REASON_PROJECT_LIMIT for r in reasons):
        reasons.append({"source": ROOT_SOURCE_LABEL, "reason": REASON_PROJECT_LIMIT})

    return _build_list_response(coverage, reasons, out_items, next_cursor, total)


def _scan_for_communication(
    communication_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    root = catalog_root()
    try:
        root = os.path.realpath(root)
    except OSError:
        raise CommunicationsCatalogUnavailable("conductor state root is unreadable") from None

    directories, root_reason, _, _ = _project_dirs(root)
    if root_reason is not None:
        raise CommunicationsCatalogUnavailable("conductor state root is missing or unreadable")

    for name, directory in directories:
        label = _safe_source_name(name)
        read = _read_project_index(directory, root, [], label)
        if read is None:
            continue
        communications, content_objects, _, _ = read
        for entry in communications:
            if isinstance(entry, dict) and entry.get("communication_id") == communication_id:
                return entry, directory, content_objects, root
    return None, None, None, None


def get_communication(communication_id: str) -> Dict[str, Any]:
    communication_id = _require_identifier(communication_id, field="communication_id")
    entry, project_dir, content_objects, root = _scan_for_communication(communication_id)
    if entry is None or project_dir is None or root is None:
        raise CommunicationsCatalogNotFound("communication not found")

    content: Optional[str] = None
    reason: Optional[str] = None
    body = entry.get("body")
    if isinstance(body, dict):
        payload, reason = _serve_document_content(body, project_dir, content_objects or {}, root)
        if payload is not None:
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError:
                raise CommunicationsCatalogUnavailable(REASON_CONTENT_UNREADABLE) from None

    return {
        "communication": _prepare_list_item(entry),
        "content": content,
        "reason": reason,
    }


def _scan_for_attachment(
    attachment_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[Dict[str, Any]], Optional[str]]:
    root = catalog_root()
    try:
        root = os.path.realpath(root)
    except OSError:
        raise CommunicationsCatalogUnavailable("conductor state root is unreadable") from None

    directories, root_reason, _, _ = _project_dirs(root)
    if root_reason is not None:
        raise CommunicationsCatalogUnavailable("conductor state root is missing or unreadable")

    for name, directory in directories:
        label = _safe_source_name(name)
        read = _read_project_index(directory, root, [], label)
        if read is None:
            continue
        communications, content_objects, _, _ = read
        for entry in communications:
            if not isinstance(entry, dict):
                continue
            body = entry.get("body")
            if isinstance(body, dict) and body.get("attachment_id") == attachment_id:
                return body, directory, content_objects, root
            for doc in entry.get("documents") or []:
                if isinstance(doc, dict) and doc.get("attachment_id") == attachment_id:
                    return doc, directory, content_objects, root
    return None, None, None, None


def get_attachment(attachment_id: str) -> Dict[str, Any]:
    attachment_id = _require_identifier(attachment_id, field="attachment_id")
    doc, project_dir, content_objects, root = _scan_for_attachment(attachment_id)
    if doc is None or project_dir is None or root is None:
        raise CommunicationsCatalogNotFound("attachment not found")

    from cli_agent_orchestrator.models.communications_catalog import DocumentEntry

    content: Optional[str] = None
    reason: Optional[str] = None
    payload, reason = _serve_document_content(doc, project_dir, content_objects or {}, root)
    if payload is not None:
        try:
            content = payload.decode("utf-8")
        except UnicodeDecodeError:
            raise CommunicationsCatalogUnavailable(REASON_CONTENT_UNREADABLE) from None

    return {
        "document": DocumentEntry.model_validate(doc).model_dump(),
        "content": content,
        "reason": reason,
    }
