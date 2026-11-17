"""The poller and the cache the HTTP layer answers from.

One thread reads CAO, but only while a client asked for the shelf in the last 30 seconds. The read
cadence per session follows its class (hot every tick, parked every 15 s, stale every 60 s), the
``caller_id`` of a terminal is read once and kept, and history (inbox rows) is read lazily. The
clock and the sleeper are injected so tests drive ticks by hand.

Only ``GET`` calls are made here; nothing in this module writes to CAO.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from . import history as history_mod
from . import tiering
from .cao import SESSION_PREFIX, CaoClient, cao_session_name, validate_cao_url
from .errors import TRANSPORT_CODES, ShelfError
from .ledger import Ledger
from .validation import NAME_RE, TERMINAL_ID_RE

log = logging.getLogger("cao_shelf.poller")

ACTIVE_WINDOW = 30.0
CLASS_INTERVAL = {"hot": 0.0, "parked": 15.0, "stale": 60.0}
HEALTH_INTERVAL = 300.0
HISTORY_MIN_INTERVAL = 300.0
HISTORY_OPEN_MAX_AGE = 15.0  # opening the drawer or "All history" re-reads a cache older than this
HISTORY_READS_PER_TICK = 4
HISTORY_ALL_BUDGET_SECONDS = 8.0  # "All history" stops reading CAO after this long and says so
CALLER_READS_PER_TICK = 100
CALLER_RETRY_SECONDS = 30.0
FIRST_SHELF_WAIT = 4.0

ClientFactory = Callable[[str, Callable[[str, str], None]], CaoClient]
RelayView = Callable[[], Dict[str, Dict[str, Any]]]
RelayReadHook = Callable[
    [List[str], Dict[str, List[Dict[str, Any]]], Dict[str, Optional[str]], Any], None
]


@dataclass
class _View:
    rows: List[Dict[str, Any]]
    read_at: float
    poll_class: str = "hot"


@dataclass
class _History:
    rows: List[Dict[str, Any]]
    read_at: float
    conductor_id: str
    conductor_last_active: str


@dataclass
class _State:
    views: Dict[str, _View] = field(default_factory=dict)
    callers: Dict[str, Optional[str]] = field(default_factory=dict)
    caller_retry: Dict[str, float] = field(default_factory=dict)
    history: Dict[str, _History] = field(default_factory=dict)
    conductors: Dict[str, Dict[str, str]] = field(default_factory=dict)
    snapshot: Optional[Dict[str, Any]] = None
    good_at: float = 0.0
    failure: Optional[ShelfError] = None
    partial: Optional[ShelfError] = None
    version: Optional[str] = None
    backend: Optional[str] = None
    health_at: float = -1e18
    listed: List[str] = field(default_factory=list)
    hidden: int = 0


def _slim(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Keep only what history needs. Message text is dropped on the way in."""
    return [
        {
            "sender_id": r.get("sender_id"),
            "created_at": r.get("created_at"),
            "status": r.get("status"),
        }
        for r in rows
    ]


