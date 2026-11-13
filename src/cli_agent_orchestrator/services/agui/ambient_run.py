"""AG-UI 1.0 conformant encoding of the ambient fleet stream (``/agui/v1/stream``).

The ambient stream is one AG-UI run per connection (spec 1.0 events/lifecycle):

- it opens with ``RUN_STARTED`` (with ``protocolVersion``), never anything else;
- each fleet session and each terminal is a subagent invocation
  (``SUBAGENT_STARTED`` / ``SUBAGENT_FINISHED`` / ``SUBAGENT_ERROR``, events/subagents),
  announced before anything is attributed to it, parents before children;
- every event is a stock SDK model (``type`` in the payload, camelCase fields,
  integer millisecond ``timestamp``) with CAO's own envelope under ``metadata.cao``;
- a deliberate stop (server shutdown, a ``maxSeconds`` limit, the source ending)
  closes every open tool call and invocation, then ends the run with the
  cancelled outcome; a bus overflow ends it with ``RUN_ERROR``; nothing follows
  either.

This is an EDGE layer. It reads the raw CAO event records and leaves the internal
``agui_stream.to_agui_event`` frame and ``ToolCallLifecycleTracker``, which the L2
constructs and the run plane's live projection consume, as they were, with one
exception: an ``OrchestrationType`` member in a record's ``orchestration_type`` is
now its plain value (``"assign"``) in that frame, so no wire name is the enum's
repr (#1292). In particular, the tracker's synthesized ``TOOL_CALL_RESULT`` for a2a delegations is
not reproduced here: ``TOOL_CALL_END`` closes the call, and a result MAY never
arrive in the stream (events/tool-calls), whereas each result would mint a tool
message in the consumer's history.

A killed terminal or session ends with ``SUBAGENT_FINISHED`` carrying no outcome and
``metadata.cao.endReason = "killed"``: AG-UI 1.0's subagent outcome is only
success | suspended, so a stock consumer reads it as success.
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional, Set, Tuple

from cli_agent_orchestrator.services.agui.attribution import (
    subagent_run_id_for_session,
    subagent_run_id_for_terminal,
)
from cli_agent_orchestrator.services.agui.base import BoundedSeen
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_CUSTOM,
    CAO_GENERATIVE_UI_EVENT_NAME,
    SESSION_SUBAGENT_RUN_ID_KEY,
    _extract_ui_intent,
    to_agui_event,
)

logger = logging.getLogger(__name__)

# The default threadId for GET connections that send no ?threadId=: stable for the
# life of the server process, so a reconnect lands on the same thread.
DEFAULT_THREAD_ID = f"cao-fleet-{uuid.uuid4()}"

# Machine-readable RUN_ERROR codes (an open string in AG-UI 1.0).
RUN_ERROR_OVERFLOW = "cao.stream_overflow"
RUN_ERROR_INTERNAL = "cao.stream_error"

_TOOL_CALL_ORCHESTRATION = ("handoff", "assign")


def _plain(value: Any) -> Any:
    """An enum's value, or the value itself.

    ``OrchestrationType`` is a ``(str, Enum)``, so ``str(member)`` is
    ``"OrchestrationType.ASSIGN"``. A record can carry the member (the in-process
    event log keeps what the publisher gave it) or a plain string.
    """
    return getattr(value, "value", value)


def _is_call_record(rec: Dict[str, Any]) -> bool:
    kind = rec.get("kind")
    if kind == "a2a_delegation":
        return True
    detail = rec.get("detail") or {}
    return (
        kind == "handoff" and _plain(detail.get("orchestration_type")) in _TOOL_CALL_ORCHESTRATION
    )


def _call_sender(rec: Dict[str, Any]) -> Any:
    return (rec.get("detail") or {}).get("sender") or rec.get("terminal_id")


def _open_calls(
    history: Iterable[Dict[str, Any]], before_id: Optional[str]
) -> List[Dict[str, Any]]:
    """The call records still open just before ``before_id``, oldest first.

    Walks ``history`` (oldest first) up to, and not including, the record with id
    ``before_id``, keeping what the live run would still hold open: a call record
    opens a call on its receiver and supersedes an older one there, and a
    ``completion`` or ``error`` of a terminal closes the call it received and every
    call it sent (the live run ends a call at its receiver's close or its owner's).
    """
    open_: Dict[Any, Tuple[int, Dict[str, Any]]] = {}
    for i, rec in enumerate(history):
        if before_id is not None and str(rec.get("id")) == before_id:
            break
        if _is_call_record(rec):
            receiver = (rec.get("detail") or {}).get("receiver")
            open_[receiver or f"id:{rec.get('id')}"] = (i, rec)
        elif rec.get("kind") in ("completion", "error") and rec.get("terminal_id"):
            closed = rec["terminal_id"]
            open_.pop(closed, None)
            for key in [k for k, (_, call) in open_.items() if _call_sender(call) == closed]:
                del open_[key]
    return [rec for _, rec in sorted(open_.values(), key=lambda item: item[0])]


Item = Tuple[Optional[str], Any]  # (SSE id or None, SDK event)


# ---------------------------------------------------------------------------
# Server-stop signal. A module-level threading.Event records that a stop was
# requested; each live generator registers its own asyncio.Event on its own loop,
# which request_stop() sets through loop.call_soon_threadsafe.
# ---------------------------------------------------------------------------

_stop_requested = threading.Event()
# Reentrant: request_stop() runs inside uvicorn's signal handler on the loop
# thread, which may already hold this lock while registering a stream.
_waiters_lock = threading.RLock()
_waiters: Set[Tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()


def request_stop(*, sticky: bool = True) -> None:
    """Ask every live ambient stream to close cleanly (RUN_FINISHED cancelled).

    ``sticky`` (the server-exit path) also makes streams that open afterwards close
    at once. The lifespan backstop passes ``sticky=False``: it only signals the
    streams open at that moment, so an app whose lifespan restarts in the same
    process (tests) is not left in a stopped state.
    """
    if sticky:
        _stop_requested.set()
    with _waiters_lock:
        waiters = list(_waiters)
    for loop, event in waiters:
        try:
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:  # pragma: no cover - loop already closed
            pass


def reset_stop() -> None:
    """Clear a previous stop request (test isolation; a fresh server process starts clear)."""
    _stop_requested.clear()


def _register_stop_waiter() -> Tuple[asyncio.AbstractEventLoop, asyncio.Event]:
    loop = asyncio.get_running_loop()
    event = asyncio.Event()
    entry = (loop, event)
    with _waiters_lock:
        _waiters.add(entry)
    if _stop_requested.is_set():
        event.set()
    return entry


def _unregister_stop_waiter(entry: Tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
    with _waiters_lock:
        _waiters.discard(entry)


_HOOK_MARKER = "_cao_ambient_stop_hook"


def install_uvicorn_exit_hook() -> None:
    """Make uvicorn's SIGINT/SIGTERM handler ask the ambient streams to close first.

    uvicorn waits for open connections BEFORE it runs the lifespan shutdown
    (``Server.shutdown``), and an ambient stream never ends on its own, so the
    signal has to reach the streams at the start of the shutdown for them to send
    their cancelled close and finish. Called once by ``cao-server``'s ``main()``
    just before ``uvicorn.run``; wraps ``uvicorn.Server.handle_exit`` (the method
    uvicorn's signal capture calls) and is idempotent.
    """
    import uvicorn

    original = uvicorn.Server.handle_exit
    if getattr(original, _HOOK_MARKER, False):
        return

    def handle_exit(self: Any, sig: int, frame: Any) -> None:
        request_stop()
        original(self, sig, frame)

    setattr(handle_exit, _HOOK_MARKER, True)
    uvicorn.Server.handle_exit = handle_exit  # type: ignore[method-assign]


def install_signal_stop_hook() -> None:
    """Make the installed SIGINT/SIGTERM handlers ask the ambient streams to close first.

    For a launch that never runs ``cao-server``'s ``main()``, such as
    ``uvicorn cli_agent_orchestrator.api.main:app``. Called from the lifespan
    startup, which uvicorn runs after it installs its own handlers, so it wraps
    them in place; uvicorn 0.29+ puts its originals back when it stops. A handler that
    already stops the streams (``install_uvicorn_exit_hook``'s) is left alone.
    Main thread only, where signal handlers live, and idempotent.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    for sig in (signal.SIGINT, signal.SIGTERM):
        current = signal.getsignal(sig)
        if not callable(current) or getattr(current, _HOOK_MARKER, False):
            continue

        def handler(signum: int, frame: Any, _next: Callable[..., Any] = current) -> None:
            request_stop()
            _next(signum, frame)

        setattr(handler, _HOOK_MARKER, True)
        signal.signal(sig, handler)


async def until_stop(source: Any) -> AsyncIterator[Any]:
    """Yield from ``source`` until the server-stop signal, then end as if it had ended.

    The legacy ``framing=cao-legacy`` stream reads its bus through this, so a stop
    ends it the way its source ending does, instead of holding uvicorn's shutdown.
    """
    waiter = _register_stop_waiter()
    _, stop_event = waiter
    it = source.__aiter__()
    pending: Optional["asyncio.Future[Any]"] = None
    stop_wait: Optional["asyncio.Future[Any]"] = None
    try:
        while True:
            pending = asyncio.ensure_future(it.__anext__())
            if stop_wait is None:
                stop_wait = asyncio.ensure_future(stop_event.wait())
            done, _ = await asyncio.wait({pending, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if stop_wait in done:
                return
            try:
                item = pending.result()
            except StopAsyncIteration:
                return
            yield item
    finally:
        for fut in (pending, stop_wait):
            if fut is not None and not fut.done():
                fut.cancel()
        _unregister_stop_waiter(waiter)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _epoch_ms(value: Any) -> Optional[int]:
    """ISO-8601 string (or datetime) to integer milliseconds since the epoch."""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value)
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _compact(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _models() -> Any:
    from ag_ui import core

    return core


# ---------------------------------------------------------------------------
# The per-connection run
# ---------------------------------------------------------------------------


class AmbientRun:
    """Fold CAO event records into one conformant AG-UI run.

    Methods return ``[(sse_id, event), ...]``; ``sse_id`` is set on frames that come
    from a record (the CAO ``Last-Event-ID`` extension) and ``None`` otherwise.
    """

    def __init__(
        self,
        thread_id: str,
        run_id: str,
        *,
        incarnation_of: Callable[[str], Optional[str]],
        state_baseline_known: bool = True,
        since: Optional[str] = None,
        terminal_known: Optional[Callable[[str], bool]] = None,
        terminal_label: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
        call_history: Optional[Callable[[], Iterable[Dict[str, Any]]]] = None,
    ) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self._incarnation_of = incarnation_of
        # Whether CAO knows a terminal this run has not seen yet. A sender or
        # terminal_id that is neither open on the run nor known to CAO (a label
        # such as "operator" or "supervisor") never becomes a subagent.
        self._terminal_known = terminal_known
        # The ``agent_profile`` and ``provider`` of a known terminal, to name one
        # first met mid-replay, before the snapshot that has the names.
        self._terminal_label = terminal_label
        # The whole retained event log, oldest first. A reconnect's replay starts at
        # its cursor, so a call made before it is not in the replay; when the receiver's
        # completion is, this finds the call so the run can announce it before closing it.
        self._call_history = call_history
        self._state_baseline_known = state_baseline_known
        self._since = since
        self._snapshot_sent = False
        self.closed = False
        # session name -> active session invocation id
        self._sessions: Dict[str, str] = {}
        self._session_counter: Dict[str, int] = {}
        # The record feed() is mapping; a session it opens takes its id from it.
        self._current_record: Optional[Dict[str, Any]] = None
        # terminal id -> (invocation id, session name)
        self._terminals: Dict[str, Tuple[str, str]] = {}
        self._finished: Set[str] = set()
        # tool_call_id -> {"owner": sid|None, "receiver": tid|None, "name": str}
        self._calls: Dict[str, Dict[str, Any]] = {}
        # receiver terminal id -> tool_call_id (one open call per receiver)
        self._call_by_receiver: Dict[str, str] = {}
        # Every call id this run has opened, so a call it already opened and ended is
        # never announced a second time.
        self._seen_calls = BoundedSeen()
        # True while a call record read back from the log is being mapped: it must not
        # itself go looking for the call it superseded.
        self._recovering = False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> List[Item]:
        core = _models()
        cao = _compact({"stream": "ambient", "since": self._since})
        return [
            (
                None,
                core.RunStartedEvent(
                    thread_id=self.thread_id,
                    run_id=self.run_id,
                    protocol_version=core.PROTOCOL_VERSION,
                    metadata={"cao": cao},
                ),
            )
        ]

    def snapshot(self, snapshot: Dict[str, Any]) -> List[Item]:
        core = _models()
        self._snapshot_sent = True
        items: List[Item] = [(None, core.StateSnapshotEvent(snapshot=snapshot))]
        items.extend(self._announce_live(snapshot))
        return items

    def delta(self, ops: List[Dict[str, Any]]) -> List[Item]:
        if not ops or not self._may_send_delta():
            return []
        return [(None, _models().StateDeltaEvent(delta=ops))]

    def close_cancelled(self, reason: str = "stream_closed") -> List[Item]:
        core = _models()
        items = self._close_everything(reason)
        items.append(
            (
                None,
                core.RunFinishedEvent(
                    thread_id=self.thread_id,
                    run_id=self.run_id,
                    outcome=core.RunFinishedCancelledOutcome(type="cancelled"),
                    metadata={"cao": {"endReason": reason}},
                ),
            )
        )
        self.closed = True
        return items

    def close_error(self, code: str, message: str) -> List[Item]:
        self.closed = True
        return [(None, _models().RunErrorEvent(message=message, code=code))]

    # -- records -------------------------------------------------------------

    def feed(self, record: Dict[str, Any]) -> List[Item]:
        """Map one CAO record to conformant events, synthesizing what must precede them."""
        if self.closed:
            return []
        self._current_record = record
        try:
            events = self._map(record)
        finally:
            self._current_record = None
        rid = record.get("id")
        if rid is None or not events:
            return [(None, e) for e in events]
        rid = str(rid)
        last = len(events) - 1
        return [(rid if i == last else f"{rid}.{i}", e) for i, e in enumerate(events)]

    def _map(self, record: Dict[str, Any]) -> List[Any]:
        core = _models()
        kind = record.get("kind")
        detail: Dict[str, Any] = record.get("detail") or {}
        terminal_id = record.get("terminal_id")
        session = record.get("session_name") or ""
        ts = _epoch_ms(record.get("timestamp"))
        envelope = self._envelope(record)
        out: List[Any] = []

        # Generative UI first, as in agui_stream.to_agui_event.
        if _extract_ui_intent(record) is not None:
            agui_type, data = to_agui_event(record)
            owner = self._owner_for(record, out)
            if agui_type == AGUI_CUSTOM:
                out.append(
                    core.CustomEvent(
                        name=CAO_GENERATIVE_UI_EVENT_NAME,
                        value=data.get("value"),
                        timestamp=ts,
                        metadata={"cao": envelope},
                        **self._attr(owner),
                    )
                )
            else:  # AGUI_RAW: the off-list refusal, kept inspectable but never rendered
                out.append(self._raw(record, data, ts, envelope, owner))
            return out

        if kind == "launch":
            if terminal_id is None or detail.get("event_type") == "post_create_session":
                out.extend(self._open_session(session, envelope, ts, record))
            else:
                out.extend(
                    self._open_terminal(
                        terminal_id,
                        session,
                        name=detail.get("agent_name") or detail.get("provider") or "terminal",
                        provider=detail.get("provider"),
                        envelope=envelope,
                        ts=ts,
                    )
                )
            return out

        if kind == "completion":
            if terminal_id is None or detail.get("event_type") == "post_kill_session":
                out.extend(self._close_session(session, "killed", envelope, ts))
            else:
                out.extend(self._close_terminal(terminal_id, session, "killed", envelope, ts))
            return out

        if kind in ("handoff", "a2a_delegation"):
            orchestration = _plain(detail.get("orchestration_type"))
            sender = detail.get("sender") or terminal_id
            receiver = detail.get("receiver")
            cao = _compact(
                {
                    **envelope,
                    "sender": sender,
                    "receiver": receiver,
                    "orchestrationType": orchestration,
                }
            )
            owner = self._owner_for({**record, "terminal_id": sender}, out)
            if kind == "a2a_delegation" or orchestration in _TOOL_CALL_ORCHESTRATION:
                name = "a2a_delegation" if kind == "a2a_delegation" else str(orchestration)
                if receiver and receiver not in self._call_by_receiver and not self._recovering:
                    # A call this run never saw may be open on the receiver: announce it
                    # so _open_call ends it as superseded, as the live run did.
                    out.extend(self._announce_unseen_calls(str(record.get("id")), receiver, None))
                out.extend(self._open_call(str(record.get("id")), name, owner, receiver, cao, ts))
            else:
                out.append(
                    core.CustomEvent(
                        name="cao.message_dispatched",
                        value=_compact(
                            {
                                "sender": sender,
                                "receiver": receiver,
                                "orchestrationType": orchestration,
                            }
                        ),
                        timestamp=ts,
                        metadata={"cao": envelope},
                        **self._attr(owner),
                    )
                )
            return out

        if kind == "file_mod":
            if not self._may_send_delta():
                return out
            agui_type, data = to_agui_event(record)
            owner = self._owner_for(record, out)
            out.append(
                core.StateDeltaEvent(
                    delta=data.get("delta") or [],
                    timestamp=ts,
                    metadata={"cao": envelope},
                    **self._attr(owner),
                )
            )
            return out

        if kind == "error":
            event_type = detail.get("event_type") or "error"
            if terminal_id:
                out.extend(self._error_terminal(terminal_id, session, event_type, envelope, ts))
            else:
                out.append(
                    core.CustomEvent(
                        name="cao.error",
                        value=_compact({"eventType": event_type, "sessionName": session or None}),
                        timestamp=ts,
                        metadata={"cao": envelope},
                    )
                )
            return out

        # "other" and anything unmapped: RAW keeps it inspectable.
        agui_type, data = to_agui_event(record)
        owner = self._owner_for(record, out) if terminal_id else None
        out.append(self._raw(record, data, ts, envelope, owner))
        return out

    # -- invocations -----------------------------------------------------------

    def _session_id(self, session: str, record: Optional[Dict[str, Any]] = None) -> str:
        """The session invocation's ``subagentRunId``, deterministic per incarnation.

        Preference order, each stable across reconnects and replays:

        1. ``detail.session_subagent_run_id`` stamped on the record when it was
           logged (the record's own incarnation, already hashed);
        2. the hash of the session's current incarnation pointer, while it has not
           been finished on this run;
        3. a hash seeded by the event id of the record that opened the invocation
           (a relaunch whose earlier incarnation is no longer current).
        """
        record = record if record is not None else self._current_record
        detail = (record or {}).get("detail") or {}
        stamped = detail.get(SESSION_SUBAGENT_RUN_ID_KEY) if isinstance(detail, dict) else None
        if isinstance(stamped, str) and stamped and stamped not in self._finished:
            return stamped
        incarnation = None
        try:
            incarnation = self._incarnation_of(session) if session else None
        except Exception:  # pragma: no cover - a DB hiccup must not break the stream
            logger.debug("ambient_run: incarnation lookup failed for %s", session, exc_info=True)
        if incarnation:
            sid = subagent_run_id_for_session(incarnation)
            if sid not in self._finished:
                return sid
        rid = (record or {}).get("id")
        if rid is not None:
            return subagent_run_id_for_session(f"event:{rid}")
        n = self._session_counter.get(session, 0) + 1
        self._session_counter[session] = n
        return subagent_run_id_for_session(f"{self.run_id}:{session}:{n}")

    def _open_session(
        self,
        session: str,
        envelope: Dict[str, Any],
        ts: Optional[int],
        record: Optional[Dict[str, Any]] = None,
    ) -> List[Any]:
        if session in self._sessions:
            return []
        sid = self._session_id(session, record)
        self._sessions[session] = sid
        cao = _compact({**envelope, "sessionName": session or None})
        return [
            _models().SubagentStartedEvent(
                subagent_run_id=sid,
                name=session or "session",
                timestamp=ts,
                metadata={"cao": cao},
            )
        ]

    def _open_terminal(
        self,
        terminal_id: str,
        session: str,
        *,
        name: str,
        provider: Optional[str],
        envelope: Dict[str, Any],
        ts: Optional[int],
    ) -> List[Any]:
        if terminal_id in self._terminals:
            return []
        sid = subagent_run_id_for_terminal(terminal_id)
        if sid in self._finished:
            return []  # one invocation per terminal; a finished one is never re-announced
        out = self._open_session(session, {}, ts) if session else []
        parent = self._sessions.get(session)
        self._terminals[terminal_id] = (sid, session)
        cao = _compact(
            {
                **envelope,
                "terminalId": terminal_id,
                "sessionName": session or None,
                "provider": provider,
            }
        )
        kwargs: Dict[str, Any] = {"parent_subagent_run_id": parent} if parent else {}
        out.append(
            _models().SubagentStartedEvent(
                subagent_run_id=sid, name=name, timestamp=ts, metadata={"cao": cao}, **kwargs
            )
        )
        return out

    def _owner_for(self, record: Dict[str, Any], out: List[Any]) -> Optional[str]:
        """The invocation an event belongs to, announcing it first when needed.

        A terminal owns the event when the run has it open, or when CAO knows it
        and its invocation has not finished on this run (then it is announced
        first). Anything else (a label such as ``"operator"``, an id CAO does not
        know, or a terminal whose invocation already finished) falls back to the
        record's open session invocation, or to no tag: a finished id is never
        reused and no phantom subagent is announced. The caller keeps the raw
        sender in ``metadata.cao``.
        """
        terminal_id = record.get("terminal_id")
        session = record.get("session_name") or ""
        if terminal_id:
            if terminal_id in self._terminals:
                return self._terminals[terminal_id][0]
            sid = subagent_run_id_for_terminal(terminal_id)
            if sid not in self._finished and self._is_known_terminal(terminal_id):
                label = self._label_of(terminal_id)
                out.extend(
                    self._open_terminal(
                        terminal_id,
                        session,
                        name=label.get("agent_profile") or label.get("provider") or "terminal",
                        provider=label.get("provider"),
                        envelope={},
                        ts=None,
                    )
                )
                return sid
        return self._sessions.get(session)

    def _is_known_terminal(self, terminal_id: str) -> bool:
        if self._terminal_known is None:
            return False
        try:
            return bool(self._terminal_known(terminal_id))
        except Exception:  # a DB hiccup must not break the stream; attribute conservatively
            logger.debug("ambient_run: terminal lookup failed for %s", terminal_id, exc_info=True)
            return False

    def _label_of(self, terminal_id: str) -> Dict[str, Any]:
        if self._terminal_label is None:
            return {}
        try:
            return self._terminal_label(terminal_id) or {}
        except Exception:  # a DB hiccup must not break the stream; keep the generic name
            logger.debug("ambient_run: label lookup failed for %s", terminal_id, exc_info=True)
            return {}

    def _announce_call(self, record: Dict[str, Any]) -> List[Any]:
        """Map one call record read back from the log, unless the run already holds its id.

        A mapping failure is logged and skipped, so the close that asked for it still
        ends the calls the run does hold.
        """
        rid = str(record.get("id"))
        if rid in self._seen_calls or rid in self._calls:
            return []
        self._recovering = True
        try:
            return self._map(record)
        except Exception:  # a bad historical record must not cost the real close
            logger.warning("ambient_run: could not map a call record from the log", exc_info=True)
            return []
        finally:
            self._recovering = False

    def _announce_unseen_calls(
        self, before_id: Optional[str], receiver: Optional[str], sender: Optional[str]
    ) -> List[Any]:
        """Announce the calls open before ``before_id`` that involve ``receiver`` or ``sender``.

        The calls' records predate the replay cursor, so this run never opened them and
        their END would otherwise be lost. Each is mapped as the live path would (owner
        included), with its own id, so the END that follows has a START to close.
        """
        if self._call_history is None:
            return []
        try:
            records = _open_calls(self._call_history(), before_id)
        except Exception:  # a ring hiccup must not break the stream; close without the START
            logger.warning("ambient_run: call history lookup failed", exc_info=True)
            return []
        out: List[Any] = []
        for rec in records:
            if (receiver is not None and (rec.get("detail") or {}).get("receiver") == receiver) or (
                sender is not None and _call_sender(rec) == sender
            ):
                out.extend(self._announce_call(rec))
        return out

    def _announce_pre_window_call(self, terminal_id: str) -> List[Any]:
        """Announce what a closing terminal was serving or had sent, when the replay skipped it."""
        current = (self._current_record or {}).get("id")
        return self._announce_unseen_calls(
            None if current is None else str(current), terminal_id, terminal_id
        )

    def _close_calls_for_terminal(self, terminal_id: str, ts: Optional[int]) -> List[Any]:
        out = self._announce_pre_window_call(terminal_id)
        sid = self._terminals.get(terminal_id, (None, ""))[0]
        ids = [
            cid
            for cid, call in self._calls.items()
            if call["receiver"] == terminal_id or (sid is not None and call["owner"] == sid)
        ]
        out.extend(self._end_call(cid, ts, "completion") for cid in ids)
        return out

    def _close_terminal(
        self,
        terminal_id: str,
        session: str,
        reason: str,
        envelope: Dict[str, Any],
        ts: Optional[int],
    ) -> List[Any]:
        out: List[Any] = []
        if terminal_id not in self._terminals:
            sid = subagent_run_id_for_terminal(terminal_id)
            if sid in self._finished:
                return out
            # A close for an invocation launched before this run (a replay window, or
            # a connection opened mid-life): announce it first, then close it.
            out.extend(
                self._open_terminal(
                    terminal_id, session, name="terminal", provider=None, envelope={}, ts=ts
                )
            )
        out.extend(self._close_calls_for_terminal(terminal_id, ts))
        sid, _ = self._terminals.pop(terminal_id)
        self._finished.add(sid)
        cao = _compact({**envelope, "terminalId": terminal_id, "endReason": reason})
        out.append(
            _models().SubagentFinishedEvent(
                subagent_run_id=sid, timestamp=ts, metadata={"cao": cao}
            )
        )
        return out

    def _error_terminal(
        self,
        terminal_id: str,
        session: str,
        event_type: str,
        envelope: Dict[str, Any],
        ts: Optional[int],
    ) -> List[Any]:
        out: List[Any] = []
        if terminal_id not in self._terminals:
            sid = subagent_run_id_for_terminal(terminal_id)
            if sid in self._finished:
                return out
            out.extend(
                self._open_terminal(
                    terminal_id, session, name="terminal", provider=None, envelope={}, ts=ts
                )
            )
        out.extend(self._close_calls_for_terminal(terminal_id, ts))
        sid, _ = self._terminals.pop(terminal_id)
        self._finished.add(sid)
        cao = _compact({**envelope, "terminalId": terminal_id})
        out.append(
            _models().SubagentErrorEvent(
                subagent_run_id=sid,
                message=f"terminal error: {event_type}",
                code=event_type,
                timestamp=ts,
                metadata={"cao": cao},
            )
        )
        return out

    def _close_session(
        self, session: str, reason: str, envelope: Dict[str, Any], ts: Optional[int]
    ) -> List[Any]:
        out: List[Any] = []
        if session not in self._sessions:
            out.extend(self._open_session(session, {}, ts))
        for terminal_id, (_, owner_session) in list(self._terminals.items()):
            if owner_session == session:
                out.extend(self._close_terminal(terminal_id, session, reason, {}, ts))
        sid = self._sessions.pop(session)
        # Calls the session itself owns (a sender that is not a terminal is kept on
        # the receiver's session) close before the session finishes. The loop above
        # ends a call only through a terminal the run has open, so this one is for a
        # receiver the run never announced, such as a terminal that started before
        # the replay window.
        for cid in [c for c, call in self._calls.items() if call["owner"] == sid]:
            out.append(self._end_call(cid, ts, "completion"))
        self._finished.add(sid)
        cao = _compact({**envelope, "sessionName": session or None, "endReason": reason})
        out.append(
            _models().SubagentFinishedEvent(
                subagent_run_id=sid, timestamp=ts, metadata={"cao": cao}
            )
        )
        return out

    def _close_everything(self, reason: str) -> List[Item]:
        out: List[Any] = []
        for cid in list(self._calls):
            out.append(self._end_call(cid, None, "stream_closed"))
        for terminal_id, (sid, _) in list(self._terminals.items()):
            self._terminals.pop(terminal_id)
            self._finished.add(sid)
            out.append(
                _models().SubagentFinishedEvent(
                    subagent_run_id=sid,
                    metadata={"cao": {"terminalId": terminal_id, "endReason": reason}},
                )
            )
        for session, sid in list(self._sessions.items()):
            self._sessions.pop(session)
            self._finished.add(sid)
            out.append(
                _models().SubagentFinishedEvent(
                    subagent_run_id=sid,
                    metadata={"cao": {"sessionName": session, "endReason": reason}},
                )
            )
        return [(None, e) for e in out]

    def _announce_live(self, snapshot: Dict[str, Any]) -> List[Item]:
        out: List[Any] = []
        for s in snapshot.get("sessions") or []:
            name = s.get("name") or s.get("id")
            if name and name not in self._sessions:
                out.extend(self._open_session(name, {}, None))
        for t in snapshot.get("terminals") or []:
            tid = t.get("id")
            if not tid or tid in self._terminals:
                continue
            out.extend(
                self._open_terminal(
                    tid,
                    t.get("session_name") or "",
                    name=t.get("agent_profile") or t.get("provider") or "terminal",
                    provider=t.get("provider"),
                    envelope={},
                    ts=None,
                )
            )
        return [(None, e) for e in out]

    # -- tool calls ------------------------------------------------------------

    def _open_call(
        self,
        call_id: str,
        name: str,
        owner: Optional[str],
        receiver: Optional[str],
        cao: Dict[str, Any],
        ts: Optional[int],
    ) -> List[Any]:
        out: List[Any] = []
        if receiver and receiver in self._call_by_receiver:
            out.append(self._end_call(self._call_by_receiver[receiver], ts, "superseded"))
        if call_id in self._calls:
            return out
        self._calls[call_id] = {"owner": owner, "receiver": receiver, "name": name}
        self._seen_calls.add(call_id)
        if receiver:
            self._call_by_receiver[receiver] = call_id
        out.append(
            _models().ToolCallStartEvent(
                tool_call_id=call_id,
                tool_call_name=name,
                timestamp=ts,
                metadata={"cao": cao},
                **self._attr(owner),
            )
        )
        return out

    def _end_call(self, call_id: str, ts: Optional[int], closed_by: str) -> Any:
        call = self._calls.pop(call_id)
        if call["receiver"] and self._call_by_receiver.get(call["receiver"]) == call_id:
            del self._call_by_receiver[call["receiver"]]
        return _models().ToolCallEndEvent(
            tool_call_id=call_id,
            timestamp=ts,
            metadata={"cao": {"closedBy": closed_by}},
            **self._attr(call["owner"]),
        )

    # -- misc ------------------------------------------------------------------

    def _may_send_delta(self) -> bool:
        return self._snapshot_sent or self._state_baseline_known

    @staticmethod
    def _attr(owner: Optional[str]) -> Dict[str, Any]:
        return {"subagent_run_id": owner} if owner else {}

    @staticmethod
    def _envelope(record: Dict[str, Any]) -> Dict[str, Any]:
        detail = record.get("detail") or {}
        return _compact(
            {
                "eventId": record.get("id"),
                "terminalId": record.get("terminal_id"),
                "sessionName": record.get("session_name"),
                "traceparent": record.get("traceparent") or detail.get("traceparent"),
            }
        )

    def _raw(
        self,
        record: Dict[str, Any],
        data: Dict[str, Any],
        ts: Optional[int],
        envelope: Dict[str, Any],
        owner: Optional[str],
    ) -> Any:
        payload = json.loads(json.dumps(_compact(dict(data)), default=str))
        payload.setdefault("kind", record.get("kind"))
        return _models().RawEvent(
            event=payload,
            source="cao",
            timestamp=ts,
            metadata={"cao": envelope},
            **self._attr(owner),
        )


# ---------------------------------------------------------------------------
# The streaming loop
# ---------------------------------------------------------------------------


#: The shortest gap between two snapshot refreshes caused by status changes alone. Status
#: changes arrive in bursts (a worker starting emits several), so they are coalesced to one
#: refresh per this many seconds per stream, and the trailing change is always delivered.
STATUS_REFRESH_MIN_SECONDS = 2.0

#: The in-process topics that mean "a terminal's reported status may have changed": the
#: status monitor publishes ``status`` only when a terminal's applied status CHANGES, and
#: ``delivery`` is published when a terminal's first-message mask lifts (which changes the
#: status a client reads without changing the applied one). Metadata only: the stream reads
#: the fact of an event, never its payload.
_STATUS_TOPICS = ("terminal.*.status", "terminal.*.delivery")


async def ambient_stream(
    *,
    run: AmbientRun,
    encode: Callable[[Optional[str], Any], str],
    is_sse: bool,
    replay: Iterable[Dict[str, Any]],
    fleet_snapshot: Callable[[], Dict[str, Any]],
    diff: Callable[[Dict[str, Any], Dict[str, Any]], Optional[List[Dict[str, Any]]]],
    bus: Any,
    sub: Any,
    keepalive_seconds: float,
    max_seconds: Optional[float] = None,
) -> AsyncIterator[str]:
    """Yield the encoded frames of one ambient run.

    ``encode(sse_id, event)`` turns an SDK event into wire text. The live loop waits
    on the next bus item and on the stop signal together (FIRST_COMPLETED), so a
    server stop or the ``max_seconds`` limit closes the run promptly with the
    cancelled outcome; a bus overflow closes it with ``RUN_ERROR``.
    """

    # When anything was last written to the client (a frame or a keep-alive), so a wake that
    # produces nothing cannot starve the keep-alive.
    last_write = time.monotonic()

    def emit(items: List[Item]) -> List[str]:
        nonlocal last_write
        if items:
            last_write = time.monotonic()
        return [encode(sid, ev) for sid, ev in items]

    status_queues: List[Tuple[str, "asyncio.Queue[Any]"]] = []
    status_bus: Any = None
    status_waits: Dict[Any, "asyncio.Future[Any]"] = {}
    status_dirty = False
    last_refresh = 0.0

    waiter = _register_stop_waiter()
    _, stop_event = waiter
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds if max_seconds else None
    drain = bus.drain(sub).__aiter__()
    pending: Optional["asyncio.Future[Any]"] = None
    stop_wait: Optional["asyncio.Future[Any]"] = None
    try:
        # Subscribed inside the try that owns the finally, so nothing can leak the queue.
        # Failure-isolated: without the status topic the stream still refreshes on records.
        try:
            from cli_agent_orchestrator.services import event_bus

            status_bus = event_bus.bus
            for topic in _STATUS_TOPICS:
                status_queues.append((topic, status_bus.subscribe(topic)))
        except Exception:
            logger.warning(
                "ambient_run: status topics unavailable; refreshing on records only",
                exc_info=True,
            )
        for frame in emit(run.start()):
            yield frame
        replayed_ids: Set[str] = set()
        for record in replay:
            rid = record.get("id")
            if rid is not None:
                replayed_ids.add(str(rid))
            for frame in emit(run.feed(record)):
                yield frame
        # Failure-isolated, as on the legacy path: a backend hiccup skips the
        # snapshot (and deltas) instead of tearing the run down.
        prev: Optional[Dict[str, Any]] = None
        try:
            prev = fleet_snapshot()
        except Exception:
            logger.warning("ambient_run: initial STATE_SNAPSHOT failed", exc_info=True)
        if prev is not None:
            for frame in emit(run.snapshot(prev)):
                yield frame
        last_refresh = loop.time()

        # shortcut: each open stream rebuilds the snapshot itself (coalesced per stream, not
        # per server), so the cost grows with the number of open streams. A shared snapshot
        # would need event-based invalidation and a key for the scope set; see docs/agui.md.
        def refresh() -> List[str]:
            """Recompute the fleet snapshot and encode the delta (or the first snapshot)."""
            nonlocal prev, status_dirty, last_refresh
            status_dirty = False
            last_refresh = loop.time()
            out: List[str] = []
            try:
                curr = fleet_snapshot()
                if prev is None:
                    prev = curr
                    out.extend(emit(run.snapshot(curr)))
                else:
                    ops = diff(prev, curr)
                    if ops:
                        out.extend(emit(run.delta(ops)))
                    prev = curr
            except Exception:
                logger.warning("ambient_run: STATE_DELTA computation failed", exc_info=True)
            return out

        reason: Optional[str] = None
        overflow = False
        while True:
            # Checked on every pass: a bus that always has a record ready never
            # lets the wait below time out, so an idle-only check could overrun.
            if deadline is not None and loop.time() >= deadline:
                reason = "max_seconds"
                break
            # Wakes that send nothing (a status event whose refresh has no delta) must not
            # starve the keep-alive: if the client has heard nothing for a whole interval,
            # say so now, whatever woke us.
            if is_sse and time.monotonic() - last_write >= keepalive_seconds:
                last_write = time.monotonic()
                yield ":keep-alive\n\n"
            if pending is None:
                pending = asyncio.ensure_future(drain.__anext__())
            if stop_wait is None:
                stop_wait = asyncio.ensure_future(stop_event.wait())
            timeout = keepalive_seconds
            if deadline is not None:
                timeout = max(0.0, min(timeout, deadline - loop.time()))
            waiting = {pending, stop_wait}
            for _topic, queue in status_queues:
                if queue not in status_waits:
                    status_waits[queue] = asyncio.ensure_future(queue.get())
                waiting.add(status_waits[queue])
            if status_dirty:
                # A change is waiting out the coalescing gap: wake exactly when it ends.
                gap = STATUS_REFRESH_MIN_SECONDS - (loop.time() - last_refresh)
                timeout = max(0.0, min(timeout, gap))
            done, _ = await asyncio.wait(
                waiting, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done:
                reason = "server_stop"
                break
            for _topic, queue in status_queues:
                if status_waits.get(queue) in done:
                    del status_waits[queue]  # one wake per drained batch; re-armed above
                    while not queue.empty():
                        queue.get_nowait()
                    status_dirty = True
            if status_dirty and loop.time() - last_refresh >= STATUS_REFRESH_MIN_SECONDS:
                for frame in refresh():
                    yield frame
                if pending not in done:
                    continue
            elif status_dirty and pending not in done:
                continue  # still inside the coalescing gap: wake again when it ends
            if pending in done:
                fut, pending = pending, None
                try:
                    record = fut.result()
                except StopAsyncIteration:
                    overflow = bool(getattr(sub, "overflowed", False))
                    reason = "source_ended"
                    break
                rid = record.get("id") if isinstance(record, dict) else None
                if rid is not None and str(rid) in replayed_ids:
                    # The replay/live overlap: already narrated above, never twice.
                    replayed_ids.discard(str(rid))
                    continue
                for frame in emit(run.feed(record)):
                    yield frame
                for frame in refresh():
                    yield frame
                continue
            if deadline is not None and loop.time() >= deadline:
                reason = "max_seconds"
                break
            if is_sse:
                last_write = time.monotonic()
                yield ":keep-alive\n\n"

        if overflow:
            for frame in emit(
                run.close_error(RUN_ERROR_OVERFLOW, "event buffer overflowed; reconnect to replay")
            ):
                yield frame
        else:
            for frame in emit(run.close_cancelled(reason or "stream_closed")):
                yield frame
    except asyncio.CancelledError:
        raise  # client disconnect: a truncated run, no closer
    except Exception:
        logger.warning("ambient_run: stream failed", exc_info=True)
        if not run.closed:
            for frame in emit(run.close_error(RUN_ERROR_INTERNAL, "ambient stream failed")):
                yield frame
    finally:
        for fut in (pending, stop_wait, *status_waits.values()):
            if fut is not None and not fut.done():
                fut.cancel()
        for topic, queue in status_queues:
            try:
                status_bus.unsubscribe(topic, queue)
            except Exception:
                logger.debug("ambient_run: status unsubscribe failed", exc_info=True)
        _unregister_stop_waiter(waiter)
