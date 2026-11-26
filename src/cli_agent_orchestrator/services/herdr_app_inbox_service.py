"""HerdrAppInboxService: event inbox for the herdr-app backend.

Follows herdr-core's event stream for the CAO core: delivers inbox messages
when an agent reports idle or done, records status transitions, tears down
CAO terminals whose program finished or was lost, names spaces and tabs when
the app changes its layout, and reconciles CAO rows with live terminals.

Only terminals carrying ``cao.managed=1`` and this server's ``cao.home`` are
ours; another cao-server sharing the core owns the rest.
"""

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

from cli_agent_orchestrator.backends.herdr_app_backend import (
    HC_STATUS_TO_TERMINAL_STATUS,
    LAYOUT_DOC,
)
from cli_agent_orchestrator.clients.hc_rpc import HcSubscription
from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

SILENCE_LIMIT_S = 45.0  # three 15 s heartbeats
BACKOFF_START_S = 1
BACKOFF_MAX_S = 30
# After a native idle/done, providers/base.py keeps reporting PROCESSING for a
# 10 s flush wait, so the delivery that event triggers finds the terminal busy.
# Deliver once more after that wait instead of leaving the message to the 30 s
# reconciliation sweep.
SETTLE_RETRY_S = 10.5


# --- Seams to the rest of CAO (patched in tests; imports local to avoid cycles) ---


def _teardown(terminal_id: str, end_reason: str) -> None:
    """Tear down a CAO terminal whose program finished, unless CAO is already doing so.

    CAO's own deletes end in the same ``finished`` event. A single-terminal
    delete unregisters the terminal before killing it, so its event is never
    mapped. Session teardown kills first, but holds the session's lifecycle
    lock from ``kill_session`` until its rows are gone: taking that lock and
    finding the row still there means no CAO delete owns this terminal.
    """
    from cli_agent_orchestrator.clients import database
    from cli_agent_orchestrator.services.herdr_inbox_service import (
        _retain_deferred_failure_tombstone,
    )
    from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

    # This holds session_lifecycle_lock, which is not reentrant, so it calls
    # the unlocked inner teardown rather than the locked delete_terminal (#1468).
    from cli_agent_orchestrator.services.terminal_service import (
        _delete_terminal_locked as delete_terminal,
    )

    if not database.terminal_exists(terminal_id):
        return
    row = database.get_terminal_metadata(terminal_id)
    if row is None:
        return
    with session_lifecycle_lock(str(row["tmux_session"])):
        if not database.terminal_exists(terminal_id):
            return  # a session teardown deleted it while this waited
        if _retain_deferred_failure_tombstone(terminal_id):
            return
        delete_terminal(terminal_id, end_reason=end_reason)


def _drop_ghost_row(terminal_id: str) -> None:
    from cli_agent_orchestrator.services.herdr_inbox_service import (
        _delete_ghost_terminal_row,
        _retain_deferred_failure_tombstone,
    )

    if not _retain_deferred_failure_tombstone(terminal_id):
        _delete_ghost_terminal_row(terminal_id)


def _record_status(terminal_id: str, status: TerminalStatus) -> None:
    from cli_agent_orchestrator.clients.database import record_launch_status_event

    record_launch_status_event(terminal_id, status.value, datetime.now(timezone.utc))


def _note_activity(terminal_id: str) -> None:
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    status_monitor.note_native_activity(terminal_id)


def _rows_by_session() -> Dict[str, List[Dict[str, Any]]]:
    from cli_agent_orchestrator.clients.database import list_all_terminals

    sessions: Dict[str, List[Dict[str, Any]]] = {}
    for row in list_all_terminals():
        sessions.setdefault(str(row["tmux_session"]), []).append(row)
    return sessions


def _rows_for(session_name: str) -> List[Dict[str, Any]]:
    from cli_agent_orchestrator.clients.database import list_terminals_by_session

    return list_terminals_by_session(session_name)


