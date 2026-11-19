#!/usr/bin/env python3
"""Seed and manipulate the SCRATCH cao-server with ``mock_cli`` sessions (stdlib only).

    python3 seed_cao.py seed --day busy [--port 9990]
    python3 seed_cao.py closed-helper --session payments-fix        (J3: helper posts a row, then is deleted)
    python3 seed_cao.py orphan --session search-tuning              (J4: helper whose caller was deleted)
    python3 seed_cao.py backdate --session NAME --hours 60 [--cao-home DIR]   (J7: test-only DB rewrite)
    python3 seed_cao.py names | terminals --session NAME | wait | clear

Safety: the port must be loopback and must not be 9889 (the owner's live CAO); ``clear`` deletes only
sessions listed by that scratch server. Names and text are generic (``payments-fix``, ``coder-1``).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

FORBIDDEN_PORT = 9889
HOST = "127.0.0.1"
DEFAULT_PORT = 9990

# (session, [(helper label, kind)]). Mirrors fixtures/_gen FLEET, except that a real CAO refuses a terminal
# without caller_id (403), so the fixture "peer" role exists only in the fake CAO; here it is a helper.
FLEET: list[tuple[str, list[tuple[str, str]]]] = [
    ("payments-fix", [("coder-1", "h"), ("tester", "h"), ("reviewer", "h")]),
    ("docs-refresh", [("writer-1", "h")]),
    ("migrate-db", []),
    ("search-tuning", [("probe-1", "h")]),
    ("onboarding-flow", [("advisor", "h")]),
    ("infra-audit", []),
    ("release-notes", []),
    ("api-cleanup", [("refactor-1", "h")]),
    ("perf-probe", []),
    ("ui-polish", [("css-fix", "h")]),
    ("data-backfill", []),
    ("ci-flakes", []),
]
SLEEP_SECONDS = 1800  # a "processing" terminal stays busy for the whole run


class Cao:
    def __init__(self, port: int) -> None:
        if port == FORBIDDEN_PORT:
            sys.exit("seed_cao: refusing port 9889 (the owner's live CAO)")
        self.base = f"http://{HOST}:{port}"

    def call(
        self, method: str, path: str, params: dict[str, str] | None = None, body: Any = None
    ) -> Any:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:  # nosec - loopback only
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:200].decode("utf-8", "replace")
            raise SystemExit(f"seed_cao: {method} {path} -> HTTP {exc.code} {detail}") from exc
        return json.loads(raw) if raw else None

    def wait(self, seconds: float = 30.0) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                self.call("GET", "/health")
                return
            except (SystemExit, OSError):
                time.sleep(0.5)
        sys.exit("seed_cao: scratch CAO did not answer /health")

    def sessions(self) -> list[dict[str, Any]]:
        return self.call("GET", "/sessions") or []

    def session(self, name: str) -> dict[str, Any]:
        return self.call("GET", f"/sessions/cao-{name}")

    def terminal_status(self, tid: str) -> str:
        return str(self.call("GET", f"/terminals/{tid}").get("status"))

    def create_session(self, name: str, cwd: str) -> dict[str, Any]:
        return self.call(
            "POST",
            "/sessions",
            {
                "agent_profile": "developer",
                "provider": "mock_cli",
                "session_name": name,
                "working_directory": cwd,
            },
        )

    def add_helper(self, session: str, caller_id: str | None, cwd: str) -> dict[str, Any]:
        params = {"agent_profile": "developer", "provider": "mock_cli", "working_directory": cwd}
        if caller_id:
            params["caller_id"] = caller_id
        return self.call("POST", f"/sessions/cao-{session}/terminals", params)

    def send_input(self, tid: str, message: str) -> Any:
        return self.call("POST", f"/terminals/{tid}/input", {"message": message})

    def wait_status(self, tid: str, want: set[str], seconds: float = 30.0) -> str:
        end = time.monotonic() + seconds
        last = ""
        while time.monotonic() < end:
            last = self.terminal_status(tid)
            if last in want:
                return last
            time.sleep(0.5)
        return last


def workdir() -> str:
    base = os.environ.get("SHELF_WORK_DIR") or str(
        Path(os.environ.get("SHELF_SCRATCH", str(Path.home() / "kc-shelf-e2e"))) / "work-c"
    )
    path = Path(base) / "cwd"
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def cmd_seed(cao: Cao, day: str, only: list[str] | None) -> None:
    cwd = workdir()
    existing = {s["name"] for s in cao.sessions()}
    for name, helpers in FLEET:
        if only and name not in only:
            continue
        if f"cao-{name}" in existing:
            print(f"seed: {name} exists, skipped")
            continue
        conductor = cao.create_session(name, cwd)
        cid = conductor["id"]
        ids = {"conductor": cid}
        for label, kind in helpers:
            helper = cao.add_helper(name, cid, cwd)
            ids[label] = helper["id"]
        print(f"seed: {name} conductor={cid} helpers={len(helpers)}")
        if day == "busy":
            if name == "payments-fix":
                cao.wait_status(cid, {"idle", "completed"})
                cao.send_input(cid, f"__mock_sleep_{SLEEP_SECONDS}")
                cao.wait_status(ids["coder-1"], {"idle", "completed"})
                cao.send_input(ids["coder-1"], f"__mock_sleep_{SLEEP_SECONDS}")
                cao.wait_status(ids["tester"], {"idle", "completed"})
                cao.send_input(ids["tester"], "__mock_approval__ push to origin?")
            elif name == "docs-refresh":
                for tid in ids.values():
                    cao.wait_status(tid, {"idle", "completed"})
                    cao.send_input(tid, f"__mock_sleep_{SLEEP_SECONDS}")


def cmd_closed_helper(cao: Cao, session: str) -> None:
    """A helper posts one row to the conductor's inbox, then is deleted (the row survives it)."""
    cwd = workdir()
    sess = cao.session(session)
    conductor = sess["terminals"][0]["id"]
    helper = cao.add_helper(session, conductor, cwd)
    hid = helper["id"]
    cao.wait_status(hid, {"idle", "completed"})
    cao.call(
        "POST",
        f"/terminals/{conductor}/inbox/messages",
        {"sender_id": hid, "message": "DONE: generic helper finished"},
    )
    cao.call("DELETE", f"/terminals/{hid}")
    print(f"closed-helper: {hid} sent one row to {conductor} and was deleted")


