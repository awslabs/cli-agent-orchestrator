#!/usr/bin/env python3
"""Generate the anonymised CAO fixtures and the expected Shelf outputs.

stdlib only. Run from anywhere: ``python3 fixtures/_gen/make_fixtures.py``.

The CAO response *shapes* below were recorded from a scratch cao-server (main at
31393711, ``mock_cli`` provider) and from GET-only reads of a live 2.5.4 server;
see SPIKE.md. Every name, id and timestamp here is invented. The fleet is the
shared one from design/concept-11.md: 12 sessions, 20 terminals, 20 closed helpers.

``reference_tier`` / ``build_shelf`` are the ORACLE for workstream A: the production
code in backend/ must produce byte-identical ``expected/*.json`` for the same input.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 15, 12, 0, 0, tzinfo=timezone.utc)
PARKED_HOURS = 1.0  # below this an idle session is "warm" (a label, not a tier)
STALE_HOURS = 48.0  # idle at or beyond this is STALE


def hid(*parts: str) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()[:8]


def iso(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# (session, conductor_age_h, [(label, profile, age_h, kind)], [(closed_label, closed_ago_h)])
FLEET = [
    (
        "payments-fix",
        2,
        [
            ("coder-1", "coder", 2, "h"),
            ("tester", "tester", 2, "h"),
            ("reviewer", "reviewer", 3, "h"),
        ],
        [("planner", 26), ("coder-2", 22), ("linter", 1.6)],
    ),
    ("docs-refresh", 5, [("writer-1", "writer", 5, "h")], [("outliner", 6), ("link-check", 30)]),
    (
        "migrate-db",
        17,
        [],
        [("schema-diff", 16), ("backup-check", 20), ("dry-run", 28), ("index-plan", 40)],
    ),
    ("search-tuning", 52, [("probe-1", "prober", 52, "orphan")], [("baseline", 60)]),
    ("onboarding-flow", 48.3, [("yoda", "advisor", 48.3, "peer")], []),
    ("infra-audit", 216, [], []),
    ("release-notes", 76, [], [("drafter", 80), ("proofreader", 90), ("tagger", 100)]),
    (
        "api-cleanup",
        100,
        [("refactor-1", "coder", 100, "h")],
        [("mapper", 110), ("deprecator", 120)],
    ),
    ("perf-probe", 128, [], [("profiler", 130)]),
    ("ui-polish", 150, [("css-fix", "coder", 150, "h")], [("screenshotter", 160)]),
    ("data-backfill", 200, [], [("loader", 210), ("verifier", 215)]),
    ("ci-flakes", 288, [], [("reruner", 300)]),
]
GONE_CALLER = "dead0001"  # a terminal id that no longer exists (orphan evidence)


def build_day(busy: bool):
    sessions = []
    for name, c_age, helpers, closed in FLEET:
        cond = {
            "id": hid(name, "conductor"),
            "label": "conductor",
            "profile": "supervisor",
            "status": "idle",
            "age": c_age,
            "caller_id": None,
            "kind": "conductor",
        }
        terms = [cond]
        for label, profile, age, kind in helpers:
            caller = cond["id"] if kind == "h" else (GONE_CALLER if kind == "orphan" else None)
            terms.append(
                {
                    "id": hid(name, label),
                    "label": label,
                    "profile": profile,
                    "status": "idle",
                    "age": age,
                    "caller_id": caller,
                    "kind": kind,
                }
            )
        sessions.append({"name": name, "terms": terms, "closed": closed})
    if busy:
        pf, dr = sessions[0]["terms"], sessions[1]["terms"]
        pf[0].update(status="processing", age=0.0)
        pf[1].update(status="processing", age=0.0)
        pf[2].update(status="waiting_user_answer", age=4 / 60)
        pf[3].update(age=0.1)
        dr[0].update(status="processing", age=0.0)
        dr[1].update(status="processing", age=0.0)
    return sessions


def cao_term_row(session: str, t: dict, full: bool) -> dict:
    """A terminal row as GET /sessions/{name} returns it (recorded key set)."""
    row = {
        "id": t["id"],
        "tmux_session": f"cao-{session}",
        "tmux_window": t["label"],
        "provider": "mock_cli",
        "agent_profile": t["profile"],
        "description": None,
        "model": None,
        "model_honored": None,
        "ephemeral": False,
        "working_directory": "/work/example",
        "engine": None,
        "deferred_init_failure": None,
        "deferred_init_external_owner": False,
        "deferred_init_runtime_reclaimed": False,
        "session_incarnation_id": hid(session, "inc"),
        "kept_until": None,
        "last_active": iso(t["age"]),
        "pending_inbox_count": 0,
        "status": t["status"],
    }
    return row


def inbox_rows(sess: dict, busy: bool) -> list[dict]:
    """Newest first, as GET /terminals/{conductor}/inbox/messages?order=desc returns."""
    cond = sess["terms"][0]["id"]
    rows, rid = [], 1
    for label, ago in sess["closed"]:
        sid = hid(sess["name"], "closed", label)
        for k, (off, text) in enumerate(
            [
                (0.5, "READY: started, working on the assignment"),
                (0.2, "progress: halfway, no blockers"),
                (0.0, f"DONE: {label} finished"),
            ]
        ):
            rows.append(
                {
                    "id": rid,
                    "sender_id": sid,
                    "receiver_id": cond,
                    "message": text,
                    "status": "delivered",
                    "created_at": (NOW - timedelta(hours=ago + off)).isoformat(),
                }
            )
            rid += 1
    if sess["name"] == "migrate-db":  # one row from outside CAO (ops tool), never a helper
        rows.append(
            {
                "id": rid,
                "sender_id": "cao-ops-mcp",
                "receiver_id": cond,
                "message": "please re-run the dry run",
                "status": "delivered",
                "created_at": (NOW - timedelta(hours=30)).isoformat(),
            }
        )
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return rows


# ---------------- reference logic (the oracle) -----------------
def tier_of(terms: list[dict]) -> tuple[str, str]:
    st = {t["status"] for t in terms}
    for s in ("waiting_user_answer", "error"):
        if s in st:
            return "needs_you", s
    if "processing" in st:
        return "live", "processing"
    youngest = min(t["age"] for t in terms)
    # CAO latches `completed`: Live only while just completed, then tiered by age like idle
    if "completed" in st and youngest < PARKED_HOURS:
        return "live", "completed"
    return (
        ("stale", "idle_over_stale_hours")
        if youngest >= STALE_HOURS
        else ("parked", "idle_under_stale_hours")
    )


def secs(h: float) -> int:
    return int(round(h * 3600))


def term_view(t: dict, live_ids: set[str], cond_id: str) -> dict:
    cid = t["caller_id"]
    if t["kind"] == "conductor":
        role = "conductor"
    elif cid is None:
        role = "peer"
    else:
        role = "helper"
    return {
        "id": t["id"],
        "label": t["label"],
        "profile": t["profile"],
        "provider": "mock_cli",
        "status": t["status"],
        "last_active": iso(t["age"]),
        "idle_seconds": secs(t["age"]),
        "role": role,
        "caller_id": cid,
        "orphan": cid is not None and cid not in live_ids,
        "pending_inbox_count": 0,
    }


def build_shelf(day: str, sessions: list[dict]) -> dict:
    live_ids = {t["id"] for s in sessions for t in s["terms"]}
    out, counts = [], {
        "processing": 0,
        "waiting_user_answer": 0,
        "error": 0,
        "completed": 0,
        "idle": 0,
        "unknown": 0,
    }
    tiers = {"needs_you": 0, "live": 0, "parked": 0, "stale": 0}
    for s in sessions:
        tier, reason = tier_of(s["terms"])
        tiers[tier] += 1
        for t in s["terms"]:
            counts[t["status"]] += 1
        views = [term_view(t, live_ids, s["terms"][0]["id"]) for t in s["terms"]]
        youngest = min(s["terms"], key=lambda t: t["age"])
        proc_helper = next(
            (
                v
                for v in views[1:]
                if v["status"] in ("processing", "waiting_user_answer")
                and v["caller_id"] == views[0]["id"]
            ),
            None,
        )
        out.append(
            {
                "name": s["name"],
                "cao_name": f"cao-{s['name']}",
                "tier": tier,
                "tier_reason": reason,
                "warm": tier == "parked" and youngest["age"] < PARKED_HOURS,
                "idle_seconds": secs(youngest["age"]),
                "last_active": iso(youngest["age"]),
                "poll_class": (
                    "hot"
                    if tier in ("needs_you", "live") or youngest["age"] < PARKED_HOURS
                    else ("parked" if tier == "parked" else "stale")
                ),
                "conductor_missing": False,
                "conductor": views[0],
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
                    {"helper_terminal_id": proc_helper["id"], "label": proc_helper["label"]}
                    if views[0]["status"] == "processing" and proc_helper
                    else None
                ),
                "history_count": len(s["closed"]),
                "relay": None,
            }
        )
    order = {"needs_you": 0, "live": 1, "parked": 2, "stale": 3}
    out.sort(key=lambda x: (order[x["tier"]], x["idle_seconds"], x["name"]))
    return {
        "revision": 1,
        "generated_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_good_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stale_snapshot": False,
        "snapshot_age_seconds": 0,
        "cao": {
            "reachable": True,
            "base_url": "http://127.0.0.1:9889",
            "version": "2.5.4",
            "terminal_backend": "tmux",
            "error": None,
        },
        "thresholds": {"warm_hours": PARKED_HOURS, "stale_hours": STALE_HOURS},
        "counts": {
            "sessions": len(out),
            "terminals": sum(counts.values()),
            "by_status": counts,
            "by_tier": tiers,
            "hidden_sessions": 0,
        },
        "history_total": sum(len(s["closed"]) for s in sessions),
        "sessions": out,
    }


def history_view(sess: dict, rows: list[dict], live_ids: set[str]) -> dict:
    by: dict[str, list[dict]] = {}
    ext: dict[str, int] = {}
    for r in rows:
        sid = r["sender_id"]
        if len(sid) == 8 and all(c in "0123456789abcdef" for c in sid):
            if sid not in live_ids:
                by.setdefault(sid, []).append(r)
        else:
            ext[sid] = ext.get(sid, 0) + 1
    closed = []
    for sid, rs in by.items():
        rs.sort(key=lambda r: r["created_at"])
        closed.append(
            {
                "terminal_id": sid,
                "label": None,
                "rows": len(rs),
                "first_at": rs[0]["created_at"],
                "last_at": rs[-1]["created_at"],
                "last_status": rs[-1]["status"],
            }
        )
    closed.sort(key=lambda c: c["last_at"], reverse=True)
    return {
        "session": sess["name"],
        "source": "inbox_rows",
        "complete": len(rows) < 100,
        "window_rows": 100,
        "rows_seen": len(rows),
        "closed": closed,
        "external_senders": [{"sender_id": k, "rows": v} for k, v in sorted(ext.items())],
    }


def history_all(sessions: list[dict], busy: bool, live_ids: set[str]) -> dict:
    """GET /api/history: every closed helper of every session, newest last message first."""
    closed, complete = [], True
    for s in sessions:
        view = history_view(s, inbox_rows(s, busy), live_ids)
        complete = complete and view["complete"]
        closed.extend(
            {
                "session": s["name"],
                "terminal_id": c["terminal_id"],
                "rows": c["rows"],
                "last_at": c["last_at"],
            }
            for c in view["closed"]
        )
    closed.sort(key=lambda c: c["last_at"], reverse=True)
    return {"closed": closed, "complete": complete}


def dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=False) + "\n")


def main() -> None:
    for day, busy in (("quiet", False), ("busy", True)):
        sessions = build_day(busy)
        base = ROOT / "cao" / day
        dump(base / "clock.json", {"now": NOW.strftime("%Y-%m-%dT%H:%M:%SZ")})
        dump(
            base / "sessions.json",
            [
                {
                    "id": f"cao-{s['name']}",
                    "name": f"cao-{s['name']}",
                    "status": "detached",
                    "working_directory": "/work/example",
                    "agent_profile": "supervisor",
                }
                for s in sessions
            ],
        )
        for s in sessions:
            rows = [cao_term_row(s["name"], t, True) for t in s["terms"]]
            dump(
                base / f"session.{s['name']}.json",
                {
                    "session": {
                        "id": f"cao-{s['name']}",
                        "name": f"cao-{s['name']}",
                        "status": "detached",
                    },
                    "terminals": rows,
                },
            )
            for t in s["terms"][1:]:
                d = dict(cao_term_row(s["name"], t, True))
                d.update(session_name=f"cao-{s['name']}", name=t["label"], caller_id=t["caller_id"])
                dump(base / f"terminal.{t['id']}.json", d)
            dump(base / f"inbox.{s['terms'][0]['id']}.json", inbox_rows(s, busy))
        live_ids = {t["id"] for s in sessions for t in s["terms"]}
        dump(ROOT / "expected" / f"{day}.shelf.json", build_shelf(day, sessions))
        for s in sessions:
            dump(
                ROOT / "expected" / day / f"history.{s['name']}.json",
                history_view(s, inbox_rows(s, busy), live_ids),
            )
        dump(ROOT / "expected" / day / "all.history.json", history_all(sessions, busy, live_ids))
    print("fixtures written under", ROOT)


if __name__ == "__main__":
    main()
