"""The steering module: the only place that writes to CAO.

Writes exist behind five endpoints: ask a conductor, answer a waiting terminal, start a session,
and stop (plan then execute). ``CaoWriter`` is the one client that may send ``POST`` or ``DELETE``.
Every write re-reads CAO first and refuses when the world no longer matches what the user saw.

Nothing here logs a message body, an answer, a prompt, a working directory or a first message.
The stop audit line carries a session name, a terminal count and an outcome.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Set, Tuple

from . import prompts, tiering
from .cao import CaoClient, cao_session_name, check_cao_name, check_terminal_id
from .errors import TRANSPORT_CODES, ShelfError
from .poller import Poller
from .validation import (
    NAME_PATTERN,
    TERMINAL_ID_PATTERN,
    TERMINAL_ID_RE,
    Field,
    check_name,
    has_control,
    validate_object,
)

log = logging.getLogger("cao_shelf.actions")

SENDER_ID = "cao-shelf"
PLAN_TTL_SECONDS = 300
MAX_PLANS = 20
MAX_STOP_TARGETS = 25
PROMPT_CACHE_SECONDS = 10.0
PROFILE_CACHE_SECONDS = 60.0
START_RESULT_SECONDS = 600.0  # a retry inside this time gets the first launch's answer
MAX_START_RESULTS = 100
ASK_DELIVERY_ROWS = 20
START_READ_TIMEOUT = 30.0
MAX_ENCODED_QUERY = 12000
WORKING_STATUSES = ("processing", "waiting_user_answer")


# ------------------------------------------------------------------------------- the write client
class CaoWriter(CaoClient):
    """``CaoClient`` plus the four write calls. Used only from this module."""

    ALLOWED_METHODS: FrozenSet[str] = frozenset({"GET", "POST", "DELETE"})

    @staticmethod
    def _fits(text: str) -> None:
        if len(urllib.parse.quote(text)) > MAX_ENCODED_QUERY:
            raise ShelfError("invalid_request", "the text is too long to send to CAO")

    def post_inbox_message(self, receiver_id: str, sender_id: str, message: str) -> int:
        rid = check_terminal_id(receiver_id)
        self._fits(message)
        data = self._request(
            "POST",
            f"/terminals/{rid}/inbox/messages",
            {"sender_id": sender_id, "message": message},
        )
        mid = data.get("message_id") if isinstance(data, dict) else None
        if isinstance(mid, bool) or not isinstance(mid, int):
            raise ShelfError("cao_incompatible", "CAO did not return a message id")
        return mid

    def post_input(self, terminal_id: str, message: str) -> None:
        """Type ``message`` into a terminal (what ``answer_user_prompt`` does)."""
        tid = check_terminal_id(terminal_id)
        self._fits(message)
        data = self._request("POST", f"/terminals/{tid}/input", {"message": message})
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ShelfError("cao_error", "CAO did not accept the input")

    def create_session(
        self,
        *,
        agent_profile: str,
        provider: Optional[str],
        session_name: Optional[str],
        working_directory: str,
        initial_message: Optional[str],
        idempotency_key: str,
    ) -> Dict[str, Any]:
        for text in (agent_profile, working_directory, initial_message or ""):
            self._fits(text)
        query = {
            "agent_profile": agent_profile,
            "provider": provider,
            "session_name": cao_session_name(session_name) if session_name else None,
            "working_directory": working_directory,
            "idempotency_key": idempotency_key,
        }
        body = {"initial_message": initial_message} if initial_message else None
        data = self._request("POST", "/sessions", query, body=body, read_timeout=START_READ_TIMEOUT)
        if not isinstance(data, dict) or not isinstance(data.get("id"), str):
            raise ShelfError("cao_incompatible", "CAO did not describe the new session")
        return data

    def delete_session(self, cao_name: str) -> None:
        """End one session. The name is always the canonical ``cao-<name>``."""
        name = check_cao_name(cao_name)
        data = self._request("DELETE", f"/sessions/{name}")
        if not isinstance(data, dict) or data.get("success") is not True:
            raise ShelfError("cao_error", "CAO did not confirm the stop")


WriterFactory = Callable[[str, Callable[[str, str], None]], CaoWriter]


@dataclass
class _PlanSession:
    terminal_ids: Set[str]
    last_active: str


@dataclass
class _Plan:
    scope: str
    expires_at: float
    sessions: Dict[str, _PlanSession]
    consumed: Set[str] = field(default_factory=set)


# ------------------------------------------------------------------------------- request shapes
ASK = {
    "message": Field("str", required=True, min_len=1, max_len=8000),
    "conductor_id": Field("str", required=True, pattern=TERMINAL_ID_PATTERN),
}
ANSWER = {
    "answer": Field("str", required=True, min_len=1, max_len=2000),
    "expect_status": Field("enum", required=True, choices=("waiting_user_answer",)),
}
IDEMPOTENCY_KEY_PATTERN = r"[A-Za-z0-9_-]{16,64}"
START = {
    # chosen by the page once per dialog and kept across retries; the backend never invents one
    "idempotency_key": Field("str", required=True, pattern=IDEMPOTENCY_KEY_PATTERN),
    "name": Field("str", pattern=NAME_PATTERN),
    "agent_profile": Field("str", required=True, min_len=1, max_len=200),
    "provider": Field("str", nullable=True, max_len=64, pattern=r"[A-Za-z0-9_.-]*"),
    "working_directory": Field("str", required=True, min_len=1, max_len=4096),
    "message": Field("str", nullable=True, max_len=8000),
}
STOP_PLAN = {
    "scope": Field("enum", required=True, choices=("stale", "named")),
    "sessions": Field(
        "array",
        items=Field("str", pattern=NAME_PATTERN),
        min_items=1,
        max_items=MAX_STOP_TARGETS,
        too_many=True,
    ),
}
_STOP_ITEM = {
    "name": Field("str", required=True, pattern=NAME_PATTERN),
    "last_active": Field("str", required=True, min_len=0, max_len=64),
    "acknowledge_working": Field("bool"),
}
STOP_EXECUTE = {
    "plan_id": Field("str", required=True, min_len=1, max_len=100),
    "sessions": Field(
        "array",
        required=True,
        items=Field("object", spec=_STOP_ITEM),
        min_items=1,
        max_items=MAX_STOP_TARGETS,
        too_many=True,
    ),
}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _text_ok(*values: Optional[str]) -> None:
    for value in values:
        if value is not None and has_control(value):
            raise ShelfError("invalid_request", "control characters are not allowed")


class Actions:
    def __init__(
        self,
        poller: Poller,
        settings: Callable[[], Dict[str, Any]],
        writer_factory: WriterFactory,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._poller = poller
        self._settings = settings
        self._factory = writer_factory
        self._clock = clock
        self._started: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._plans: Dict[str, _Plan] = {}
        self._tails: Dict[str, Tuple[float, List[str]]] = {}
        self._profiles: Optional[Tuple[float, List[Dict[str, Any]]]] = None

    def _writer(self) -> CaoWriter:
        return self._factory(self._settings()["cao_url"], self._poller.observe)

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self._clock(), tz=timezone.utc)

    # ------------------------------------------------------------------------------------- ask
    def ask(self, name: str, body: Any) -> Dict[str, Any]:
        check_name(name)
        req = validate_object(body, ASK)
        _text_ok(req["message"])
        writer = self._writer()
        data = writer.get_session(cao_session_name(name))
        rows = [r for r in data["terminals"] if _valid_id(r)]
        if not rows:
            raise ShelfError("not_found", "that session has no terminals")
        conductor = self._conductor_of(writer, rows)
        if conductor is None:
            raise ShelfError(
                "conflict_changed",
                "this session has no conductor (it was deleted), so there is nobody to ask",
                detail={"conductor_id": None},
            )
        if conductor != req["conductor_id"]:
            raise ShelfError(
                "conflict_changed",
                "this session changed, reload",
                detail={"conductor_id": conductor},
            )
        message_id = writer.post_inbox_message(conductor, SENDER_ID, req["message"])
        self._poller.invalidate(name)
        return {
            "accepted": True,
            "terminal_id": conductor,
            "message_id": message_id,
            "delivery": self._delivery(writer, conductor, message_id),
        }

    def conductor_of_session(self, name: str) -> Optional[str]:
        """The conductor of a session read from CAO now, or None when it has none.

        ``not_found`` when the session is gone or has no terminals. Reads only."""
        writer = self._writer()
        data = writer.get_session(cao_session_name(check_name(name)))
        rows = [r for r in data["terminals"] if _valid_id(r)]
        if not rows:
            raise ShelfError("not_found", "that session has no terminals")
        return self._conductor_of(writer, rows)

    def _conductor_of(self, writer: CaoWriter, rows: List[Dict[str, Any]]) -> Optional[str]:
        """The conductor of these rows, never a helper. A caller the poller has not read yet is
        read here, so a stale or empty cache cannot send an Ask to a helper."""
        callers = dict(self._poller.caller_ids())
        first = str(rows[0]["id"])
        if first not in callers:
            caller = writer.get_terminal(first).get("caller_id")
            callers[first] = caller if isinstance(caller, str) and _valid_text_id(caller) else None
        return tiering.pick_conductor(rows, callers, self._poller.role_live_ids())

    @staticmethod
    def _delivery(writer: CaoWriter, conductor: str, message_id: int) -> str:
        """What CAO did with the message: ``delivered``, ``pending`` (queued until the conductor is
        idle), ``failed``, or ``unknown`` when the row cannot be read back."""
        try:
            rows = writer.inbox_messages(conductor, limit=ASK_DELIVERY_ROWS, order="desc")
        except ShelfError:
            return "unknown"
        for row in rows:
            if row.get("id") == message_id:
                status = row.get("status")
                return status if status in ("pending", "delivered", "failed") else "unknown"
        return "unknown"

    # --------------------------------------------------------------------------------- prompt
    def prompt(self, terminal_id: str) -> Dict[str, Any]:
        tid = check_terminal_id(terminal_id)
        writer = self._writer()
        term = writer.get_terminal(tid)
        status = tiering._status(term.get("status"))
        raw_provider = term.get("provider")
        provider = raw_provider if isinstance(raw_provider, str) else ""
        tail: List[str] = []
        if status in ("waiting_user_answer", "error"):
            tail = self._tail(writer, tid)
        suggested = prompts.suggest(tail, provider) if status == "waiting_user_answer" else []
        return {
            "terminal_id": tid,
            "status": status,
            "provider": provider,
            "tail": tail,
            "suggested": suggested,
        }

    def _tail(self, writer: CaoWriter, tid: str) -> List[str]:
        now = self._clock()
        with self._lock:
            for key in [k for k, (at, _) in self._tails.items() if now - at > PROMPT_CACHE_SECONDS]:
                del self._tails[key]
            hit = self._tails.get(tid)
        if hit is not None:
            return list(hit[1])
        tail = prompts.clean_tail(writer.get_output(tid, "full"))
        with self._lock:
            if len(self._tails) >= 64:
                self._tails.clear()
            self._tails[tid] = (now, tail)
        return list(tail)

    # --------------------------------------------------------------------------------- answer
    def answer(self, terminal_id: str, body: Any) -> Dict[str, Any]:
        tid = check_terminal_id(terminal_id)
        req = validate_object(body, ANSWER)
        _text_ok(req["answer"])
        writer = self._writer()
        status = tiering._status(writer.get_terminal(tid).get("status"))
        if status != "waiting_user_answer":
            raise ShelfError(
                "not_waiting", "that terminal is no longer waiting", detail={"status": status}
            )
        writer.post_input(tid, req["answer"])  # typed input; never the inbox route
        with self._lock:
            self._tails.pop(tid, None)
        return {"delivered": True, "terminal_id": tid}

    # -------------------------------------------------------------------------------- profiles
    def profiles(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            cached = self._profiles
        if cached is not None and now - cached[0] < PROFILE_CACHE_SECONDS:
            return {"profiles": cached[1]}
        out = []
        for row in self._writer().list_profiles():
            desc = row.get("description")
            provider = row.get("provider")
            out.append(
                {
                    "name": row["name"][:200],
                    "description": desc[:300] if isinstance(desc, str) else "",
                    "provider": provider if isinstance(provider, str) else None,
                }
            )
        with self._lock:
            self._profiles = (now, out)
        return {"profiles": out}

    # ---------------------------------------------------------------------------------- start
    def start(self, body: Any) -> Dict[str, Any]:
        req = validate_object(body, START)
        _text_ok(req.get("name"), req["agent_profile"], req["working_directory"])
        provider = req.get("provider") or None
        fingerprint = json.dumps(
            [
                req.get("name"),
                req["agent_profile"],
                provider,
                req["working_directory"],
                req.get("message") or None,
            ],
            sort_keys=True,
        )
        memo = (req["idempotency_key"], fingerprint)
        now = self._clock()
        with self._lock:
            for key in [
                k for k, (at, _) in self._started.items() if now - at > START_RESULT_SECONDS
            ]:
                del self._started[key]
            first = self._started.get(memo)
        if first is not None:
            return dict(first[1])  # the browser retried: this is the first launch's answer
        try:
            data = self._writer().create_session(
                agent_profile=req["agent_profile"],
                provider=provider,
                session_name=req.get("name"),
                working_directory=req["working_directory"],
                initial_message=req.get("message") or None,
                idempotency_key=req["idempotency_key"],
            )
        except ShelfError as err:
            if err.code != "cao_error":
                raise
            # CAO's own text is never copied into a message (it could echo the request); the code
            # and the HTTP status stay in ``detail`` and the message names the usual causes.
            raise ShelfError(
                "cao_error",
                "CAO refused to start the session. Check that the name is not already used, the "
                "working directory exists and the profile is installed.",
                detail=err.detail,
            ) from None
        created = data.get("session_name")
        bare = (
            created[len("cao-") :]
            if isinstance(created, str) and created.startswith("cao-")
            else created
        )
        if not isinstance(bare, str) or not bare:
            bare = req.get("name") or ""
        result = {"session": bare, "conductor_id": data["id"]}
        with self._lock:
            if len(self._started) >= MAX_START_RESULTS:
                del self._started[min(self._started, key=lambda k: self._started[k][0])]
            self._started[memo] = (now, result)
        return dict(result)

    # ----------------------------------------------------------------------------- stop: plan
    def stop_plan(self, body: Any) -> Dict[str, Any]:
        req = validate_object(body, STOP_PLAN)
        scope = req["scope"]
        names = req.get("sessions")
        if scope == "stale" and names is not None:
            raise ShelfError("invalid_request", "sessions is only for scope 'named'")
        if scope == "named":
            if not names:
                raise ShelfError("invalid_request", "sessions is required for scope 'named'")
            if len(set(names)) != len(names):
                raise ShelfError("invalid_request", "a session is listed twice")
        writer = self._writer()
        truncated = False
        if scope == "stale":
            snap = self._poller.peek()
            candidates = sorted(
                (s for s in (snap or {}).get("sessions", []) if s["tier"] == "stale"),
                key=lambda s: (-s["idle_seconds"], s["name"]),
            )
            truncated = len(candidates) > MAX_STOP_TARGETS
            names = [s["name"] for s in candidates[:MAX_STOP_TARGETS]]
        assert names is not None
        thresholds = self._thresholds()
        listed: List[Dict[str, Any]] = []
        plan_sessions: Dict[str, _PlanSession] = {}
        callers, live = self._poller.caller_ids(), self._poller.role_live_ids()
        now = self._now()
        for name in names:
            rows = [
                r for r in writer.get_session(cao_session_name(name))["terminals"] if _valid_id(r)
            ]
            if not rows:
                if scope == "named":
                    raise ShelfError("not_found", "that session has no terminals")
                continue
            facts = _facts(rows, now, thresholds)
            if scope == "stale" and facts["tier"] != "stale":
                continue  # it moved since the shelf was last read: not listed
            roles = tiering.assign_roles(rows, callers, live)
            listed.append(
                {
                    "name": name,
                    "tier": facts["tier"],
                    "last_active": facts["last_active"],
                    "terminals": [
                        {
                            "id": str(r["id"]),
                            "label": (
                                r.get("tmux_window")
                                if isinstance(r.get("tmux_window"), str)
                                else ""
                            ),
                            "role": roles[str(r["id"])],
                            "status": tiering._status(r.get("status")),
                        }
                        for r in rows
                    ],
                    "working": facts["working"],
                }
            )
            plan_sessions[name] = _PlanSession({str(r["id"]) for r in rows}, facts["last_active"])
        plan_id = secrets.token_urlsafe(18)
        expires = self._clock() + PLAN_TTL_SECONDS
        with self._lock:
            self._prune_plans()
            self._plans[plan_id] = _Plan(scope, expires, plan_sessions)
        return {
            "plan_id": plan_id,
            "expires_at": _iso(expires),
            "sessions": listed,
            "truncated": truncated,
        }

    def _prune_plans(self) -> None:
        now = self._clock()
        for key in [k for k, p in self._plans.items() if p.expires_at <= now]:
            del self._plans[key]
        while len(self._plans) >= MAX_PLANS:
            del self._plans[min(self._plans, key=lambda k: self._plans[k].expires_at)]

    def _thresholds(self) -> Dict[str, float]:
        s = self._settings()
        return {"warm_hours": float(s["warm_hours"]), "stale_hours": float(s["stale_hours"])}

    # --------------------------------------------------------------------------- stop: execute
    def stop_execute(self, body: Any) -> Dict[str, Any]:
        req = validate_object(body, STOP_EXECUTE)
        items: List[Dict[str, Any]] = req["sessions"]
        with self._lock:
            plan = self._plans.get(req["plan_id"])
            if plan is None or plan.expires_at <= self._clock():
                self._plans.pop(req["plan_id"], None)
                raise ShelfError("plan_expired", "that plan is unknown or older than 5 minutes")
        if plan.scope == "stale" and any(i.get("acknowledge_working") for i in items):
            raise ShelfError(
                "invalid_request", "a clean-up plan cannot stop sessions that are working"
            )
        writer = self._writer()
        thresholds = self._thresholds()
        results: List[Dict[str, Any]] = []
        halted = False
        with self._stop_lock:
            for item in items:
                name = item["name"]
                if halted:
                    results.append(_result(name, "failed", "not attempted: CAO stopped answering"))
                    continue
                outcome, message, halted = self._stop_one(writer, plan, item, thresholds)
                log.info(
                    "stop session=%s terminals=%d outcome=%s",
                    name,
                    len(plan.sessions[name].terminal_ids) if name in plan.sessions else 0,
                    outcome,
                )
                results.append(_result(name, outcome, message))
        return {"results": results}

    def _stop_one(
        self,
        writer: CaoWriter,
        plan: _Plan,
        item: Mapping[str, Any],
        thresholds: Mapping[str, float],
    ) -> Tuple[str, Optional[str], bool]:
        """One session: ``(outcome, message, halt)``. Sends a DELETE only after every check."""
        name = item["name"]
        with self._lock:
            listed = plan.sessions.get(name)
            if listed is None or name in plan.consumed:
                return "skipped_not_in_plan", "not in this plan, or already handled", False
            plan.consumed.add(name)
        try:
            data = writer.get_session(cao_session_name(name))
        except ShelfError as err:
            if err.code == "not_found":
                return "not_found", "the session is already gone", False
            return "failed", f"could not read the session ({err.code})", err.code in TRANSPORT_CODES
        rows = [r for r in data["terminals"] if _valid_id(r)]
        facts = _facts(rows, self._now(), thresholds)
        ids = {str(r["id"]) for r in rows}
        if not ids <= listed.terminal_ids:
            return "skipped_changed", "terminals were added since the plan", False
        if facts["last_active"] != item["last_active"] or item["last_active"] != listed.last_active:
            return "skipped_changed", "the session was active since the plan", False
        if facts["working"] and not (item.get("acknowledge_working") and plan.scope == "named"):
            return "skipped_working", "the session is working or waiting for an answer", False
        try:
            writer.delete_session(cao_session_name(name))
        except ShelfError as err:
            if err.code == "not_found":
                return "not_found", "the session is already gone", False
            return "failed", f"CAO did not stop it ({err.code})", err.code in TRANSPORT_CODES
        return "stopped", None, False


def _valid_text_id(text: str) -> bool:
    return TERMINAL_ID_RE.fullmatch(text) is not None


def _valid_id(row: Any) -> bool:
    try:
        check_terminal_id(row.get("id"))
    except (ShelfError, AttributeError):
        return False
    return True


def _facts(
    rows: List[Mapping[str, Any]], now: datetime, thresholds: Mapping[str, float]
) -> Dict[str, Any]:
    """Tier, youngest ``last_active`` (CAO's own text) and whether anything is working."""
    youngest_at: Optional[datetime] = None
    raw: Any = None
    for r in rows:
        moment = tiering.parse_time_or_none(r.get("last_active"))
        if moment is not None and (youngest_at is None or moment > youngest_at):
            youngest_at, raw = moment, r.get("last_active")
    age, age_known = tiering.session_age(rows, now)
    statuses = [tiering._status(r.get("status")) for r in rows]
    tier, _, _ = tiering.classify(
        statuses,
        age,
        float(thresholds["warm_hours"]),
        float(thresholds["stale_hours"]),
        age_known,
    )
    return {
        "tier": tier,
        "last_active": raw if isinstance(raw, str) else "",
        "working": any(s in WORKING_STATUSES for s in statuses),
    }


def _result(name: str, outcome: str, message: Optional[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": name, "outcome": outcome}
    if message:
        out["message"] = message[:300]
    return out