class HerdrAppInboxService:
    def __init__(self, backend: Any, delivery_callback: Callable[[str], None]) -> None:
        self._backend = backend
        self._deliver = delivery_callback
        self._home = str(backend.home_label)
        self._subscribe: Callable[[], Any] = lambda: HcSubscription(backend.herdr_app_session)
        # terminal_service calls register/unregister from worker threads (the
        # API runs delete_terminal in asyncio.to_thread), so the two maps they
        # share with the loop are guarded by this lock. Task state below is
        # touched only on the loop (``self._loop``, set by start()).
        self._state_lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._by_address: Dict[str, str] = {}
        self._last_state: Dict[str, TerminalStatus] = {}
        self._floor_seq = -1
        # One delivery in flight per terminal, off the event stream: delivery
        # types through terminal.send, which can wait ~60 s for a person's draft.
        self._delivering: Dict[str, "asyncio.Task[None]"] = {}
        self._deliver_again: Set[str] = set()
        # One pending post-flush-wait delivery per terminal (SETTLE_RETRY_S).
        self._settling: Dict[str, "asyncio.Task[None]"] = {}

    def _managed_id(self, record: Any) -> Optional[str]:
        """CAO terminal id of one of this server's terminals, else None."""
        labels = record.get("labels") if isinstance(record, dict) else None
        if not isinstance(labels, dict):
            return None
        if labels.get("cao.managed") != "1" or labels.get("cao.home") != self._home:
            return None
        tid = labels.get("cao.terminal_id")
        return str(tid) if tid else None

    # Called by terminal_service, from any thread, after it creates a terminal.
    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None:
        with self._state_lock:
            self._by_address[pane_id] = terminal_id

    def unregister_terminal(self, terminal_id: str) -> None:
        """Forget a terminal; callable from any thread.

        The mapping is gone when this returns, so the ``finished`` event of a
        kill that follows it is ignored (``dismantle_terminal_runtime``
        unregisters before ``kill_window``). A pending retry is cancelled on
        the loop.
        """
        self._forget(terminal_id)
        self._on_loop(self._cancel_settle, terminal_id)

    def _forget(self, terminal_id: str) -> None:
        with self._state_lock:
            for address in [a for a, t in self._by_address.items() if t == terminal_id]:
                del self._by_address[address]
            self._last_state.pop(terminal_id, None)

    def _on_loop(self, fn: Callable[[str], None], arg: str) -> None:
        """Run ``fn(arg)`` now when on the service's loop (or it never started), else on it."""
        loop = self._loop
        try:
            running: Optional[asyncio.AbstractEventLoop] = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if loop is None or loop is running:
            fn(arg)
            return
        try:
            loop.call_soon_threadsafe(fn, arg)
        except RuntimeError:  # loop closed: its tasks are gone with it
            pass

    def _tid_at(self, address: str) -> Optional[str]:
        with self._state_lock:
            return self._by_address.get(address)

    def mapped(self) -> Dict[str, str]:
        with self._state_lock:
            return dict(self._by_address)

    def load_snapshot(self, snapshot: Dict[str, Any]) -> None:
        mapping: Dict[str, str] = {}
        for t in snapshot.get("terminals") or []:
            tid = self._managed_id(t)
            if tid and t.get("address"):
                mapping[str(t["address"])] = tid
        with self._state_lock:
            self._by_address = mapping
        self._floor_seq = int(snapshot.get("seq") or 0)

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await self._follow()
        except asyncio.CancelledError:
            # Cancelled anywhere: mid-stream, mid-subscribe, or in the backoff sleep.
            for task in [*self._delivering.values(), *self._settling.values()]:
                task.cancel()
            raise

    async def _follow(self) -> None:
        delay = BACKOFF_START_S
        while True:
            sub = None
            try:
                sub = await asyncio.to_thread(self._subscribe)
                self.load_snapshot(sub.snapshot)
                await asyncio.to_thread(self._reconcile)
                await asyncio.to_thread(self._backend.sync_layout_names)
                while True:
                    message = await asyncio.to_thread(sub.next_notification, SILENCE_LIMIT_S)
                    delay = BACKOFF_START_S  # the stream is alive again
                    await self.handle(message)
            except (EOFError, TimeoutError) as exc:
                logger.warning("herdr-app event stream ended (%r); reconnecting", exc)
            except Exception as exc:  # noqa: BLE001 - keep delivering after any failure
                logger.warning("herdr-app event stream failed: %s; reconnecting", exc)
            finally:
                if sub is not None:
                    try:
                        sub.close()
                    except Exception:  # noqa: BLE001 - already broken
                        pass
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX_S)

    async def handle(self, message: Dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "missed":
            self.load_snapshot(params)
            # Terminals that finished during the gap are in no snapshot and
            # sent no event that reached us: reconcile drops their rows.
            await asyncio.to_thread(self._reconcile)
            await asyncio.to_thread(self._backend.sync_layout_names)
            return
        if method != "event":
            return  # heartbeat or anything new
        seq = params.get("seq")
        if isinstance(seq, int) and seq <= self._floor_seq:
            return
        etype, address = params.get("type"), params.get("terminal")
        data = params.get("data") or {}
        if etype == "doc":
            if data.get("name") == LAYOUT_DOC:
                await asyncio.to_thread(self._backend.sync_layout_names)
            return
        if etype in ("created", "restarted", "labels"):
            record = data.get("record") if etype == "labels" else data
            tid = self._managed_id(record)
            if not (tid and address):
                return
            if etype == "restarted" and self._tid_at(address) is None:
                # A kept agent was lost and replaced by a shell; CAO already tore it down.
                await self._kill(address)
            else:
                with self._state_lock:
                    self._by_address[address] = tid
            return
        tid = self._tid_at(address or "")
        if tid is None:
            return
        if etype == "status":
            await self._on_status(tid, data.get("status"))
        elif etype in ("finished", "lost"):
            self.unregister_terminal(tid)
            reason = "terminal_finished" if etype == "finished" else "terminal_lost"
            try:
                await asyncio.to_thread(_teardown, tid, reason)
            except Exception as exc:  # noqa: BLE001 - one failed teardown must not stop the stream
                logger.error("Teardown failed for terminal %s: %s", tid, exc)
            # The terminal is no longer live, so the backend's label lookup in
            # kill_window cannot see it: remove it (and end a kept recovery) by address.
            await self._kill(str(address))
        elif etype == "removed":
            self.unregister_terminal(tid)
            # Removed outside CAO (`hc rm`): no kill_address closes its input.
            await asyncio.to_thread(self._backend.release_input, str(address))

    async def _kill(self, address: str) -> None:
        try:
            await asyncio.to_thread(self._backend.kill_address, address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("herdr-app: could not remove terminal %s: %s", address, exc)

    async def _on_status(self, tid: str, status: Any) -> None:
        state = status.get("state") if isinstance(status, dict) else None
        if state in ("working", "blocked"):
            self._cancel_settle(tid)
            _note_activity(tid)
        mapped = HC_STATUS_TO_TERMINAL_STATUS.get(state) if isinstance(state, str) else None
        if mapped is not None:
            with self._state_lock:
                changed = self._last_state.get(tid) != mapped
                self._last_state[tid] = mapped
            if changed:
                await asyncio.to_thread(_record_status, tid, mapped)
        if state in ("idle", "done"):
            self._schedule_delivery(tid)
            self._cancel_settle(tid)
            self._settling[tid] = asyncio.create_task(self._settle_then_deliver(tid))

    def _cancel_settle(self, tid: str) -> None:
        task = self._settling.pop(tid, None)
        if task is not None:
            task.cancel()

    async def _settle_then_deliver(self, tid: str) -> None:
        await asyncio.sleep(SETTLE_RETRY_S)
        if self._settling.get(tid) is asyncio.current_task():
            del self._settling[tid]
        self._schedule_delivery(tid)

    def _schedule_delivery(self, tid: str) -> None:
        if tid in self._delivering:
            self._deliver_again.add(tid)  # rerun once the current one ends
            return
        self._delivering[tid] = asyncio.create_task(self._delivery(tid))

    async def _delivery(self, tid: str) -> None:
        try:
            while True:
                try:
                    await asyncio.to_thread(self._deliver, tid)
                except Exception as exc:  # noqa: BLE001 - one failed delivery must not stop others
                    logger.error("Delivery failed for terminal %s: %s", tid, exc)
                if tid not in self._deliver_again:
                    return
                self._deliver_again.discard(tid)
        finally:
            self._delivering.pop(tid, None)

    async def drain(self) -> None:
        """Wait for every delivery in flight (tests; reruns included)."""
        while self._delivering:
            await asyncio.gather(*list(self._delivering.values()), return_exceptions=True)

    def _reconcile(self) -> None:
        """Drop CAO rows whose terminal finished here and kill our live terminals without a row.

        A row is dropped only when the core has a record for its terminal id
        under this server's ``cao.home`` and every such record is finished.
        Rows the core has never heard of (made by the tmux or herdr TUI
        backend, or by another core) are not ours and stay.
        """
        from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock

        try:
            sessions = set(_rows_by_session())
            sessions |= {
                t["labels"].get("cao.session")
                for t in self._backend.list_managed()
                if self._managed_id(t)
            }
        except Exception as exc:  # noqa: BLE001 - unknown is not absence
            logger.warning("herdr-app reconcile skipped: %s", exc)
            return
        for session in sorted(s for s in sessions if s):
            with session_lifecycle_lock(session):
                try:
                    rows = _rows_for(session)
                    live = [
                        t
                        for t in self._backend.list_managed()
                        if self._managed_id(t) and t["labels"].get("cao.session") == session
                    ]
                    states: Dict[str, List[Any]] = {}
                    for t in self._backend.list_managed(include_finished=True):
                        tid = self._managed_id(t)
                        if tid:
                            states.setdefault(tid, []).append(t.get("state"))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("herdr-app reconcile skipped %s: %s", session, exc)
                    continue
                live_ids = {self._managed_id(t) for t in live}
                row_ids = {str(r["id"]) for r in rows}
                for row in rows:
                    rid = str(row["id"])
                    known = states.get(rid)
                    # No record: not a terminal of this core. Any record not
                    # plainly finished (live, or a state we don't know) keeps it.
                    if rid in live_ids or not known or any(s != "finished" for s in known):
                        continue
                    try:
                        _drop_ghost_row(rid)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("herdr-app: could not drop row %s: %s", rid, exc)
                for t in live:
                    if self._managed_id(t) not in row_ids:
                        try:
                            self._backend.kill_address(t["address"])
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "herdr-app: could not kill orphan %s: %s", t["address"], exc
                            )
