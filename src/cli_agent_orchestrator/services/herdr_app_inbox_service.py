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


# --- Seams to the rest of CAO (patched in tests; imports local to avoid cycles) ---


def _teardown(terminal_id: str, end_reason: str) -> None:
    from cli_agent_orchestrator.services.herdr_inbox_service import (
        _retain_deferred_failure_tombstone,
    )
    from cli_agent_orchestrator.services.terminal_service import delete_terminal

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
        self._by_address: Dict[str, str] = {}
        self._last_state: Dict[str, TerminalStatus] = {}
        self._floor_seq = -1
        # One delivery in flight per terminal, off the event stream: delivery
        # types through terminal.send, which can wait ~60 s for a person's draft.
        self._delivering: Dict[str, "asyncio.Task[None]"] = {}
        self._deliver_again: Set[str] = set()

    def _managed_id(self, record: Any) -> Optional[str]:
        """CAO terminal id of one of this server's terminals, else None."""
        labels = record.get("labels") if isinstance(record, dict) else None
        if not isinstance(labels, dict):
            return None
        if labels.get("cao.managed") != "1" or labels.get("cao.home") != self._home:
            return None
        tid = labels.get("cao.terminal_id")
        return str(tid) if tid else None

    # Called by terminal_service after it creates a terminal.
    def register_terminal(self, terminal_id: str, pane_id: str, is_kiro: bool = False) -> None:
        self._by_address[pane_id] = terminal_id

    def unregister_terminal(self, terminal_id: str) -> None:
        for address in [a for a, t in self._by_address.items() if t == terminal_id]:
            del self._by_address[address]
        self._last_state.pop(terminal_id, None)

    def mapped(self) -> Dict[str, str]:
        return dict(self._by_address)

    def load_snapshot(self, snapshot: Dict[str, Any]) -> None:
        mapping: Dict[str, str] = {}
        for t in snapshot.get("terminals") or []:
            tid = self._managed_id(t)
            if tid and t.get("address"):
                mapping[str(t["address"])] = tid
        self._by_address = mapping
        self._floor_seq = int(snapshot.get("seq") or 0)

    async def start(self) -> None:
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
            except asyncio.CancelledError:
                for task in list(self._delivering.values()):
                    task.cancel()
                raise
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
            if etype == "restarted" and address not in self._by_address:
                # A kept agent was lost and replaced by a shell; CAO already tore it down.
                await self._kill(address)
            else:
                self._by_address[address] = tid
            return
        tid = self._by_address.get(address or "")
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

    async def _kill(self, address: str) -> None:
        try:
            await asyncio.to_thread(self._backend.kill_address, address)
        except Exception as exc:  # noqa: BLE001
            logger.warning("herdr-app: could not remove terminal %s: %s", address, exc)

    async def _on_status(self, tid: str, status: Any) -> None:
        state = status.get("state") if isinstance(status, dict) else None
        if state in ("working", "blocked"):
            _note_activity(tid)
        mapped = HC_STATUS_TO_TERMINAL_STATUS.get(state) if isinstance(state, str) else None
        if mapped is not None and self._last_state.get(tid) != mapped:
            self._last_state[tid] = mapped
            await asyncio.to_thread(_record_status, tid, mapped)
        if state in ("idle", "done"):
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
        """Drop CAO rows without a live terminal and kill our live terminals without a row."""
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
                except Exception as exc:  # noqa: BLE001
                    logger.warning("herdr-app reconcile skipped %s: %s", session, exc)
                    continue
                live_ids = {self._managed_id(t) for t in live}
                row_ids = {str(r["id"]) for r in rows}
                for row in rows:
                    if str(row["id"]) not in live_ids:
                        try:
                            _drop_ghost_row(str(row["id"]))
                        except Exception as exc:  # noqa: BLE001
                            logger.warning("herdr-app: could not drop row %s: %s", row["id"], exc)
                for t in live:
                    if self._managed_id(t) not in row_ids:
                        try:
                            self._backend.kill_address(t["address"])
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "herdr-app: could not kill orphan %s: %s", t["address"], exc
                            )