class Poller:
    def __init__(
        self,
        client_factory: ClientFactory,
        settings: Callable[[], Dict[str, Any]],
        ledger: Ledger,
        *,
        clock: Callable[[], float] = time.time,
        wait: Optional[Callable[[float], Any]] = None,
    ) -> None:
        self._factory = client_factory
        self._settings = settings
        self._ledger = ledger
        self._clock = clock
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._wait = wait if wait is not None else self._wake.wait
        self._thread: Optional[threading.Thread] = None
        self._st = _State()
        self._revision = 0
        self._signature = ""
        self._ticks = 0
        self._last_touch = -1e18
        self._last_tick_at: Optional[float] = None
        self._calls: List[float] = []
        self._client: Optional[CaoClient] = None
        self._relay_view: Optional[RelayView] = None
        self._on_read: Optional[RelayReadHook] = None

    # ------------------------------------------------------------------ plumbing
    def set_relay_hooks(self, view: RelayView, on_read: RelayReadHook) -> None:
        """``view`` gives each session's ``relay`` field; ``on_read`` sees every good read (the
        relays reconcile from it without asking CAO again)."""
        self._relay_view = view
        self._on_read = on_read

    def observe(self, method: str, path: str) -> None:
        now = self._clock()
        with self._lock:
            self._calls.append(now)
            if len(self._calls) > 5000:
                del self._calls[:2500]

    def requests_last_minute(self) -> int:
        cutoff = self._clock() - 60.0
        with self._lock:
            return sum(1 for t in self._calls if t >= cutoff)

    def _now_dt(self, now: Optional[float] = None) -> datetime:
        return datetime.fromtimestamp(self._clock() if now is None else now, tz=timezone.utc)

    def client(self) -> CaoClient:
        """The read client for the configured CAO address (rebuilt when the address changes)."""
        url = self._settings()["cao_url"]
        wanted = validate_cao_url(url)
        with self._lock:
            if self._client is None or self._client.base_url != wanted:
                self._client = self._factory(url, self.observe)
                self._st = _State()
                self._signature = ""
                self._ledger.set_cao_url(wanted)
            return self._client

    # ------------------------------------------------------------------ activity
    def active(self) -> bool:
        return self._clock() - self._last_touch <= ACTIVE_WINDOW

    def mark_active(self) -> bool:
        """A client asked for the shelf. Returns True when the poller had been idle."""
        was_active = self.active()
        self._last_touch = self._clock()
        if not was_active:
            self._wake.set()
        return not was_active

    def wake(self) -> None:
        """Ask the loop to tick now (a setting changed)."""
        self._wake.set()

    def step(self) -> bool:
        """One iteration of the loop: tick when a client is active. Returns whether it ticked."""
        if self._stop.is_set() or not self.active():
            return False
        self.tick()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            if self.step():
                self._wait(float(self._settings()["poll_seconds"]))
            else:
                self._wake.wait()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="shelf-poller", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ one tick
    def tick(self) -> None:
        with self._tick_lock:
            now = self._clock()
            try:
                client = self.client()
                self._ledger.set_enabled(bool(self._settings()["persist_history"]))
                self._read(client, now)
                with self._lock:
                    self._st.failure = None
                    self._st.good_at = now
                self._publish(now)
            except ShelfError as err:
                self._fail(err, now)
            except Exception as exc:  # the thread must never die
                log.error("poll tick failed: %s", type(exc).__name__)
                self._fail(ShelfError("internal", "the poller hit an unexpected error"), now)
            finally:
                with self._cond:
                    self._ticks += 1
                    self._last_tick_at = now
                    self._cond.notify_all()

    def _read(self, client: CaoClient, now: float) -> None:
        st = self._st
        tol = float(self._settings()["poll_seconds"]) / 2
        listed = client.list_sessions()
        names: List[str] = []
        unaddressable: Set[str] = set()
        for row in listed:
            name = row["name"]
            if not name.startswith(SESSION_PREFIX):
                continue
            if NAME_RE.fullmatch(name[len(SESSION_PREFIX) :]):
                if name not in names:
                    names.append(name)
            else:
                unaddressable.add(name)  # counted on the shelf, never read or stopped
        if now - st.health_at >= HEALTH_INTERVAL:
            try:
                info = client.health()
                st.version = info.get("version") if isinstance(info.get("version"), str) else None
                backend = info.get("terminal_backend")
                st.backend = backend if isinstance(backend, str) else None
                st.health_at = now
            except ShelfError as err:
                if err.code in TRANSPORT_CODES:
                    raise
                st.health_at = now
        with self._lock:
            st.listed = names
            st.hidden = len(unaddressable)
            for gone in [n for n in st.views if n not in names]:
                del st.views[gone]
            bare_names = {n[len(SESSION_PREFIX) :] for n in names}
            for gone_history in [b for b in st.history if b not in bare_names]:
                del st.history[gone_history]
        partial: Optional[ShelfError] = None
        for name in names:
            view = st.views.get(name)
            if view is not None:
                interval = CLASS_INTERVAL.get(view.poll_class, 0.0)
                if interval > 0.0 and now - view.read_at < interval - tol:
                    continue
            try:
                data = client.get_session(name)
            except ShelfError as err:
                if err.code in TRANSPORT_CODES:
                    raise
                if err.code == "not_found":
                    with self._lock:
                        st.views.pop(name, None)
                else:
                    partial = err
                continue
            fresh = _View(
                rows=data["terminals"], read_at=now, poll_class=view.poll_class if view else "hot"
            )
            with self._lock:
                st.views[name] = fresh
        st.partial = partial
        self._read_callers(client, now)
        self._update_conductors()
        self._read_history_due(client, now)
        self._tell_relays()

    def _tell_relays(self) -> None:
        if self._on_read is None:
            return
        with self._lock:
            listed = list(self._st.listed)
            views = {name: list(view.rows) for name, view in self._st.views.items()}
            callers = dict(self._st.callers)
            live = self.role_live_ids()
        try:
            self._on_read(listed, views, callers, live)
        except Exception as exc:  # a relay problem must never stop the shelf
            log.error("relay reconcile failed: %s", type(exc).__name__)

    def _read_callers(self, client: CaoClient, now: float) -> None:
        st = self._st
        live: Set[str] = set()
        wanted: List[str] = []
        for view in st.views.values():
            ids = [
                str(r.get("id")) for r in view.rows if TERMINAL_ID_RE.fullmatch(str(r.get("id")))
            ]
            live.update(ids)
            # every terminal, the first one too: a first terminal whose caller is gone is an
            # orphaned helper, not the conductor (read once per id, then cached)
            wanted.extend(ids)
        with self._lock:
            for tid in [t for t in st.callers if t not in live]:
                del st.callers[tid]
            for tid in [t for t in st.caller_retry if t not in live]:
                del st.caller_retry[tid]
        reads = 0
        for tid in wanted:
            if tid in st.callers or st.caller_retry.get(tid, 0.0) > now:
                continue
            if reads >= CALLER_READS_PER_TICK:
                break
            reads += 1
            try:
                data = client.get_terminal(tid)
            except ShelfError as err:
                if err.code in TRANSPORT_CODES:
                    raise
                st.caller_retry[tid] = now + CALLER_RETRY_SECONDS
                continue
            caller = data.get("caller_id")
            known = caller if isinstance(caller, str) and TERMINAL_ID_RE.fullmatch(caller) else None
            with self._lock:
                st.callers[tid] = known
                st.caller_retry.pop(tid, None)

    def _update_conductors(self) -> None:
        """Which terminal is each session's conductor (needed before history can be read)."""
        st = self._st
        with self._lock:
            live = self.role_live_ids()
            found: Dict[str, Dict[str, str]] = {}
            for name, view in st.views.items():
                rows = [r for r in view.rows if TERMINAL_ID_RE.fullmatch(str(r.get("id")))]
                if not rows or not NAME_RE.fullmatch(name[len(SESSION_PREFIX) :]):
                    continue
                cid = tiering.pick_conductor(rows, st.callers, live)
                if cid is None:
                    continue  # no conductor: there is no inbox to read history from
                row = next(r for r in rows if r["id"] == cid)
                found[name[len(SESSION_PREFIX) :]] = {
                    "id": cid,
                    "last_active": str(row.get("last_active", "")),
                }
            st.conductors = found

    # ------------------------------------------------------------------ history
    def _conductor_of(self, bare: str) -> Optional[Dict[str, str]]:
        with self._lock:
            return self._st.conductors.get(bare)

    def _history_due(self, bare: str, now: float) -> bool:
        info = self._conductor_of(bare)
        if info is None:
            return False
        hist = self._st.history.get(bare)
        if hist is None:
            return True
        if now - hist.read_at < HISTORY_MIN_INTERVAL:
            return False
        return hist.conductor_id != info["id"] or hist.conductor_last_active != info["last_active"]

    def _read_history_due(self, client: CaoClient, now: float) -> None:
        reads = 0
        for bare in list(self._st.conductors):
            if reads >= HISTORY_READS_PER_TICK:
                break
            if self._history_due(bare, now):
                reads += 1
                try:
                    self._read_history(client, bare, now)
                except ShelfError as err:
                    if err.code in TRANSPORT_CODES:
                        raise
                    log.info("history read skipped: %s", err.code)

    def _read_history(self, client: CaoClient, bare: str, now: float) -> _History:
        info = self._conductor_of(bare)
        if info is None:
            raise ShelfError("not_found", "unknown session")
        rows = _slim(client.inbox_messages(info["id"], limit=100, order="desc"))
        hist = _History(rows, now, info["id"], info["last_active"])
        with self._lock:
            self._st.history[bare] = hist
        built = history_mod.rebuild(rows, self.live_ids(), session=bare)
        self._ledger.merge(history_mod.ledger_entries(bare, built), self._now_dt(now))
        return hist

    def _closed_view(self, bare: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            hist = self._st.history.get(bare)
            conductorless = (
                bare not in self._st.conductors and cao_session_name(bare) in self._st.views
            )
        if hist is None and not conductorless:
            return None
        # no conductor means no inbox to read: only the ledger remembers that session's helpers
        rows = hist.rows if hist is not None else []
        built = history_mod.rebuild(rows, self.live_ids(), session=bare)
        return history_mod.merge_with_ledger(
            built, self._ledger.entries_for(bare), live_ids=self.live_ids()
        )

    def _history_sessions(self) -> List[str]:
        """Bare names of every session read, with or without a conductor."""
        with self._lock:
            return [n[len(SESSION_PREFIX) :] for n in self._st.views]

    def history_counts(self) -> Dict[str, Optional[int]]:
        out: Dict[str, Optional[int]] = {}
        for bare in self._history_sessions():
            view = self._closed_view(bare)
            out[bare] = None if view is None else len(view["closed"])
        return out

    def history(self, bare: str) -> Dict[str, Any]:
        """The history document of one session, reading its inbox when the cache is old."""
        if not NAME_RE.fullmatch(bare):
            raise ShelfError("invalid_request", "invalid session name")
        now = self._clock()
        self._ensure_history(bare, now)
        view = self._closed_view(bare)
        if view is None:
            raise ShelfError("not_found", "no such session")
        return view

    def _ensure_history(self, bare: str, now: float) -> None:
        if self._conductor_of(bare) is None:
            if cao_session_name(bare) in self._st.views:
                return  # no conductor: nothing to read, the ledger is all there is
            raise ShelfError("not_found", "no such session")
        hist = self._st.history.get(bare)
        if hist is None or now - hist.read_at >= HISTORY_OPEN_MAX_AGE:
            self._read_history(self.client(), bare, now)

    def history_rows(self, bare: str, terminal_id: str) -> Dict[str, Any]:
        if not NAME_RE.fullmatch(bare) or not TERMINAL_ID_RE.fullmatch(terminal_id):
            raise ShelfError("invalid_request", "invalid session name or terminal id")
        view = self.history(bare)
        if terminal_id not in {c["terminal_id"] for c in view["closed"]}:
            raise ShelfError("not_found", "no such closed helper")
        info = self._conductor_of(bare)
        if info is None:
            raise ShelfError("not_found", "the conductor is gone, so its messages cannot be read")
        rows = self.client().inbox_messages(
            info["id"], limit=3, order="desc", sender_id=terminal_id
        )
        out = []
        for row in rows[:3]:
            status = row.get("status")
            out.append(
                {
                    "id": row.get("id") if isinstance(row.get("id"), int) else 0,
                    "created_at": str(row.get("created_at", ""))[: history_mod.TIME_CAP],
                    "status": status if status in history_mod.STATUSES else "delivered",
                    "text": history_mod.row_text(row.get("message")),
                }
            )
        return {"rows": out}

    def history_all(self) -> Dict[str, Any]:
        """Every session's closed helpers, from caches, reading at most a few old ones.

        At most ``HISTORY_READS_PER_TICK`` inbox reads and at most ``HISTORY_ALL_BUDGET_SECONDS``
        of CAO time per call, so an HTTP thread is never held for long. A read that fails or a
        budget that runs out leaves ``complete`` false; the sessions that were not refreshed show
        what the cache or the ledger knows.
        """
        start = self._clock()
        reads = 0
        pending = False
        stopped = False
        # never read first, then the oldest cache, so repeated calls work through a large fleet
        order = sorted(
            self._st.conductors,
            key=lambda b: -1e18 if b not in self._st.history else self._st.history[b].read_at,
        )
        for bare in order:
            hist = self._st.history.get(bare)
            if hist is not None and start - hist.read_at < HISTORY_OPEN_MAX_AGE:
                continue
            if stopped or self._clock() - start >= HISTORY_ALL_BUDGET_SECONDS:
                pending = True  # out of time, or CAO stopped answering: this one was not refreshed
                continue
            if reads >= HISTORY_READS_PER_TICK:
                pending = pending or hist is None  # an old cache still shows; "never read" does not
                continue
            reads += 1
            try:
                self._read_history(self.client(), bare, start)
            except ShelfError as err:
                pending = True
                stopped = err.code in TRANSPORT_CODES  # CAO is not answering: ask no more
                log.info("all-history read skipped: %s", err.code)
        closed: List[Dict[str, Any]] = []
        complete = not pending
        for bare in self._history_sessions():
            view = self._closed_view(bare)
            if view is None:
                complete = False
                continue
            complete = complete and view["complete"]
            for c in view["closed"]:
                closed.append(
                    {
                        "session": bare,
                        "terminal_id": c["terminal_id"],
                        "rows": c["rows"],
                        "last_at": c["last_at"],
                    }
                )
        closed.sort(key=lambda c: history_mod._when(c["last_at"]), reverse=True)
        return {"closed": closed, "complete": complete}

    # ------------------------------------------------------------------ views for other modules
    def live_ids(self) -> Set[str]:
        with self._lock:
            return {
                str(r["id"])
                for view in self._st.views.values()
                for r in view.rows
                if isinstance(r.get("id"), str)
            }

    def role_live_ids(self) -> Any:
        """The ids a ``caller_id`` may name: the live ones, or any id while a listed session has
        not been read yet (the caller may be in it)."""
        with self._lock:
            if any(n not in self._st.views for n in self._st.listed):
                return tiering.EVERY_ID
            return self.live_ids()

    def caller_ids(self) -> Dict[str, Optional[str]]:
        with self._lock:
            return dict(self._st.callers)

    def invalidate(self, bare: str) -> None:
        """Re-read this session on the next tick (after a write)."""
        with self._lock:
            view = self._st.views.get(cao_session_name(bare))
            if view is not None:
                view.read_at = -1e18
        self._wake.set()

    # ------------------------------------------------------------------ the projection
    def _cao_info(self, now: float) -> Dict[str, Any]:
        st = self._st
        err = st.failure or st.partial
        reachable = not (
            st.failure is not None and st.failure.code in ("cao_unreachable", "cao_timeout")
        )
        return {
            "reachable": reachable,
            "base_url": self._client.base_url if self._client else self._settings()["cao_url"],
            "version": st.version,
            "terminal_backend": st.backend,
            "error": err.inner() if err else None,
        }

    def _thresholds(self) -> Dict[str, float]:
        s = self._settings()
        return {"warm_hours": float(s["warm_hours"]), "stale_hours": float(s["stale_hours"])}

    def _publish(self, now: float) -> None:
        st = self._st
        with self._lock:
            # a listed session that has never been read is None (disables orphan marking)
            listed: Dict[str, Any] = {name: None for name in st.listed}
            for name, view in st.views.items():
                listed[name] = view.rows
            counts = self.history_counts()
            shelf = tiering.build_shelf(
                listed,
                dict(st.callers),
                self._now_dt(now),
                self._thresholds(),
                counts,
                cao=self._cao_info(now),
                hidden_sessions=st.hidden,
            )
            for s in shelf["sessions"]:
                current = st.views.get(s["cao_name"])
                if current is not None:
                    current.poll_class = s["poll_class"]
            self._decorate(shelf)
            self._store(shelf)

    def _decorate(self, shelf: Dict[str, Any]) -> None:
        """Put each session's ``relay`` field on (``null`` when it has none)."""
        view: Dict[str, Dict[str, Any]] = {}
        if self._relay_view is not None:
            try:
                view = self._relay_view()
            except Exception as exc:
                log.error("relay view failed: %s", type(exc).__name__)
        for s in shelf.get("sessions", []):
            s["relay"] = view.get(s["name"])

    def refresh_relays(self) -> None:
        """A relay changed: re-project the cached shelf (bumps the revision) without CAO."""
        with self._lock:
            snap = self._st.snapshot
            if snap is None:
                return
            shelf = dict(snap)
            shelf["sessions"] = [dict(s) for s in snap.get("sessions", [])]
            self._decorate(shelf)
            self._store(shelf)

    def _store(self, shelf: Dict[str, Any]) -> None:
        signature = tiering.change_signature(shelf)
        if signature != self._signature:
            self._signature = signature
            self._revision += 1
        shelf["revision"] = self._revision
        self._st.snapshot = shelf

    def _fail(self, err: ShelfError, now: float) -> None:
        with self._lock:
            st = self._st
            st.failure = err
            if st.snapshot is None:
                shelf = tiering.empty_shelf(
                    self._now_dt(now), self._thresholds(), self._cao_info(now)
                )
            else:
                shelf = dict(st.snapshot)
                shelf["cao"] = self._cao_info(now)
                shelf["stale_snapshot"] = True
                # generated_at is this clock reading; last_good_at stays the last good read, and the
                # page ages the snapshot from it on its own ticking clock
                shelf["generated_at"] = tiering._iso(self._now_dt(now))
            self._store(shelf)

    # ------------------------------------------------------------------ what the API serves
    def shelf(self, since: Optional[int] = None) -> Dict[str, Any]:
        """The cached shelf, or the ``unchanged`` stub when ``since`` is the current revision.

        Marks a client active. If the poller was idle, or nothing was read yet, it waits briefly for
        the next tick so a page that was away does not show old data as current.
        """
        was_idle = self.mark_active()
        with self._cond:
            if self.running() and (self._st.snapshot is None or was_idle):
                start = self._ticks
                self._cond.wait_for(lambda: self._ticks > start, timeout=FIRST_SHELF_WAIT)
            snap = self._st.snapshot
            if snap is None:
                err = self._st.failure or ShelfError(
                    "cao_timeout", "the first read of CAO is still running"
                )
                cao = {
                    "reachable": False,
                    "base_url": self._settings()["cao_url"],
                    "version": None,
                    "terminal_backend": None,
                    "error": err.inner(),
                }
                return tiering.empty_shelf(self._now_dt(), self._thresholds(), cao, revision=0)
            if since is not None and since == snap["revision"]:
                return {"revision": snap["revision"], "unchanged": True}
            out = dict(snap)
            out["snapshot_age_seconds"] = (
                max(0, int(self._clock() - self._st.good_at)) if self._st.good_at else 0
            )
            return out

    def peek(self) -> Optional[Dict[str, Any]]:
        """The cached shelf as it is, without marking a client active or waiting."""
        with self._lock:
            return self._st.snapshot

    def status(self) -> Dict[str, Any]:
        with self._lock:
            err = self._st.failure or self._st.partial
            last = self._last_tick_at
        return {
            "poller": {
                "running": self.running(),
                "interval_seconds": float(self._settings()["poll_seconds"]),
                "last_tick_at": (
                    None if last is None else self._now_dt(last).strftime("%Y-%m-%dT%H:%M:%SZ")
                ),
                "requests_last_minute": self.requests_last_minute(),
            },
            "cao_error": err.code if err else None,
        }
