"""Pure projection of CAO's session reads into the shelf (no I/O, no clock reads).

Inputs are plain data and ``now`` is a parameter, so every function here is a unit under test and
the module ports as is. The reference implementation (the oracle) is ``fixtures/_gen``; the
semantics are written down in api-contract.md, "Shelf semantics".
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

STATUSES = ("processing", "waiting_user_answer", "error", "completed", "idle", "unknown")
TIERS = ("needs_you", "live", "parked", "stale")
SESSION_PREFIX = "cao-"

_ID_RE = re.compile(r"[a-f0-9]{8}")
_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,58}")
_FRACTION_RE = re.compile(r"\.(\d+)")

DEFAULT_CAO: Dict[str, Any] = {
    "reachable": True,
    "base_url": "http://127.0.0.1:9889",
    "version": None,
    "terminal_backend": None,
    "error": None,
}


def parse_time_or_none(value: Any) -> Optional[datetime]:
    """Parse an ISO 8601 time (``Z`` or an offset). Naive is UTC; unparsable gives ``None``."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # fromisoformat takes 3 or 6 fractional digits only on some versions: normalise to 6
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_time(value: Any, default: datetime) -> datetime:
    """Like ``parse_time_or_none`` but an unreadable time gives ``default``."""
    parsed = parse_time_or_none(value)
    return default if parsed is None else parsed


def _age_seconds(last_active: datetime, now: datetime) -> float:
    return max(0.0, (now - last_active).total_seconds())


def _status(value: Any) -> str:
    return value if isinstance(value, str) and value in STATUSES else "unknown"


def _bare_name(cao_name: str) -> Optional[str]:
    if not cao_name.startswith(SESSION_PREFIX):
        return None
    bare = cao_name[len(SESSION_PREFIX) :]
    return bare if _NAME_RE.fullmatch(bare) else None


def _rows(raw: Sequence[Mapping[str, Any]]) -> List[Mapping[str, Any]]:
    """Terminal rows that can be addressed (valid 8-hex id), in CAO's order."""
    out = []
    for row in raw:
        tid = row.get("id") if isinstance(row, Mapping) else None
        if isinstance(tid, str) and _ID_RE.fullmatch(tid):
            out.append(row)
    return out


class _EveryId:
    """``in`` is true for every id: used while a listed session was not read, so a caller that is
    not among the read terminals may still be alive there."""

    def __contains__(self, item: object) -> bool:
        return True


EVERY_ID = _EveryId()


def pick_conductor(
    rows: Sequence[Mapping[str, Any]],
    caller_ids: Mapping[str, Optional[str]],
    live_ids: Any,
) -> Optional[str]:
    """The conductor's id, or ``None`` when the session has none.

    The conductor is the first row (CAO lists the oldest terminal first) unless that row has a
    caller: then it is a helper and the first row without a caller takes the title. When every row
    has a caller, a first row whose caller is alive stays the title (its caller lives elsewhere);
    one whose caller no longer exists is an orphaned helper and the session has no conductor.
    ``rows`` must be addressable.
    """
    if not rows:
        return None
    first = rows[0]["id"]
    caller = caller_ids.get(first)
    if not caller:
        return str(first)
    for row in rows:
        if not caller_ids.get(row["id"]):
            return str(row["id"])
    return str(first) if caller in live_ids else None


def assign_roles(
    rows: Sequence[Mapping[str, Any]],
    caller_ids: Mapping[str, Optional[str]],
    live_ids: Any,
) -> Dict[str, str]:
    """Role of each terminal id: ``conductor``, ``helper`` or ``peer``.

    A session without a conductor (see ``pick_conductor``) has no ``conductor`` entry.
    """
    rows = _rows(rows)
    if not rows:
        return {}
    conductor = pick_conductor(rows, caller_ids, live_ids)
    roles: Dict[str, str] = {}
    for row in rows:
        tid = str(row["id"])
        if tid == conductor:
            roles[tid] = "conductor"
        else:
            roles[tid] = "helper" if caller_ids.get(tid) else "peer"
    return roles


