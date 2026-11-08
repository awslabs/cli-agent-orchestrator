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

This is an EDGE layer. It reads the raw CAO event records and never changes the
internal ``agui_stream.to_agui_event`` frame or ``ToolCallLifecycleTracker``,
which the L2 constructs and the run plane's live projection consume. In
particular, the tracker's synthesized ``TOOL_CALL_RESULT`` for a2a delegations is
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
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Optional, Set, Tuple

from cli_agent_orchestrator.services.agui.attribution import (
    subagent_run_id_for_session,
    subagent_run_id_for_terminal,
)
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
    ) -> None:
        self.thread_id = thread_id
        self.run_id = run_id
        self._incarnation_of = incarnation_of
        # Whether CAO knows a terminal this run has not seen yet. A sender or
        # terminal_id that is neither open on the run nor known to CAO (a label
        # such as "operator" or "supervisor") never becomes a subagent.
        self._terminal_known = terminal_known
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
            orchestration = detail.get("orchestration_type")
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
                out.extend(
                    self._open_terminal(
                        terminal_id, session, name="terminal", provider=None, envelope={}, ts=None
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

    def _close_calls_for_terminal(self, terminal_id: str, ts: Optional[int]) -> List[Any]:
        sid = self._terminals.get(terminal_id, (None, ""))[0]
        ids = [
            cid
            for cid, call in self._calls.items()
            if call["receiver"] == terminal_id or (sid is not None and call["owner"] == sid)
        ]
        return [self._end_call(cid, ts, "completion") for cid in ids]

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

    def emit(items: List[Item]) -> List[str]:
        return [encode(sid, ev) for sid, ev in items]

    waiter = _register_stop_waiter()
    _, stop_event = waiter
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_seconds if max_seconds else None
    drain = bus.drain(sub).__aiter__()
    pending: Optional["asyncio.Future[Any]"] = None
    stop_wait: Optional["asyncio.Future[Any]"] = None
    try:
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

        reason: Optional[str] = None
        overflow = False
        while True:
            # Checked on every pass: a bus that always has a record ready never
            # lets the wait below time out, so an idle-only check could overrun.
            if deadline is not None and loop.time() >= deadline:
                reason = "max_seconds"
                break
            if pending is None:
                pending = asyncio.ensure_future(drain.__anext__())
            if stop_wait is None:
                stop_wait = asyncio.ensure_future(stop_event.wait())
            timeout = keepalive_seconds
            if deadline is not None:
                timeout = max(0.0, min(timeout, deadline - loop.time()))
            done, _ = await asyncio.wait(
                {pending, stop_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if stop_wait in done:
                reason = "server_stop"
                break
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
                try:
                    curr = fleet_snapshot()
                    if prev is None:
                        prev = curr
                        for frame in emit(run.snapshot(curr)):
                            yield frame
                        continue
                    ops = diff(prev, curr)
                    if ops:
                        for frame in emit(run.delta(ops)):
                            yield frame
                    prev = curr
                except Exception:
                    logger.warning("ambient_run: STATE_DELTA computation failed", exc_info=True)
                continue
            if deadline is not None and loop.time() >= deadline:
                reason = "max_seconds"
                break
            if is_sse:
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
        for fut in (pending, stop_wait):
            if fut is not None and not fut.done():
                fut.cancel()
        _unregister_stop_waiter(waiter)