def cmd_orphan(cao: Cao, session: str) -> None:
    """A helper whose caller terminal is deleted afterwards (caller_id points at nothing)."""
    cwd = workdir()
    sess = cao.session(session)
    first = sess["terminals"][0]["id"]
    caller = cao.add_helper(session, first, cwd)  # the soon-to-be-deleted caller
    helper = cao.add_helper(session, caller["id"], cwd)
    cao.call("DELETE", f"/terminals/{caller['id']}")
    print(f"orphan: helper {helper['id']} has caller {caller['id']} which no longer exists")


def to_db_text(dt: datetime) -> str:
    # CAO stores UTCDateTime as naive UTC text 'YYYY-MM-DD HH:MM:SS.ffffff' (confirmed against the scratch DB).
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")


def cmd_backdate(session: str, hours: float, cao_home: str) -> None:
    """Test-only: rewrite terminals.last_active for one session in the SCRATCH CAO database."""
    home = Path(cao_home).resolve()
    if home.name != "cao-home" or ".kiro" in home.parts:
        sys.exit(f"seed_cao: refusing to touch a database outside a scratch cao-home: {home}")
    db = home / "db" / "cli-agent-orchestrator.db"
    if not db.is_file():
        sys.exit(f"seed_cao: no database at {db}")
    when = to_db_text(datetime.now(timezone.utc) - timedelta(hours=hours))
    con = sqlite3.connect(str(db), timeout=10)
    try:
        cur = con.execute(
            "UPDATE terminals SET last_active = ? WHERE tmux_session = ?", (when, f"cao-{session}")
        )
        con.commit()
        print(f"backdate: {session} {cur.rowcount} terminal row(s) set to {when}")
    finally:
        con.close()


def cmd_clear(cao: Cao) -> None:
    for row in cao.sessions():
        name = row["name"]
        cao.call("DELETE", f"/sessions/{name}")
        print(f"clear: deleted {name}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "command",
        choices=[
            "seed",
            "closed-helper",
            "orphan",
            "backdate",
            "names",
            "terminals",
            "wait",
            "clear",
        ],
    )
    ap.add_argument("--port", type=int, default=int(os.environ.get("SHELF_CAO_PORT", DEFAULT_PORT)))
    ap.add_argument("--day", choices=["quiet", "busy"], default="busy")
    ap.add_argument("--only", action="append", help="seed only this session name (repeatable)")
    ap.add_argument("--session")
    ap.add_argument("--hours", type=float)
    ap.add_argument("--cao-home", default=os.environ.get("SHELF_CAO_HOME", ""))
    args = ap.parse_args()

    cao = Cao(args.port)
    if args.command == "wait":
        cao.wait()
        print("wait: scratch CAO is healthy")
        return
    if args.command == "backdate":
        if not (args.session and args.hours is not None and args.cao_home):
            sys.exit(
                "seed_cao: backdate needs --session, --hours and --cao-home (or SHELF_CAO_HOME)"
            )
        cmd_backdate(args.session, args.hours, args.cao_home)
        return
    cao.wait()
    if args.command == "seed":
        cmd_seed(cao, args.day, args.only)
    elif args.command == "closed-helper":
        cmd_closed_helper(cao, args.session or "payments-fix")
    elif args.command == "orphan":
        cmd_orphan(cao, args.session or "search-tuning")
    elif args.command == "names":
        print("\n".join(sorted(s["name"] for s in cao.sessions())))
    elif args.command == "terminals":
        sess = cao.session(args.session or "payments-fix")
        for t in sess["terminals"]:
            print(t["id"], t["status"], t.get("tmux_window"))
    elif args.command == "clear":
        cmd_clear(cao)


if __name__ == "__main__":
    main()