def classify(
    statuses: Sequence[str],
    youngest_age: float,
    warm_hours: float,
    stale_hours: float,
    age_known: bool = True,
) -> Tuple[str, str, bool]:
    """``(tier, tier_reason, warm)`` from a session's statuses and the age of its youngest row.

    A session that has any terminal with an unreadable ``last_active`` (``age_known`` false) is
    never ``stale`` and never ``warm``: a clock that cannot be read must not look fresh, and it must
    not make a session eligible for clean-up either.

    CAO latches ``completed`` until new input arms the terminal, so it means "at a prompt" as
    ``idle`` does: it is ``live`` only while the youngest row is under ``warm_hours`` old (just
    completed), and then tiered by age like ``idle``.
    """
    present = set(statuses)
    for status in ("waiting_user_answer", "error"):
        if status in present:
            return "needs_you", status, False
    if "processing" in present:
        return "live", "processing", False
    if "completed" in present and age_known and youngest_age < warm_hours * 3600:
        return "live", "completed", False
    if not age_known:
        return "parked", "age_unknown", False
    if youngest_age >= stale_hours * 3600:
        return "stale", "idle_over_stale_hours", False
    return "parked", "idle_under_stale_hours", youngest_age < warm_hours * 3600


def session_age(rows: Sequence[Mapping[str, Any]], now: datetime) -> Tuple[float, bool]:
    """``(age in seconds of the youngest readable last_active, every clock readable)``."""
    ages = []
    known = True
    for row in rows:
        moment = parse_time_or_none(row.get("last_active"))
        if moment is None:
            known = False
        else:
            ages.append(_age_seconds(moment, now))
    return (min(ages) if ages else 0.0), known and bool(ages)


def _poll_class(tier: str, warm: bool) -> str:
    if tier in ("needs_you", "live") or warm:
        return "hot"
    return "parked" if tier == "parked" else "stale"


