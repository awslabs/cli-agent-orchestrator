"""Remote-control relays: a Kiro Crew chat on the ``cao-remote-control`` agent drives a CAO session.

What is enforced here, in code:

* at most one ``active`` relay per CAO session (``relay_exists``), and Take over replaces it;
* attach checks the conductor, creates the chat, sets its trust mode to ``normal`` and posts the
  setup message; any failure deletes the chat and leaves no record;
* ending a relay posts the relay-ended message (or deletes the chat) and never calls CAO;
* reconcile: a relay whose CAO session is gone is ``orphaned``, one whose conductor changed is
  ``needs_reattach`` (both get the relay-ended message), one whose chat is gone is ``ended``;
* records live in ``data/relays.json`` (0600, atomic) with ids, names, labels, states, times,
  counts and the CAO address, never message or reply text.

What is NOT enforced here: which terminal the agent addresses, the tags, the catch-up check and
reply freshness. Those are the agent's instructions (``agents/cao-remote-control.json``).

The texts posted into chats are the ones in api-contract.md, "Remote control".
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

from . import tiering
from .actions import START, Actions
from .cao import SESSION_PREFIX, validate_cao_url
from .config import atomic_write_json, move_aside
from .errors import TRANSPORT_CODES, ShelfError
from .gateway import GatewayClient
from .poller import Poller
from .validation import (
    NAME_PATTERN,
    TERMINAL_ID_RE,
    Field,
    check_name,
    has_control,
    validate_object,
)

log = logging.getLogger("cao_shelf.relays")

# ------------------------------------------------------------------ texts (api-contract.md)
HANDSHAKE = (
    "[REMOTE-CONTROL] Remote control is on for this session. A message that begins with "
    "[REMOTE-CONTROL] comes from your owner through a Kiro Crew chat, and your reply to it is "
    "relayed back to that chat. A message without the tag is your owner typing to you directly. "
    "Both are your owner: answer both and carry on with the same work. When a message begins "
    'with [REMOTE-CONTROL catch-up], open your reply with a line that starts with "Since the '
    'last remote message:" and briefly say what happened in the untagged exchanges since the last '
    "tagged message. Give that brief unprompted too whenever you received untagged messages since "
    "the last tagged one. Reply to this message with OK only."
)
SETUP_TEMPLATE = (
    "CAO Shelf relay setup\n"
    "relay_id: <relay_id>\n"
    "session: <session>\n"
    "conductor_terminal_id: <terminal_id>\n"
    "handshake: <pending|already_given>\n"
    "This message was posted by the CAO Shelf app, not typed by the owner."
)
ENDED_TEMPLATE = (
    "CAO Shelf relay ended\n"
    "relay_id: <relay_id>\n"
    "reason: <reason>\n"
    "This message was posted by the CAO Shelf app, not typed by the owner."
)
END_REASONS: Dict[str, str] = {
    "ended_by_owner": "you pressed End relay",
    "taken_over": "another chat took over this session",
    "session_gone": "the CAO session no longer exists",
    "conductor_changed": "the session's conductor changed",
}
# The risk the owner accepts before a relay chat is made (api-contract.md, "risk:relay" and
# "risk:relay-detail"). The short text is shown first; the detail, one paragraph per case, sits
# behind "What the chat can reach". On the kas backend the agent's `tools` list is a strict
# allowlist; on the claude backend Kiro Crew gives an App agent no way to keep the owner's plugins,
# user-level MCP servers or the remaining built-ins out of the chat, nor to hold it off auto mode.
# On either, a host plugin can add an MCP server to every agent spec (a credentials server, say).
RISK = (
    "A Kiro Crew chat sends your messages to the CAO session and shows its replies. Text from "
    "the session reaches the chat's model, so an instruction hidden in it could make the model "
    "call a tool the chat has, and an allowed tool runs without asking you. On the claude "
    "backend the chat also has your own Claude Code tools and plugins, and on any backend a "
    "host plugin can add servers to every agent, for example a credentials server."
)
RISK_DETAIL = (
    "On the kas backend the chat is offered only its four CAO tools (send_session_message, "
    "get_terminal_status, read_session_output and get_session_info) and Kiro Crew's wait tool, "
    "which only pauses the chat. They run without asking you, and a CAO tool runs for any "
    "terminal the model names. No Kiro built-in tool (shell, files, web or sub-agents), no Claude "
    "Code setting or plugin and no server from your mcp.json reaches it, except servers that Kiro "
    "Crew or a host plugin adds to every agent.",
    "On the claude backend the chat runs with your own Claude Code settings. Besides its four CAO "
    "tools and Kiro Crew's wait tool (which only pauses the chat), it can reach your Claude Code "
    "plugins, your user-level MCP servers and Claude Code's other built-in tools (for example "
    "Read, WebFetch, sub-agents, scheduled tasks and worktrees). Kiro Crew refuses the shell and "
    "Claude Code's file-edit tools here; if you enable the Claude Code sandbox, also set "
    "sandbox.autoAllowBashIfSandboxed to false, or sandboxed shell commands run without that "
    "refusal. A call runs without asking you when your Claude Code settings allow that tool or "
    "use auto mode. Sub-agents start without asking and can go on after End relay or Take over. "
    "Read plus WebFetch, or any plugin that posts or sends, is enough to send out any file you "
    "can read.",
    "On both backends a host plugin can add an MCP server to every agent, this one included: on "
    "some installs a credentials server whose tool hands out cloud credentials. Such "
    "a tool asks you first unless an allow rule, auto mode or a YOLO grant covers it. In a relay "
    "chat, refuse any request for a tool other than the four CAO tools and wait.",
)
# Fixed replies the prompt requires with no tool call (api-contract.md), so a turn answered with one
# of them was handled correctly and is not counted as "not relayed": the owner chose cancel, stop
# waiting or "Answer it in CAO Shelf", or asked for another session (refuse_target, whose filled-in
# text starts with this prefix), or Kiro Crew posted its own notice.
CHOICE_REPLIES = (
    "Cancelled. Nothing was sent.",
    "Stopped waiting. The session keeps working; its reply is not shown here, and your next "
    "message asks the session what happened, where CAO supports it.",
    "Answer it on the CAO Shelf page, in the Answer box on this session's card. Then choose "
    "Check again here.",
    "This chat controls only session ",
    "That was a notice from Kiro Crew, not a message from you, so nothing was sent.",
)
CHOICE_WORDS = frozenset({"cancel", "stop waiting"})
ROW_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

# ------------------------------------------------------------------ shapes and limits
RELAY_ID_PATTERN = r"rc-[a-f0-9]{10}"
RELAY_ID_RE = re.compile(RELAY_ID_PATTERN)
SLOT_PREFIX = "cao-shelf-"
STATES = ("attaching", "active", "ended", "orphaned", "needs_reattach")
CARD_STATES = ("active", "needs_reattach")
ORIGINS = ("attached", "started_by_relay")
MAX_INACTIVE_KEPT = 100
WATCH_SECONDS = 60.0  # slot and CAO check while any relay is active and no page is open
OPS_TOOLS = (
    "send_session_message",
    "get_terminal_status",
    "read_session_output",
    "get_session_info",
)
FORWARD_TOOLS = ("send_session_message", "get_terminal_status")
RECORD_FIELDS = (
    "relay_id",
    "slot_key",
    "title",
    "session",
    "conductor_id",
    "label",
    "origin",
    "state",
    "end_reason",
    "created_at",
    "updated_at",
    "ended_at",
    "not_relayed",
    "permission_pending",
    "cao_url",
    "start_key",
    "ended_notice_pending",
    "judged_through",
)

LABEL = Field("str", min_len=1, max_len=80)
START_RELAY = {k: v for k, v in START.items() if k != "message"}  # the handshake is the message
CREATE = {
    "session": Field("str", pattern=NAME_PATTERN),
    "label": LABEL,
    "start": Field("object", spec=START_RELAY),
    "acknowledge_risk": Field("bool"),
}
END = {"delete_chat": Field("bool")}
TAKEOVER = {"label": LABEL, "acknowledge_risk": Field("bool")}


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def setup_text(relay_id: str, session: str, conductor_id: str, handshake_given: bool) -> str:
    return (
        SETUP_TEMPLATE.replace("<relay_id>", relay_id)
        .replace("<session>", session)
        .replace("<terminal_id>", conductor_id)
        .replace("<pending|already_given>", "already_given" if handshake_given else "pending")
    )


def ended_text(relay_id: str, reason: str) -> str:
    return ENDED_TEMPLATE.replace("<relay_id>", relay_id).replace(
        "<reason>", f"{reason} ({END_REASONS[reason]})"
    )


def check_risk_acknowledged(req: Mapping[str, Any]) -> None:
    """Every request that makes a relay chat carries the owner's acceptance of ``RISK``."""
    if req.get("acknowledge_risk") is not True:
        raise ShelfError(
            "risk_not_acknowledged",
            "a relay chat is made only after the owner accepts its stated risk "
            "(acknowledge_risk: true; README, Remote control)",
        )


