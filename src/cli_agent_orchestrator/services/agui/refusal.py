"""The metadata-only refusal record for an off-list ``emit_ui`` call (#1158).

``POST /agui/v1/emit_ui`` refuses a component that is not on the frozen
allow-list with a 400. So that operators can see the refusal, the server also
publishes a record built from a fixed schema: a cleaned name and, when calls
were folded into it, a count. Nothing else from the request is copied into it,
and the props are never read.

- :func:`refusal_name` cleans the requested name to ``[A-Za-z0-9_.-]`` and
  always appends a short hash of the original, so the result is at most 33
  bytes (24 + "." + 8 hex digits) and can never equal an allow-listed name (none contains a ``.``).
- :class:`RefusalLimiter` decides whether a refusal is published. It dedupes by
  (terminal, name, window), limits each terminal (and, overall, the whole
  server) per window, and folds what it holds back into a count carried by
  that terminal's next published refusal. Its memory is bounded.
- :func:`ring_share_reached` bounds the refusals' share of the event log's
  ring, which real events replay from: at most a tenth of it.

A folded count is reported only with that terminal's next published refusal,
so a flood that stops leaves its last count unreported, and a bucket the
limiter evicts loses its count.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)

REFUSAL_HASH_CHARS = 8
# The visible, cleaned prefix of the requested name. It is caller text, not redacted,
# so it is kept short: the hash is what keeps two names apart.
REFUSAL_NAME_PREFIX_CHARS = 24
_PREFIX_CHARS = REFUSAL_NAME_PREFIX_CHARS
_SAFE = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")

# The detail's event type. ``agent_ui`` is the on-list record's.
REFUSAL_EVENT_TYPE = "agent_ui_refused"

# At most this fraction of the event log's ring may hold refusal records.
REFUSAL_RING_SHARE = 0.1


_share_full = False
_share_lock = threading.Lock()


def ring_share_reached(held: int, capacity: int) -> bool:
    """Whether ``held`` refusal records already fill their share of a ring of ``capacity``.

    The event log is shared with real events, and its ring is the horizon a
    ``since`` or ``Last-Event-ID`` replay reaches back to; refusals past their
    share are folded instead of appended, so they can't cycle the ring. ``held``
    must count only records still inside the event log's TTL (the window
    ``history()`` returns), or expired rows would keep refusals folded forever.
    """
    return held >= int(capacity * REFUSAL_RING_SHARE)


def note_ring_share(reached: bool) -> None:
    """Log one warning when refusals start being folded because their share is full.

    Refusals folded this way are not published, so the operator sees no record for
    them. The warning is the only trace; it is logged once per time the share fills,
    and the flag clears when live refusal records fall back below the share.
    """
    global _share_full
    with _share_lock:
        if reached and not _share_full:
            logger.warning(
                "refusal records fill their share of the event log (%d%%); further "
                "off-list emit_ui refusals are folded into a count, not published, until "
                "refusal records expire from the 24 h window",
                int(REFUSAL_RING_SHARE * 100),
            )
        _share_full = reached


def refusal_name(raw: str) -> str:
    """The name a refusal record carries for the requested component ``raw``.

    Every character outside ``[A-Za-z0-9_.-]`` is REPLACED by ``_`` (deleting
    would let ``"metric!"`` become ``"metric"``), the result is cut to
    ``REFUSAL_NAME_PREFIX_CHARS`` characters, and ``.`` plus the first 8 hex
    digits of the SHA-256 of the whole original is always appended. The prefix
    is caller text, not redacted; only the length is bounded. Every replacement
    is one ASCII byte, so the name is at most 33 bytes: 24 + "." + 8 hex digits.
    """
    digest = hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()[:REFUSAL_HASH_CHARS]
    cleaned = "".join(c if c in _SAFE else "_" for c in raw[:_PREFIX_CHARS])
    return f"{cleaned}.{digest}"


def refusal_detail(name: str, more_refused: int) -> Dict[str, Any]:
    """The record's ``detail``: the fixed schema, with no props."""
    ui: Dict[str, Any] = {"component": name}
    if more_refused:
        ui["more_refused"] = more_refused
    return {"event_type": REFUSAL_EVENT_TYPE, "ui": ui}


class _Bucket:
    __slots__ = ("window_start", "names", "folded")

    def __init__(self, now: float) -> None:
        self.window_start = now
        self.names: Set[str] = set()
        self.folded = 0


class RefusalLimiter:
    """Which refusals are published, with bounded memory.

    A bucket is a terminal CAO knows, or one shared bucket for every call it
    can't attribute. In each window a bucket publishes at most ``per_bucket``
    records, each for a different name, and the server at most
    ``global_per_window``. Any other call is folded: counted in its bucket, and
    the count rides on that bucket's next published record as ``more_refused``.
    At most ``max_buckets`` buckets are kept (least recently used is dropped),
    each holding at most ``per_bucket`` names.
    """

    def __init__(
        self,
        *,
        window_seconds: float = 10.0,
        per_bucket: int = 5,
        global_per_window: int = 20,
        max_buckets: int = 256,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window = window_seconds
        self._per_bucket = per_bucket
        self._global_cap = global_per_window
        self._max_buckets = max_buckets
        self._clock = clock
        self._buckets: "OrderedDict[str, _Bucket]" = OrderedDict()
        self._global_start = clock()
        self._global_count = 0
        self._lock = threading.Lock()

    def decide(self, terminal_id: Optional[str], name: str) -> Optional[int]:
        """``None`` to publish nothing, else the ``more_refused`` count to publish with."""
        key = terminal_id or ""
        with self._lock:
            now = self._clock()
            if now - self._global_start >= self._window:
                self._global_start, self._global_count = now, 0
            bucket = self._buckets.pop(key, None) or _Bucket(now)
            self._buckets[key] = bucket  # most recently used last
            while len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
            if now - bucket.window_start >= self._window:
                bucket.window_start, bucket.names = now, set()
            if (
                name in bucket.names
                or len(bucket.names) >= self._per_bucket
                or self._global_count >= self._global_cap
            ):
                bucket.folded += 1
                return None
            bucket.names.add(name)
            self._global_count += 1
            folded, bucket.folded = bucket.folded, 0
            return folded

    def fold(self, terminal_id: Optional[str]) -> None:
        """Count one refusal into its bucket without publishing it."""
        key = terminal_id or ""
        with self._lock:
            bucket = self._buckets.pop(key, None) or _Bucket(self._clock())
            self._buckets[key] = bucket
            while len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
            bucket.folded += 1

    @property
    def tracked_buckets(self) -> int:
        with self._lock:
            return len(self._buckets)

    @property
    def tracked_names(self) -> int:
        with self._lock:
            return sum(len(b.names) for b in self._buckets.values())


_limiter: Optional[RefusalLimiter] = None
_limiter_lock = threading.Lock()


def get_refusal_limiter() -> RefusalLimiter:
    """The process-wide limiter (lazily created)."""
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RefusalLimiter()
    return _limiter


def reset_refusal_limiter() -> None:
    """Drop the singleton (used by tests to start clean)."""
    global _limiter
    with _limiter_lock:
        _limiter = None