def _terminal(
    row: Mapping[str, Any],
    role: str,
    caller_ids: Mapping[str, Optional[str]],
    live_ids: Any,
    orphans_possible: bool,
    now: datetime,
) -> Dict[str, Any]:
    tid = str(row["id"])
    last = parse_time_or_none(row.get("last_active"))
    caller = caller_ids.get(tid) or None
    pending = row.get("pending_inbox_count")
    profile = row.get("agent_profile")
    label = row.get("tmux_window")
    provider = row.get("provider")
    return {
        "id": tid,
        "label": label if isinstance(label, str) else "",
        "profile": profile if isinstance(profile, str) else None,
        "provider": provider if isinstance(provider, str) else "",
        "status": _status(row.get("status")),
        # "" when CAO's clock cannot be read: the page says "age unknown", never "now"
        "last_active": row["last_active"] if last is not None else "",
        "idle_seconds": 0 if last is None else int(_age_seconds(last, now)),
        "role": role,
        "caller_id": caller,
        "orphan": bool(caller) and orphans_possible and caller not in live_ids,
        "pending_inbox_count": pending if isinstance(pending, int) and pending >= 0 else 0,
    }


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_shelf(
    cao_view: Mapping[str, Optional[Sequence[Mapping[str, Any]]]],
    caller_ids: Mapping[str, Optional[str]],
    now: datetime,
    thresholds: Mapping[str, float],
    ledger: Mapping[str, Optional[int]],
    *,
    cao: Optional[Mapping[str, Any]] = None,
    revision: int = 1,
    stale_snapshot: bool = False,
    snapshot_age_seconds: int = 0,
    hidden_sessions: int = 0,
    last_good_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the ``shelf.schema.json`` document.

    ``cao_view`` maps CAO session names (with the ``cao-`` prefix) to the terminal rows of
    ``GET /sessions/{name}``; ``None`` means the session was listed but never read, so it is not
    shown and it also disables orphan marking (an unread session may hold the caller).
    ``caller_ids`` maps terminal id to ``caller_id`` (absent or ``None`` means none known).
    ``ledger`` maps a bare session name to its closed-helper count (``None`` until read).
    ``hidden_sessions`` is the number of ``cao-`` sessions the caller already left out because
    their name cannot be addressed; names dropped here are added to it.
    """
    warm_h = float(thresholds["warm_hours"])
    stale_h = float(thresholds["stale_hours"])
    read: Dict[str, List[Mapping[str, Any]]] = {}
    unread = False
    hidden = hidden_sessions
    for cao_name, raw in cao_view.items():
        bare = _bare_name(cao_name)
        if bare is None:
            if cao_name.startswith(SESSION_PREFIX):
                hidden += 1
            continue
        if raw is None:
            unread = True
            continue
        rows = _rows(raw)
        if rows:
            read[bare] = rows
    live_ids = {str(r["id"]) for rows in read.values() for r in rows}
    orphans_possible = not unread

    by_status = {s: 0 for s in STATUSES}
    by_tier = {t: 0 for t in TIERS}
    sessions: List[Dict[str, Any]] = []
    counts_known = not unread
    for bare, rows in read.items():
        roles = assign_roles(rows, caller_ids, live_ids if orphans_possible else EVERY_ID)
        conductor_id = next((t for t, role in roles.items() if role == "conductor"), None)
        # without a conductor the first row still heads the card, as the orphaned helper it is
        head_id = conductor_id if conductor_id is not None else str(rows[0]["id"])
        ordered = [r for r in rows if r["id"] == head_id] + [r for r in rows if r["id"] != head_id]
        views = [
            _terminal(r, roles[str(r["id"])], caller_ids, live_ids, orphans_possible, now)
            for r in ordered
        ]
        for view in views:
            by_status[view["status"]] += 1
        readable = [v for v in views if v["last_active"]]
        youngest_at = (
            max(readable, key=lambda v: parse_time(v["last_active"], now))["last_active"]
            if readable
            else ""
        )
        youngest_age, age_known = session_age(ordered, now)
        tier, reason, warm = classify(
            [v["status"] for v in views], youngest_age, warm_h, stale_h, age_known
        )
        by_tier[tier] += 1
        head = views[0]
        in_flight = next(
            (
                v
                for v in views[1:]
                if v["status"] in ("processing", "waiting_user_answer")
                and v["caller_id"] == head["id"]
            ),
            None,
        )
        count = ledger.get(bare)
        if count is None:
            counts_known = False
        sessions.append(
            {
                "name": bare,
                "cao_name": SESSION_PREFIX + bare,
                "tier": tier,
                "tier_reason": reason,
                "warm": warm,
                "idle_seconds": int(youngest_age),
                "last_active": youngest_at,
                "poll_class": _poll_class(tier, warm),
                "conductor_missing": conductor_id is None,
                "conductor": head,
                "helpers": views[1:],
                "terminal_count": len(views),
                "asks": [
                    {
                        "terminal_id": v["id"],
                        "label": v["label"],
                        "status": v["status"],
                        "since": v["last_active"],
                        "since_is_approximate": True,
                    }
                    for v in views
                    if v["status"] in ("waiting_user_answer", "error")
                ],
                "in_flight": (
                    {"helper_terminal_id": in_flight["id"], "label": in_flight["label"]}
                    if conductor_id is not None and head["status"] == "processing" and in_flight
                    else None
                ),
                "history_count": count,
                "relay": None,
            }
        )
    order = {t: i for i, t in enumerate(TIERS)}
    sessions.sort(key=lambda s: (order[s["tier"]], s["idle_seconds"], s["name"]))
    total: Optional[int] = None
    if counts_known:
        total = sum(int(s["history_count"] or 0) for s in sessions)
    return {
        "revision": revision,
        "generated_at": _iso(now),
        "last_good_at": last_good_at if last_good_at is not None else _iso(now),
        "stale_snapshot": stale_snapshot,
        "snapshot_age_seconds": snapshot_age_seconds,
        "cao": dict(cao if cao is not None else DEFAULT_CAO),
        "thresholds": {"warm_hours": warm_h, "stale_hours": stale_h},
        "counts": {
            "sessions": len(sessions),
            "terminals": sum(by_status.values()),
            "by_status": by_status,
            "by_tier": by_tier,
            "hidden_sessions": hidden,
        },
        "history_total": total,
        "sessions": sessions,
    }


def empty_shelf(
    now: datetime,
    thresholds: Mapping[str, float],
    cao: Mapping[str, Any],
    *,
    revision: int = 0,
) -> Dict[str, Any]:
    """The answer when CAO cannot be read and there is no earlier snapshot.

    The UI must not render this as "no sessions": ``cao.reachable`` is false and ``cao.error``
    says why.
    """
    shelf = build_shelf({}, {}, now, thresholds, {}, cao=cao, revision=revision)
    shelf["history_total"] = None
    shelf["last_good_at"] = None  # nothing was ever read
    return shelf


def change_signature(shelf: Mapping[str, Any]) -> str:
    """A string that changes when the shelf changes in a way a viewer would notice.

    Clock-derived fields (``generated_at``, ``last_good_at``, ``snapshot_age_seconds``, every
    ``idle_seconds``) are left out: the page computes ages from ``last_active`` (and the age of a
    stale snapshot from ``last_good_at``) against its own ticking clock. ``last_good_at`` only
    matters while ``stale_snapshot`` is true and it does not move then; the flip of
    ``stale_snapshot`` and ``cao`` is in the signature, so the page receives it in full. The
    tier, ``warm`` and ``poll_class`` fields already carry every threshold the clock can cross.
    An unchanged fleet therefore keeps its revision and the page keeps getting the ``unchanged``
    stub.
    """

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return json.dumps(strip(shelf), sort_keys=True, separators=(",", ":"))


_VOLATILE = frozenset(
    {"revision", "generated_at", "last_good_at", "snapshot_age_seconds", "idle_seconds"}
)
