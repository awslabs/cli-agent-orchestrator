"""The conductor annotation seam — one additive read, and no conductor semantics.

This module is the *entire* fork-side surface of the work-state projection
(design §9.5). It is deliberately written to never need a second edit: it reads
a fixed, conductor-owned location, bounds and confines what it finds, and hands
the result on **verbatim**. It knows nothing about work items, campaigns,
lifecycles, gates, rounds, tracks or chips, and it must never learn.

WHY THE FORK MUST STAY IGNORANT. ``CaoPlugin`` exposes ``setup()``,
``teardown()`` and ``on_mcp_server(mcp)`` — no route hook, no static mount, no
response-field contribution — so one fork change is unavoidable. Making it
*one* change rather than one per status-model revision requires that the fork
carry no vocabulary the conductor could outgrow. Concretely:

* ``kind`` is an OPAQUE bounded string. Nothing here (or in the renderer)
  branches on it, there is no allowlist, and there is no "supported kinds"
  constant to keep in step. That is what "unknown kinds are ignored" buys: the
  conductor adds a kind and it arrives, unannounced, with no fork release.
* ``semantic_role`` is carried through unmodified. The renderer resolves it
  against the six roles already in ``design-tokens/tokens.json`` and falls back
  to ``neutral`` for anything it does not recognise, so a new role degrades to
  a visibly-unstyled chip rather than to a blank card or a thrown render.
* The document's own ``schema`` string is *recorded and never enforced*. Pinning
  it would make a ``…-v2`` producer a fork change, which is exactly the outcome
  this seam exists to prevent. Items are validated individually and carry their
  own ``version``.

WHY NOT READ ``work-state.json`` DIRECTLY. The A2 sentinel already publishes
``<state root>/<project>/work-state.json``. Translating that into chips here
would put the six-label display policy, the dependency vocabulary, and every
future facet inside the fork — a second fork change per model revision, which
is precisely what §9.5 forbids. It would also republish material this route has
no business serving: real ``work-state.json`` items carry absolute ``run_dir``
paths and a free-form ``detail`` bag, and this dashboard is unauthenticated by
default (``conduct dashboard`` instructs ``tailscale serve``). So the seam
consumes a SEPARATE, presentation-ready, conductor-authored document whose
every field is already safe to display, and the conductor owns the projection
from work item to annotation.

WHAT "FIXED LOCATION" MEANS HERE. ``annotation_root()`` takes no argument, reads
no request state, consults no environment variable, and is not reachable from
the route's signature — the route has no parameters at all. ``CAO_STATE_ROOT``
deliberately does NOT move it: that knob relocates *CAO's* state, and this
directory belongs to the conductor, which resolves it as
``~/.local/state/cao-conductor`` in ``plugin/conductor_sentinel/sentinel.py``
(``STATE_ROOT_DEFAULT``) with no override of its own. Tests reach it by
patching this module's function, which is a test seam and not an operator knob.

FAILURE POSTURE. Every failure degrades to *fewer annotations*, never to an
error: a missing root, an unreadable root, a malformed document, an oversized
document, a symlinked path and an item that fails validation all reduce the
served list and add a typed, bounded reason. The route never returns 5xx for
input it read off disk, because a dashboard that 500s when a producer writes a
bad byte is worse than one that shows nothing — and showing nothing is exactly
what today's dashboard does, which is the compatibility floor §9.5 requires.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: The document this seam reads, one per conductor project state directory.
#: Named separately from ``work-state.json`` on purpose — see the module
#: docstring: that file is the canonical model, this one is its presentation
#: projection, and only the latter is safe to serve unauthenticated.
ANNOTATIONS_FILENAME = "annotations.json"

#: The wire schema of THIS route's response. Bumping it is a fork change, so it
#: describes only the envelope the fork owns (counts, coverage, reasons) and
#: never the annotation vocabulary, which is the conductor's.
RESPONSE_SCHEMA = "cao-annotations-v1"

# ── bounds ────────────────────────────────────────────────────────────────
#
# Every one of these truncates VISIBLY: the response carries the counters and
# the renderer draws a marker. Silent truncation is prohibited (§9.7) because a
# partial answer that renders like a complete one is the failure an operator
# cannot detect.

#: Most project directories scanned in one request. The measured fleet is 23.
MAX_SOURCES = 64
#: Most bytes read from any one document. The measured per-project work-state
#: snapshot is ~26KB; an annotation projection is a fraction of that.
MAX_SOURCE_BYTES = 256 * 1024
#: Most bytes read across the whole fan-out in one request.
MAX_TOTAL_BYTES = 2 * 1024 * 1024
#: Most annotations served in one response.
MAX_ITEMS = 500
#: Ceiling on any bounded string that is not a label or a detail value.
MAX_STRING = 200
#: Ceiling on a chip label. Longer labels are truncated with an ellipsis rather
#: than dropped: the conductor's wording is not the fork's to arbitrate, but an
#: unbounded string is a layout weapon on a 390px viewport.
MAX_LABEL = 64
#: Ceiling on the derived-facet bag: keys, and the length of each value.
#: ``details`` is the ONLY channel through which the conductor can add data
#: without a fork change — every top-level item field is an explicit key here —
#: so the key ceiling is the growth budget of the whole seam. 12 was too tight
#: for that job; anything past the cap is now counted and reported rather than
#: dropped in silence.
MAX_DETAIL_KEYS = 24
MAX_DETAIL_VALUE = 160
#: Extra identity keys carried on a subject beyond the four the fork names.
#: A subject type invented in 2031 arrives with an identifier the fork has never
#: heard of; dropping it leaves an unattributable chip, which is not actionable.
MAX_SUBJECT_KEYS = 8
#: Ceiling on ``priority``. Clamped, never rejected.
MAX_PRIORITY = 100
#: How long a document is assumed current when it declares no expiry of its own.
#: The producer republishes on a 30s tick, so ten ticks of grace absorbs a slow
#: tick without ever letting a dead producer look authoritative forever. This is
#: a FLOOR the fork derives from the file's own mtime, never an override: a
#: declared ``valid_until`` always wins.
DERIVED_VALIDITY_SECONDS = 300

#: Typed, bounded reasons. A reason NEVER carries a path, a payload excerpt, or
#: an exception string — the dashboard is unauthenticated by default and a
#: diagnostic that echoes the filesystem is an information leak with a
#: reassuring name.
REASON_MISSING = "missing"
REASON_UNREADABLE = "unreadable"
REASON_MALFORMED = "malformed"
REASON_OVERSIZE = "oversize"
REASON_NOT_REGULAR = "not-a-regular-file"
REASON_SYMLINK_REFUSED = "symlink-refused"
REASON_OUTSIDE_ROOT = "outside-root"
REASON_SOURCE_LIMIT = "source-limit"
REASON_BYTE_LIMIT = "byte-limit"
#: Something the producer sent did not fit the facet bag and was not served.
#: The bag is the seam's only growth path, so losing part of it silently would
#: be the one truncation an operator could never detect.
REASON_DETAIL_TRUNCATED = "detail-truncated"

#: The literal used in ``reasons[].source`` for the root itself, so that even
#: the root's own failure is reported without naming a directory.
ROOT_SOURCE_LABEL = "conductor-state-root"

#: Coverage vocabulary of the response envelope.
COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_TRUNCATED = "truncated"
COVERAGE_UNAVAILABLE = "unavailable"

#: Characters allowed in a reported source name. Project directory names come
#: off the filesystem, not off the wire, but they are still echoed into a
#: response, so they are filtered to a conservative set rather than trusted.
_SAFE_SOURCE_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


def annotation_root() -> str:
    """The fixed, non-configurable, conductor-owned annotation root.

    No parameter, no request state, and no CAO or conductor configuration
    variable — in particular ``CAO_STATE_ROOT`` does not move it. The path is
    resolved relative to the server process's own home directory, exactly as
    the producer resolves it, so ``expanduser`` reading ``HOME`` is the one
    environment input and it is the same input on both sides of the seam.

    The route that calls this takes no arguments, so there is no path for
    caller input to reach a filesystem operation — the property §9.5 asks for,
    obtained by construction rather than by sanitising.
    """
    return os.path.expanduser("~/.local/state/cao-conductor")


def _bounded(value: Any, limit: int = MAX_STRING) -> Optional[str]:
    """A string of at most ``limit`` characters, or None if this is not one."""
    if not isinstance(value, str):
        return None
    return value[:limit]


def _bounded_required(value: Any, limit: int = MAX_STRING) -> Optional[str]:
    """As ``_bounded``, but an empty string is not a value."""
    text = _bounded(value, limit)
    if text is None or not text.strip():
        return None
    return text


def _label(value: Any) -> Optional[str]:
    """A chip label: required, non-empty, ellipsised rather than dropped."""
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) <= MAX_LABEL:
        return value
    return value[: MAX_LABEL - 1] + "…"


def _safe_source_name(name: str) -> str:
    """A reportable source name: filtered, bounded, never a path."""
    filtered = "".join(ch for ch in name if ch in _SAFE_SOURCE_CHARS)
    return filtered[:64] or "unnamed"


def _detail_value(raw: Any) -> Optional[str]:
    """One facet value as a displayable string, or None if it cannot be one.

    Scalars stringify. A LIST of scalars joins, because a list is the natural
    shape for a facet like a dependency set and dropping it would delete the
    conductor's most obvious representation with no signal. Mappings are still
    dropped rather than flattened: a flattener is a schema in disguise, and a
    nested object has no single displayable form this bag could promise.
    """
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return _truncate_detail(str(raw))
    if isinstance(raw, str):
        return _truncate_detail(raw) if raw.strip() else None
    if isinstance(raw, (list, tuple)):
        parts: List[str] = []
        for element in raw:
            if isinstance(element, bool):
                parts.append("true" if element else "false")
            elif isinstance(element, (int, float)):
                parts.append(str(element))
            elif isinstance(element, str) and element.strip():
                parts.append(element)
        if not parts:
            return None
        return _truncate_detail(", ".join(parts))
    return None


def _truncate_detail(text: str) -> str:
    """Bounded like ``_label`` — ellipsised, so a cut value looks cut."""
    if len(text) <= MAX_DETAIL_VALUE:
        return text
    return text[: MAX_DETAIL_VALUE - 1] + "…"


def _coerce_details(value: Any) -> Tuple[Dict[str, str], int]:
    """``(facets, lost)`` — the derived-facet bag, bounded in both dimensions.

    Values are stringified rather than typed, because the fork must not acquire
    an opinion about which facets exist.

    THE SECOND RETURN VALUE IS THE POINT. This bag is the only channel the
    conductor can grow without a fork release, so it is also the only place a
    cap could silently delete a facet the operator needed — a chip that looks
    complete while its ``dependencies`` facet was thrown away is precisely the
    confidently-wrong rendering §9.7 prohibits. Every key past the cap and
    every value with no displayable form is COUNTED, and the count reaches the
    envelope as a reason.
    """
    if not isinstance(value, dict):
        return {}, 0
    out: Dict[str, str] = {}
    lost = 0
    for key, raw in value.items():
        name = _bounded_required(key, 48)
        if name is None:
            lost += 1
            continue
        if isinstance(raw, str) and not raw.strip():
            # An empty value carries nothing, so nothing was lost by omitting it.
            continue
        if len(out) >= MAX_DETAIL_KEYS:
            lost += 1
            continue
        text = _detail_value(raw)
        if text is None:
            lost += 1
            continue
        out[name] = text
    return out, lost


def _coerce_subject(value: Any) -> Optional[Dict[str, Optional[str]]]:
    """One annotation subject.

    A subject may name a terminal generation, a task, or a campaign (§9.5), so
    an unbound gate and an orphaned run both have somewhere to render. ``type``
    is carried verbatim and NOT validated against those three: a fourth subject
    type must arrive without a fork change, and the renderer already has a
    terminal-independent place to put anything it cannot attach to a row.

    The four named identity keys are joined by a BOUNDED PASSTHROUGH of any
    other string-valued key. Placement is durable without it — anything the
    fork cannot attach lands on the terminal-independent surface — but IDENTITY
    is not: a fixed whitelist means a subject type invented later arrives with
    no identifier at all, and "something is wrong somewhere" is not an operator
    action. The passthrough is capped and length-bounded exactly like the facet
    bag, so widening it costs nothing and can leak nothing unbounded.
    """
    if not isinstance(value, dict):
        return None
    subject_type = _bounded_required(value.get("type"), 40)
    if subject_type is None:
        return None
    out: Dict[str, Optional[str]] = {
        "type": subject_type,
        "terminal_id": _bounded(value.get("terminal_id"), 128),
        "generation": _bounded(value.get("generation"), 128),
        "task_id": _bounded(value.get("task_id"), 200),
        "campaign": _bounded(value.get("campaign"), 200),
    }
    extra = 0
    for key, raw in value.items():
        if extra >= MAX_SUBJECT_KEYS:
            break
        name = _bounded_required(key, 48)
        if name is None or name in out:
            continue
        text = _bounded_required(raw, 200)
        if text is None:
            continue
        out[name] = text
        extra += 1
    return out


def _coerce_version(value: Any) -> int:
    """The item's own version, coerced and never a reason to drop the item.

    Nothing in the fork consumes this: it exists so a future producer can say
    which item schema it wrote. Refusing an item because that bookkeeping field
    was a string would discard a perfectly renderable chip over a field with no
    reader, so anything unrecognisable degrades to 1.
    """
    if isinstance(value, bool):
        return 1
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, float) and value >= 1:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        if parsed >= 1:
            return parsed
    return 1


def _coerce_item(raw: Any) -> Tuple[Optional[Dict[str, Any]], int]:
    """``(item, facets_lost)`` — one annotation, bounded.

    An item of None is the whole unknown-input policy in one place: an item the
    fork cannot represent is DROPPED and counted, never raised and never
    half-rendered. Nothing here inspects ``kind`` or ``semantic_role`` beyond
    checking that they are bounded strings.
    """
    if not isinstance(raw, dict):
        return None, 0
    namespace = _bounded_required(raw.get("namespace"), 64)
    kind = _bounded_required(raw.get("kind"), 64)
    label = _label(raw.get("label"))
    semantic_role = _bounded_required(raw.get("semantic_role"), 32)
    subject = _coerce_subject(raw.get("subject"))
    if namespace is None or kind is None or label is None:
        return None, 0
    if semantic_role is None or subject is None:
        return None, 0

    priority = raw.get("priority", 50)
    if isinstance(priority, bool) or not isinstance(priority, (int, float)):
        priority = 50
    priority = max(0, min(MAX_PRIORITY, int(priority)))

    details, facets_lost = _coerce_details(raw.get("details"))
    return {
        "namespace": namespace,
        "kind": kind,
        "version": _coerce_version(raw.get("version")),
        "label": label,
        "semantic_role": semantic_role,
        "priority": priority,
        "subject": subject,
        "valid_until": _bounded(raw.get("valid_until"), 40),
        # ``_bounded``, NOT ``_bounded_required``. An empty value here is a
        # third state, not an absent one: the producer distinguishes "not an
        # identity chip" (field absent) from "an identity chip with no colour"
        # (empty), and the renderer draws those two differently. The required
        # variant collapses the empty case to None and deletes one of the
        # three renderings.
        "colour_key": _bounded(raw.get("colour_key"), 64),
        "details": details,
    }, facets_lost


def _open_document(directory: str) -> Tuple[Optional[int], Optional[str]]:
    """A descriptor on one project's document, or a typed refusal.

    The parent directory is opened first, ``O_DIRECTORY|O_NOFOLLOW``, and the
    document is then opened RELATIVE TO THAT DESCRIPTOR. That is what closes
    the check-then-open race on the intermediate component: between resolving
    the path and opening the file, nothing can swap ``<root>/<project>`` for a
    symlink, because the open never re-walks that name.

    ``O_NONBLOCK`` IS LOAD-BEARING, NOT AN OPTIMISATION. ``open(O_RDONLY)`` on a
    FIFO blocks until a writer appears — forever, uninterruptibly, and
    ``O_NOFOLLOW`` does not help because a FIFO is not a symlink. This route is
    off-loaded with ``asyncio.to_thread``, which uses the event loop's DEFAULT
    executor, shared with every other blocking call in the API; one stale FIFO
    in the state root would park one worker per 5s dashboard poll until the
    whole pool was gone and the server could run no blocking work at all. With
    ``O_NONBLOCK`` the open returns immediately and the ``S_ISREG`` check below
    — otherwise unreachable for the one non-regular file type that hurts —
    refuses it as ``not-a-regular-file``. Blocking mode is restored once the
    descriptor is known to be a regular file, so the read path is unchanged.
    """
    dir_flags = os.O_RDONLY
    dir_flags |= getattr(os, "O_DIRECTORY", 0)
    dir_flags |= getattr(os, "O_NOFOLLOW", 0)
    dir_flags |= getattr(os, "O_CLOEXEC", 0)
    dir_flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        dfd = os.open(directory, dir_flags)
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return None, REASON_MISSING
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            return None, REASON_SYMLINK_REFUSED
        if exc.errno == errno.ENOTDIR:
            return None, REASON_NOT_REGULAR
        return None, REASON_UNREADABLE

    file_flags = os.O_RDONLY
    file_flags |= getattr(os, "O_NOFOLLOW", 0)
    file_flags |= getattr(os, "O_CLOEXEC", 0)
    file_flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(ANNOTATIONS_FILENAME, file_flags, dir_fd=dfd)
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


def _read_document(
    directory: str, root: str
) -> Tuple[Optional[bytes], Optional[float], Optional[str]]:
    """Read one project's annotation document. ``(payload, mtime, reason)``.

    Path confinement is obtained three ways, deliberately overlapping, because
    each one alone has a hole:

    1. the directory entry was taken from ``os.scandir`` on the fixed root and
       checked with ``follow_symlinks=False``, so no component between the root
       and the file can be a symlink;
    2. the document is opened ``O_NOFOLLOW`` **relative to an ``O_NOFOLLOW``
       descriptor on its parent**, which refuses a symlink at either component
       atomically — the check-then-open race an ``islink()`` test cannot close,
       for the whole path rather than only its last name;
    3. the resolved path is required to sit under the resolved root, which is
       the explicit statement of the invariant rather than a consequence of the
       first two, so it fails loudly if either is ever weakened.

    The mtime comes back because it is the fork's own freshness floor: a
    document that declares no expiry still gets one, derived from when it was
    written (§9.6). Without it a producer that died last week renders exactly
    like one that published a second ago.
    """
    path = os.path.join(directory, ANNOTATIONS_FILENAME)
    try:
        resolved = os.path.realpath(path)
    except OSError:
        return None, None, REASON_UNREADABLE
    confined_root = root.rstrip(os.sep) + os.sep
    if not resolved.startswith(confined_root):
        return None, None, REASON_OUTSIDE_ROOT

    fd, reason = _open_document(directory)
    if fd is None:
        return None, None, reason
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None, None, REASON_NOT_REGULAR
        if info.st_size > MAX_SOURCE_BYTES:
            return None, None, REASON_OVERSIZE
        if hasattr(os, "set_blocking"):
            os.set_blocking(fd, True)
        # Looped, because one os.read() may short-read a perfectly valid
        # document and half a JSON object parses as "malformed". The cap+1
        # accumulation is preserved: a file that grew between fstat and read is
        # refused as oversize rather than silently half-parsed.
        chunks: List[bytes] = []
        total = 0
        while total <= MAX_SOURCE_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
    except OSError:
        return None, None, REASON_UNREADABLE
    finally:
        os.close(fd)
    if len(payload) > MAX_SOURCE_BYTES:
        return None, None, REASON_OVERSIZE
    return payload, info.st_mtime, None


def _parse_expiry(value: Optional[str]) -> Optional[float]:
    """An ISO-8601 expiry as an epoch, or None when it is not one."""
    if not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _derived_expiry(mtime: float) -> str:
    """The freshness floor the fork derives when a producer declares none."""
    at = datetime.fromtimestamp(mtime + DERIVED_VALIDITY_SECONDS, tz=timezone.utc)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_dirs(
    root: str,
) -> Tuple[List[Tuple[str, str]], Optional[str], bool, List[Dict[str, str]]]:
    """``(entries, reason, truncated, refusals)`` — child directories of the root.

    A symlinked child is refused rather than followed, so the root cannot be
    used as a springboard into an arbitrary tree. Entries are sorted so the
    response is deterministic and the source cap is not a lottery.

    A REFUSAL IS REPORTED; A NON-CANDIDATE IS NOT. A symlink that resolves to a
    directory looks exactly like a project the operator expects to see, so
    declining it silently would report ``complete`` coverage over a source that
    was never read. Plain files at the root are a different thing entirely:
    §9.7 documents that the state root legitimately holds ``conductor-repo.json``,
    ``pending-issues.ndjson`` and more, and charging each of those as a failed
    source would make every healthy fleet permanently ``partial`` — a warning
    that is always on is a warning nobody reads.
    """
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
        if len(out) >= MAX_SOURCES:
            truncated = True
            break
        out.append((entry.name, entry.path))
    return out, None, truncated, refusals


def _empty(coverage: str, reasons: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "annotation_schema": RESPONSE_SCHEMA,
        "coverage": coverage,
        "sources_read": 0,
        "sources_failed": len(reasons),
        "items_dropped": 0,
        "items_omitted": 0,
        "facets_dropped": 0,
        "reasons": reasons,
        "annotations": [],
    }


def _note(reasons: List[Dict[str, str]], source: str, reason: str) -> None:
    """Record a reason once per source, so one bad document is not a flood."""
    entry = {"source": source, "reason": reason}
    if entry not in reasons:
        reasons.append(entry)


def _is_expired(valid_until: Optional[str], now: float) -> bool:
    """True only when the fork can PROVE the claim is past its expiry.

    An absent or unparseable expiry is not proof of anything, so it is not
    treated as expired here — the sort must not demote a claim on a guess. The
    renderer draws that case as explicitly unknown instead.
    """
    at = _parse_expiry(valid_until)
    return at is not None and now > at


def _read_source(
    directory: str,
    root: str,
    label: str,
    reasons: List[Dict[str, str]],
) -> Optional[Tuple[List[Dict[str, Any]], int, int, int]]:
    """One project's contribution: ``(items, bytes, dropped, facets_dropped)``.

    None means the source contributed nothing and has already been accounted
    for in ``reasons`` (or was simply absent, which is not a failure).

    Everything that can go wrong with ONE producer is contained here, so the
    fan-out above can wrap a single call and charge any surprise to a single
    source name.
    """
    payload, mtime, reason = _read_document(directory, root)
    if reason == REASON_MISSING:
        # A project with no annotation document is not a failure: the producer
        # publishes only what it has.
        return None
    if payload is None:
        _note(reasons, label, reason or REASON_UNREADABLE)
        return None
    try:
        document = json.loads(payload.decode("utf-8"))
    except Exception:  # noqa: BLE001 - malformed is malformed, however it fails
        _note(reasons, label, REASON_MALFORMED)
        return None
    if not isinstance(document, dict):
        _note(reasons, label, REASON_MALFORMED)
        return None
    raw_items = document.get("annotations")
    if not isinstance(raw_items, list):
        _note(reasons, label, REASON_MALFORMED)
        return None

    # The document's declared ``valid_until`` is the default freshness for
    # every item it carries; an item may override it. Denormalised onto the
    # item because one response merges several producers' envelopes, and a
    # single response-level expiry would grey a fresh campaign because a
    # different campaign's producer stalled (§9.6).
    #
    # WHEN NOBODY DECLARES ONE, THE FORK DERIVES IT. `valid_until` is the only
    # field that governs whether a chip is drawn as current, and it was the one
    # field with no bound and no default: a producer that omitted it, or a
    # conductor process that died leaving the file on disk, rendered in full
    # vivid colour forever, indistinguishable from a claim validated a second
    # ago. The file's own mtime plus a bounded grace is a floor the fork can
    # always compute, so "I read this but have no idea whether it is still
    # true" is never answered with "it is true".
    document_valid_until = _bounded(document.get("valid_until"), 40)
    if document_valid_until is None and mtime is not None:
        document_valid_until = _derived_expiry(mtime)

    items: List[Dict[str, Any]] = []
    dropped = 0
    facets_dropped = 0
    for raw in raw_items:
        item, facets_lost = _coerce_item(raw)
        facets_dropped += facets_lost
        if item is None:
            dropped += 1
            continue
        if item["valid_until"] is None:
            item["valid_until"] = document_valid_until
        item["source"] = label
        items.append(item)
    return items, len(payload), dropped, facets_dropped


def read_annotations() -> Dict[str, Any]:
    """Every renderable annotation the conductor has published. NEVER RAISES.

    An unset or unreadable source produces an empty list with
    ``coverage: "unavailable"``, which is byte-identical in effect to the
    dashboard's behaviour before this route existed: no chips, no error, no
    empty state. That is the compatibility floor, and it is the common case on
    a machine with no conductor installed.
    """
    try:
        return _read_annotations()
    except Exception:  # noqa: BLE001 - a projection must never take the server down
        logger.debug("annotation read failed", exc_info=True)
        return _empty(
            COVERAGE_UNAVAILABLE,
            [{"source": ROOT_SOURCE_LABEL, "reason": REASON_UNREADABLE}],
        )


def _read_annotations() -> Dict[str, Any]:
    root = annotation_root()
    try:
        root = os.path.realpath(root)
    except OSError:
        return _empty(
            COVERAGE_UNAVAILABLE,
            [{"source": ROOT_SOURCE_LABEL, "reason": REASON_UNREADABLE}],
        )

    directories, root_reason, source_truncated, refusals = _project_dirs(root)
    if root_reason is not None:
        # Missing is the ordinary state on a machine with no conductor, so it
        # is reported as unavailable rather than as a failure the operator is
        # expected to act on.
        return _empty(
            COVERAGE_UNAVAILABLE,
            [{"source": ROOT_SOURCE_LABEL, "reason": root_reason}],
        )

    reasons: List[Dict[str, str]] = list(refusals)
    items: List[Dict[str, Any]] = []
    sources_read = 0
    dropped = 0
    facets_dropped = 0
    total_bytes = 0
    byte_capped = False

    if source_truncated:
        _note(reasons, ROOT_SOURCE_LABEL, REASON_SOURCE_LIMIT)

    for name, directory in directories:
        label = _safe_source_name(name)
        if total_bytes >= MAX_TOTAL_BYTES:
            byte_capped = True
            _note(reasons, label, REASON_BYTE_LIMIT)
            continue
        # ONE SOURCE CANNOT UNWIND THE FAN-OUT. Everything below is charged to
        # `label` and nothing else, because the alternative is what the blanket
        # handler in `read_annotations` would do with a late failure: discard
        # every item already collected from healthy producers and answer
        # "no conductor state root" — confidently wrong, when the truth is
        # "one of 23 producers wrote garbage". On a 15-worker fleet that is
        # every chip on the dashboard disappearing for one bad document.
        # RecursionError from a deeply-nested document is the concrete case:
        # it is a RuntimeError, not a ValueError, and it used to escape.
        try:
            read = _read_source(directory, root, label, reasons)
        except Exception:  # noqa: BLE001 - see above; the loop must survive anything
            logger.debug("annotation source failed", exc_info=True)
            _note(reasons, label, REASON_UNREADABLE)
            continue
        if read is None:
            continue
        source_items, consumed, source_dropped, facets_lost = read
        total_bytes += consumed
        sources_read += 1
        dropped += source_dropped
        facets_dropped += facets_lost
        if facets_lost:
            _note(reasons, label, REASON_DETAIL_TRUNCATED)
        items.extend(source_items)

    # FRESHNESS OUTRANKS PRIORITY. An annotation the fork can already tell is no
    # longer true must not consume a served slot ahead of one that is: a p99
    # claim that expired last week beating a live p10 `danger` to the cap means
    # the operator's overflow marker hides the only thing that is still
    # happening. The renderer applies the same key to the per-row cap.
    now = datetime.now(timezone.utc).timestamp()
    items.sort(
        key=lambda i: (
            1 if _is_expired(i["valid_until"], now) else 0,
            -i["priority"],
            i["source"],
            i["namespace"],
            i["kind"],
            i["label"],
        )
    )
    omitted = 0
    if len(items) > MAX_ITEMS:
        omitted = len(items) - MAX_ITEMS
        items = items[:MAX_ITEMS]

    if omitted or source_truncated or byte_capped or facets_dropped:
        coverage = COVERAGE_TRUNCATED
    elif reasons or dropped:
        coverage = COVERAGE_PARTIAL
    else:
        coverage = COVERAGE_COMPLETE

    return {
        "annotation_schema": RESPONSE_SCHEMA,
        "coverage": coverage,
        "sources_read": sources_read,
        "sources_failed": len(reasons),
        "items_dropped": dropped,
        "items_omitted": omitted,
        "facets_dropped": facets_dropped,
        "reasons": reasons[:MAX_SOURCES],
        "annotations": items,
    }