def check_relay_id(relay_id: Any) -> str:
    if not isinstance(relay_id, str) or RELAY_ID_RE.fullmatch(relay_id) is None:
        raise ShelfError("invalid_request", "invalid relay id")
    return relay_id


def _same_cao(a: str, b: str) -> bool:
    """Two loopback CAO addresses name the same server (``localhost`` is 127.0.0.1)."""

    def norm(url: str) -> str:
        return url.replace("://localhost", "://127.0.0.1").rstrip("/")

    return norm(a) == norm(b)


# ------------------------------------------------------------------ reading a chat's rows
def _meta_of(row: Mapping[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    meta = row.get("meta")
    if isinstance(meta, dict):
        out.update(meta)
    cls = row.get("cls")
    if isinstance(cls, str) and cls.startswith("{"):
        try:
            parsed = json.loads(cls)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            out.update(parsed)
    return out


def _names_forward_tool(row: Mapping[str, Any]) -> bool:
    """Lenient: does a tool or permission row mention a call that forwards or checks?"""
    blob = json.dumps([row.get("content"), _meta_of(row)], default=str)
    return any(t in blob for t in FORWARD_TOOLS)


def _is_app_turn(row: Mapping[str, Any]) -> bool:
    meta = _meta_of(row)
    return meta.get("turnActor") == "app"


class RowSignals(NamedTuple):
    """What a relay chat's rows say: see :func:`analyse_rows`."""

    not_relayed: int  # settled owner turns not forwarded with a row id after ``judged_through``
    window_total: int  # settled owner turns not forwarded in the whole window
    judged_through: Optional[str]  # the row id of the newest settled user turn judged
    placed: (
        bool  # every settled user turn has a row id, so the count can be added to the stored one
    )
    permission_pending: bool  # an unanswered permission request in the chat, for any tool


def _row_id(row: Mapping[str, Any]) -> Optional[str]:
    mid = _meta_of(row).get("mid")
    return mid if isinstance(mid, str) and ROW_ID_RE.fullmatch(mid) else None


def _not_forwarded(user: Mapping[str, Any], after: Sequence[Mapping[str, Any]]) -> bool:
    if _is_app_turn(user):
        return False
    replies = [
        str(r.get("content") or "").strip()
        for r in after
        if r.get("role") == "assistant" and str(r.get("content") or "").strip()
    ]
    if not replies:
        return False
    if any(r.get("role") in ("tool", "permission") and _names_forward_tool(r) for r in after):
        return False
    owner = str(user.get("content") or "").strip().lower()
    # contained, not a prefix: Claude Code can put its own notice before the agent's text
    return not (owner in CHOICE_WORDS or any(c in r for r in replies for c in CHOICE_REPLIES))


def analyse_rows(
    rows: Sequence[Mapping[str, Any]], judged_through: Optional[str] = None
) -> RowSignals:
    """The "not relayed" count and the rule notice from a chat's rows (the newest 200).

    A settled owner turn (one with a later user row after it) counts as not relayed when it has an
    assistant reply but no ``send_session_message`` or ``get_terminal_status`` call, unless the
    owner chose cancel or stop waiting or the reply is one of ``CHOICE_REPLIES``. App-posted turns
    (setup, relay ended) never count. Only turns after the user row whose id is ``judged_through``
    are judged; when that row is no longer in the window, every settled turn in it is newer, so all
    are judged. Kiro Crew gives every row a random id (``meta.mid``) that survives a restore; when
    a settled turn has none, ``placed`` is false and the caller cannot add the count."""
    turns: List[Tuple[Mapping[str, Any], List[Mapping[str, Any]]]] = []
    current: Optional[Tuple[Mapping[str, Any], List[Mapping[str, Any]]]] = None
    for row in rows:
        if row.get("role") == "user":
            if current is not None:
                turns.append(current)
            current = (row, [])
        elif current is not None:
            current[1].append(row)
    # the turn still open (``current``) is never judged
    ids = [_row_id(user) for user, _ in turns]
    placed = all(ids)
    start = ids.index(judged_through) + 1 if judged_through in ids else 0
    # turns after the judged one, by id; an id-less turn is never one of them, it reaches the stored
    # count only through ``window_total`` (raise-only), so no turn is counted twice or dropped
    count = sum(
        1
        for (user, after), rid in zip(turns[start:], ids[start:])
        if (placed or rid) and _not_forwarded(user, after)
    )
    total = sum(1 for user, after in turns if _not_forwarded(user, after))
    newest = next((i for i in reversed(ids) if i), judged_through)
    # any unanswered request: its title can be model-authored and Kiro Crew 0.12.2 records no
    # trusted tool identity on a Claude-backend row, so which tool it names is not read
    pending = any(
        row.get("role") == "permission" and not _meta_of(row).get("resolved") for row in rows
    )
    return RowSignals(count, total, newest, placed, pending)


# ------------------------------------------------------------------ the store and the logic
class Relays:
    def __init__(
        self,
        path: Path,
        actions: Actions,
        poller: Poller,
        settings: Callable[[], Dict[str, Any]],
        gateway: GatewayClient,
        *,
        clock: Callable[[], float] = time.time,
        ops_target: Optional[str] = None,
    ) -> None:
        self._path = path
        self._actions = actions
        self._poller = poller
        self._settings = settings
        self._gw = gateway
        self._clock = clock
        self._ops_target = ops_target
        self._lock = threading.RLock()  # the records
        self._op_lock = threading.RLock()  # one create, end or take over at a time
        self._records: List[Dict[str, Any]] = self._load()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._slot_due = True
        self._cao_checked_at = -1e18
        self._slot_checked_at = -1e18
        poller.set_relay_hooks(self.shelf_view, self.reconcile_views)

    # -------------------------------------------------------------- persistence
    def _load(self) -> List[Dict[str, Any]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            doc = json.loads(raw)
            rows = doc["relays"] if isinstance(doc, dict) else None
            if not isinstance(rows, list):
                raise ValueError("no relays list")
            out = [self._clean(r) for r in rows]
        except (ValueError, KeyError, TypeError):
            aside = move_aside(self._path, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
            log.warning("relays file was unreadable; moved aside to %s", aside.name)
            return []
        return [r for r in out if r is not None]

    @staticmethod
    def _clean(row: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(row, dict):
            raise ValueError("relay is not an object")
        rec = {k: row.get(k) for k in RECORD_FIELDS}
        if not isinstance(rec["relay_id"], str) or not RELAY_ID_RE.fullmatch(rec["relay_id"]):
            raise ValueError("bad relay id")
        if rec["state"] not in STATES or rec["origin"] not in ORIGINS:
            raise ValueError("bad state")
        rec["not_relayed"] = int(rec["not_relayed"] or 0)
        rec["permission_pending"] = bool(rec["permission_pending"])
        rec["ended_notice_pending"] = bool(rec["ended_notice_pending"])
        judged = rec["judged_through"]
        rec["judged_through"] = (
            judged if isinstance(judged, str) and ROW_ID_RE.fullmatch(judged) else None
        )
        return rec

    def _save(self) -> None:
        with self._lock:
            active = [r for r in self._records if r["state"] in ("attaching", "active")]
            rest = [r for r in self._records if r["state"] not in ("attaching", "active")]
            rest.sort(key=lambda r: str(r["updated_at"]), reverse=True)
            self._records = active + rest[:MAX_INACTIVE_KEPT]
            atomic_write_json(self._path, {"version": 1, "relays": self._records})

    def _changed(self) -> None:
        self._save()
        self._poller.refresh_relays()

    def _now(self) -> str:
        return _iso(self._clock())

    # -------------------------------------------------------------- views
    @staticmethod
    def summary(rec: Mapping[str, Any]) -> Dict[str, Any]:
        return {
            "relay_id": rec["relay_id"],
            "slot_key": rec["slot_key"] or "",
            "title": rec["title"] or "",
            "session": rec["session"],
            "conductor_id": rec["conductor_id"],
            "label": rec["label"],
            "origin": rec["origin"],
            "state": rec["state"],
            "end_reason": rec["end_reason"],
            "created_at": rec["created_at"],
            "updated_at": rec["updated_at"],
            "not_relayed": int(rec["not_relayed"]),
            "permission_pending": bool(rec["permission_pending"]),
            "ended_notice_pending": bool(rec.get("ended_notice_pending")),
        }

    def list(self) -> Dict[str, Any]:
        with self._lock:
            rows = [self.summary(r) for r in self._records if r["state"] != "attaching"]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return {"relays": rows}

    def shelf_view(self) -> Dict[str, Dict[str, Any]]:
        """The ``relay`` field of each session on the shelf: the active relay, else the newest one
        that needs re-attaching, else the newest ended one whose relay-ended message is still owed.
        ``ended_notice_pending`` says whether any chat of the session has not been told yet."""
        out: Dict[str, Dict[str, Any]] = {}
        rank = {"ended": 0, "orphaned": 0, "needs_reattach": 1, "active": 2}
        with self._lock:
            owed = {str(r["session"]) for r in self._records if r.get("ended_notice_pending")}
            shown = [
                r
                for r in self._records
                if r["state"] in CARD_STATES or r.get("ended_notice_pending")
            ]
            for rec in sorted(shown, key=lambda r: (rank[r["state"]], str(r["created_at"]))):
                out[str(rec["session"])] = {
                    "relay_id": rec["relay_id"],
                    "slot_key": rec["slot_key"] or "",
                    "title": rec["title"] or "",
                    "state": "needs_reattach" if rec["state"] == "orphaned" else rec["state"],
                    "label": rec["label"],
                    "not_relayed": int(rec["not_relayed"]),
                    "permission_pending": bool(rec["permission_pending"]),
                    "ended_notice_pending": str(rec["session"]) in owed,
                }
        return out

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for r in self._records if r["state"] == "active")

    def _get(self, relay_id: str) -> Dict[str, Any]:
        rid = check_relay_id(relay_id)
        with self._lock:
            for rec in self._records:
                if rec["relay_id"] == rid and rec["state"] != "attaching":
                    return rec
        raise ShelfError("not_found", "no such relay")

    def _active_for(self, session: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for rec in self._records:
                if rec["session"] == session and rec["state"] in ("attaching", "active"):
                    return rec
        return None

    # -------------------------------------------------------------- create
    def create(self, body: Any) -> Dict[str, Any]:
        """``POST /api/relays``: ``{session, label?}`` attaches; ``{start, label?}`` starts a
        session with the handshake as its first message and attaches to it."""
        req = validate_object(body, CREATE)
        label = req.get("label")
        if label is not None and has_control(label):
            raise ShelfError("invalid_request", "control characters are not allowed in a label")
        if ("session" in req) == ("start" in req):
            raise ShelfError("invalid_request", "give either session or start")
        check_risk_acknowledged(req)
        if "session" in req:
            session = check_name(req["session"])
            return self._attach(session, label or session, origin="attached")
        return self._start_and_attach(req["start"], label)

    def _start_and_attach(self, start: Dict[str, Any], label: Optional[str]) -> Dict[str, Any]:
        key = start["idempotency_key"]
        with self._op_lock:
            prior = self._by_start_key(key)
            if prior is not None:
                return self.summary(prior)  # a retry of a start that already has its relay
            # a relay that would be refused must not leave a started session behind
            self._check_cao_target()
            # the Start path, its validation and its idempotency memo, with the handshake first
            result = self._actions.start({**start, "message": HANDSHAKE})
            session = check_name(result["session"])
            conductor = str(result["conductor_id"])
            if TERMINAL_ID_RE.fullmatch(conductor) is None:
                raise ShelfError("cao_incompatible", "CAO did not name the new conductor")
            return self._attach(
                session,
                label or session,
                origin="started_by_relay",
                conductor_id=conductor,
                start_key=key,
            )

    def _by_start_key(self, key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            for rec in self._records:
                if rec["start_key"] == key and rec["state"] != "attaching":
                    return rec
        return None

    def _check_cao_target(self) -> str:
        url = validate_cao_url(self._settings()["cao_url"])
        if self._ops_target is not None and not _same_cao(url, self._ops_target):
            raise ShelfError(
                "relay_cao_mismatch",
                "the remote-control agent was installed for another CAO address than the one "
                "this page reads; run install.sh again after changing cao_url",
                detail={"agent_cao_url": self._ops_target},
            )
        return url

    def _attach(
        self,
        session: str,
        label: str,
        *,
        origin: str,
        conductor_id: Optional[str] = None,
        start_key: Optional[str] = None,
        replaces: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._op_lock:
            existing = self._active_for(session)
            if existing is not None and existing is not replaces:
                raise ShelfError(
                    "relay_exists",
                    "this session already has an active relay",
                    detail={"relay_id": existing["relay_id"], "label": existing["label"]},
                )
            url = self._check_cao_target()
            handshake_given = conductor_id is not None
            if conductor_id is None:
                conductor_id = self._actions.conductor_of_session(session)
                if conductor_id is None:
                    raise ShelfError(
                        "no_conductor",
                        "this session has no conductor (it was deleted), so there is nobody to "
                        "remote control",
                    )
            relay_id = "rc-" + secrets.token_hex(5)
            now = self._now()
            title = f"Remote control: {label}"
            rec: Dict[str, Any] = {
                "relay_id": relay_id,
                "slot_key": None,
                "title": title,
                "session": session,
                "conductor_id": conductor_id,
                "label": label,
                "origin": origin,
                "state": "attaching",
                "end_reason": None,
                "created_at": now,
                "updated_at": now,
                "ended_at": None,
                "not_relayed": 0,
                "permission_pending": False,
                "cao_url": url,
                "start_key": start_key,
                "ended_notice_pending": False,
                "judged_through": None,
            }
            with self._lock:
                self._records.append(rec)
            self._save()
            slot: Optional[str] = None
            try:
                slot = self._gw.create_slot(SLOT_PREFIX + relay_id, title)
                rec["slot_key"] = slot
                self._save()
                self._gw.set_mode(slot, "normal")
                self._gw.post_message(
                    slot, setup_text(relay_id, session, conductor_id, handshake_given)
                )
            except ShelfError:
                if slot is not None:
                    try:
                        self._gw.delete_slot(slot)
                    except ShelfError as err:
                        log.warning(
                            "relay attach: the new chat could not be removed (%s)", err.code
                        )
                with self._lock:
                    self._records = [r for r in self._records if r is not rec]
                self._save()
                raise
            with self._lock:
                rec["state"] = "active"
                rec["updated_at"] = self._now()
            out = self.summary(rec)
            if replaces is not None:
                # only from active: if the watch thread ended it meanwhile, its own message (and
                # reason) is owed; it still ends here, so no inactive relay is left behind
                if not self._finish(replaces, "ended", "taken_over"):
                    self._finish(
                        replaces, "ended", None, from_states=("orphaned", "needs_reattach")
                    )
                out["replaced_chat"] = self._deliver(replaces)
            self._changed()
            self._wake.set()
            log.info("relay attached origin=%s", origin)
            return out

    # -------------------------------------------------------------- end and take over
    def _finish(
        self,
        rec: Dict[str, Any],
        state: str,
        reason: Optional[str],
        *,
        from_states: Tuple[str, ...] = ("active",),
    ) -> bool:
        """Move ``rec`` to ``state``, only from ``from_states`` (a compare-and-set under the lock).
        Returns whether it moved. A reason with a chat to tell makes the relay-ended message owed
        (``ended_notice_pending``) until ``_deliver`` posts it."""
        with self._lock:
            if rec["state"] not in from_states:
                return False
            now = self._now()
            rec["state"] = state
            if reason is not None:
                rec["end_reason"] = reason
                if reason in END_REASONS and rec.get("slot_key"):
                    rec["ended_notice_pending"] = True
            rec["updated_at"] = now
            rec["ended_at"] = rec["ended_at"] or now
            rec["permission_pending"] = False
            return True

    def _deliver(self, rec: Dict[str, Any]) -> str:
        """Post the owed relay-ended message. ``notified`` or ``gone`` clear the debt;
        ``unreachable`` keeps it for the watch thread's next pass."""
        with self._lock:
            owed = bool(rec.get("ended_notice_pending"))
            slot, reason = rec.get("slot_key"), rec.get("end_reason")
        if not owed:
            return "gone" if reason == "chat_gone" or not slot else "notified"
        try:
            self._gw.post_message(str(slot), ended_text(str(rec["relay_id"]), str(reason)))
            result = "notified"
        except ShelfError as err:
            if err.code != "not_found":
                log.warning("relay-ended message not posted yet (%s)", err.code)
                return "unreachable"
            result = "gone"
        with self._lock:
            rec["ended_notice_pending"] = False
            rec["updated_at"] = self._now()
        return result

    def end(self, relay_id: str, body: Any) -> Dict[str, Any]:
        """``POST /api/relays/{id}/end``. Never calls CAO."""
        req = validate_object(body, END)
        with self._op_lock:
            rec = self._get(relay_id)
            if not self._finish(rec, "ended", "ended_by_owner"):
                # reconciled meanwhile: its own relay-ended message is (or was) owed already
                self._finish(rec, "ended", None, from_states=("orphaned", "needs_reattach"))
            if req.get("delete_chat"):
                try:
                    chat = "deleted" if self._gw.delete_slot(str(rec["slot_key"])) else "gone"
                except ShelfError as err:
                    log.warning("relay chat not deleted (%s)", err.code)
                    chat = "unreachable"  # the message stays owed: the chat still exists
                if chat != "unreachable":
                    with self._lock:
                        rec["ended_notice_pending"] = False
            else:
                chat = self._deliver(rec)
            self._changed()
            log.info("relay ended chat=%s", chat)
            return {"relay": self.summary(rec), "chat": chat}

    def takeover(self, relay_id: str, body: Any) -> Dict[str, Any]:
        """``POST /api/relays/{id}/takeover``: a new chat for the same session; the old relay
        ends only once the new one is attached."""
        req = validate_object(body, TAKEOVER)
        label = req.get("label")
        if label is not None and has_control(label):
            raise ShelfError("invalid_request", "control characters are not allowed in a label")
        with self._op_lock:
            rec = self._get(relay_id)
            if rec["state"] != "active":
                raise ShelfError(
                    "conflict_changed",
                    "that relay is no longer active; press Remote control to start a new one",
                    detail={"state": rec["state"]},
                )
            check_risk_acknowledged(req)
            return self._attach(
                str(rec["session"]), label or str(rec["label"]), origin="attached", replaces=rec
            )

    # -------------------------------------------------------------- reconcile
    def reconcile_views(
        self,
        listed: List[str],
        views: Mapping[str, List[Dict[str, Any]]],
        callers: Mapping[str, Optional[str]],
        live: Any,
    ) -> None:
        """Poller hook, after a successful read: CAO session gone -> ``orphaned``; conductor
        changed -> ``needs_reattach``. Uses what the poller already read, except that a relayed
        session missing from the listing is confirmed gone by its own read (``not_found``), as
        the watch thread does: CAO answers an empty listing with 200 when it fails inside."""
        url = validate_cao_url(self._settings()["cao_url"])
        changed = False
        with self._lock:
            missing = [
                rec
                for rec in self._records
                if rec["state"] == "active"
                and _same_cao(str(rec["cao_url"]), url)
                and SESSION_PREFIX + str(rec["session"]) not in listed
            ]
        gone = {id(rec) for rec in missing if self._session_gone(str(rec["session"]))}
        with self._lock:
            for rec in self._records:
                if rec["state"] != "active" or not _same_cao(str(rec["cao_url"]), url):
                    continue
                name = SESSION_PREFIX + str(rec["session"])
                if name not in listed:
                    if id(rec) in gone:
                        changed = self._finish(rec, "orphaned", "session_gone") or changed
                    continue
                rows = views.get(name)
                if rows is None:
                    continue  # listed but not read this tick: unknown is not gone
                if self._conductor_moved(rec, rows, callers, live):
                    changed = self._finish(rec, "needs_reattach", "conductor_changed") or changed
            self._slot_due = True
        if changed:
            self._changed()
        self._wake.set()

    def _session_gone(self, session: str) -> bool:
        """True only when CAO answers ``not_found`` for the session now; any other answer or
        error keeps the relay for a later pass (unknown is not gone)."""
        try:
            self._actions.conductor_of_session(session)
        except ShelfError as err:
            return err.code == "not_found"
        return False

    @staticmethod
    def _conductor_moved(
        rec: Mapping[str, Any],
        rows: List[Dict[str, Any]],
        callers: Mapping[str, Optional[str]],
        live: Any,
    ) -> bool:
        ids = [str(r.get("id")) for r in rows if TERMINAL_ID_RE.fullmatch(str(r.get("id")))]
        if rec["conductor_id"] not in ids:
            return True
        if not all(i in callers for i in ids):
            return False  # not every caller is known yet: judge on a later tick
        usable = [r for r in rows if TERMINAL_ID_RE.fullmatch(str(r.get("id")))]
        return bool(tiering.pick_conductor(usable, callers, live) != rec["conductor_id"])

    def flush(self) -> None:
        """Post every owed relay-ended message (the watch thread does this on each pass, so one that
        could not be posted is posted once Kiro Crew answers again). Under the operation lock, so
        an End or Take over in flight never posts the same message twice."""
        with self._op_lock:
            with self._lock:
                owed = [r for r in self._records if r.get("ended_notice_pending")]
            results = [self._deliver(rec) for rec in owed]
            if any(r != "unreachable" for r in results):
                self._save()

    def check(self, *, force: bool = False) -> None:
        """One watch pass: CAO side (every 60 s, or when forced) and chat side (when due) for
        every active relay."""
        self.flush()
        now = self._clock()
        with self._lock:
            active = [r for r in self._records if r["state"] == "active"]
        if not active:
            return
        if force or now - self._cao_checked_at >= WATCH_SECONDS:
            self._cao_checked_at = now
            self._check_cao(active)
        if force or self._slot_due or now - self._slot_checked_at >= WATCH_SECONDS:
            self._slot_due = False
            self._slot_checked_at = now
            self._check_slots()
        self.flush()

    def _check_cao(self, active: List[Dict[str, Any]]) -> None:
        url = validate_cao_url(self._settings()["cao_url"])
        changed = False
        for rec in active:
            if rec["state"] != "active" or not _same_cao(str(rec["cao_url"]), url):
                continue
            try:
                conductor = self._actions.conductor_of_session(str(rec["session"]))
            except ShelfError as err:
                if err.code == "not_found":
                    changed = self._finish(rec, "orphaned", "session_gone") or changed
                    continue
                if err.code in TRANSPORT_CODES:
                    break  # CAO is not answering: judge nothing now
                continue
            if conductor != rec["conductor_id"]:
                changed = self._finish(rec, "needs_reattach", "conductor_changed") or changed
        if changed:
            self._changed()

    def _check_slots(self) -> None:
        changed = False
        with self._lock:
            active = [r for r in self._records if r["state"] == "active" and r["slot_key"]]
        for rec in active:
            slot = str(rec["slot_key"])
            try:
                rows = self._gw.slot_rows(slot)
            except ShelfError as err:
                if err.code == "not_found":
                    changed = self._finish(rec, "ended", "chat_gone") or changed
                elif err.code == "gateway_unavailable":
                    break
                continue
            with self._lock:
                judged = rec.get("judged_through")
            seen = analyse_rows(rows, judged)
            with self._lock:
                if rec["state"] != "active":
                    continue  # ended while its rows were read
                new_count = int(rec["not_relayed"]) + seen.not_relayed
                if not seen.placed:  # id-less turns can only raise it to the window's count
                    new_count = max(new_count, seen.window_total)
                pending = seen.permission_pending
                if (
                    new_count != rec["not_relayed"]
                    or pending != rec["permission_pending"]
                    or seen.judged_through != judged
                ):
                    rec["not_relayed"] = new_count
                    rec["permission_pending"] = pending
                    rec["judged_through"] = seen.judged_through
                    rec["updated_at"] = self._now()
                    changed = True
        if changed:
            self._changed()

    # -------------------------------------------------------------- the watch thread
    def startup(self) -> None:
        """At backend start: drop half-made relays (and their chats), then check the rest."""
        with self._lock:
            half = [r for r in self._records if r["state"] == "attaching"]
            self._records = [r for r in self._records if r["state"] != "attaching"]
        for rec in half:
            if rec["slot_key"]:
                try:
                    self._gw.delete_slot(str(rec["slot_key"]))
                except ShelfError as err:
                    log.warning("half-made relay chat not removed (%s)", err.code)
        if half:
            self._changed()
        self.check(force=True)

    def _run(self) -> None:
        try:
            self.startup()
        except Exception as exc:  # the thread must never die
            log.error("relay start-up pass failed: %s", type(exc).__name__)
        while not self._stop.is_set():
            self._wake.wait(WATCH_SECONDS)
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self.check()
            except Exception as exc:
                log.error("relay check failed: %s", type(exc).__name__)

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="shelf-relays", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(5)


AGENT_NAME = "cao-remote-control"
OPS_SERVER = "cao-shelf:ops"


def ops_target_from_manifest(path: Path) -> Optional[str]:
    """The CAO address the installed relay agent's ops server points at, or None when unknown.

    The server lives only in the agent's own file (``agents/cao-remote-control.json``, entry
    ``cao-shelf:ops``), never in the manifest: Kiro Crew writes every manifest ``mcpServers`` entry
    into the host's default agent config, which would hand all of cao-ops-mcp to ordinary chats."""
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        env: Any = None
        for rel in manifest["agents"]:
            agent = json.loads((path.parent / str(rel)).resolve().read_text(encoding="utf-8"))
            if agent.get("name") == AGENT_NAME:
                env = agent["mcpServers"][OPS_SERVER]["env"]
        host, port = str(env["CAO_API_HOST"]).strip("[]"), int(env["CAO_API_PORT"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    shown = f"[{host}]" if ":" in host else host
    try:
        return validate_cao_url(f"http://{shown}:{port}")
    except ShelfError:
        return None
