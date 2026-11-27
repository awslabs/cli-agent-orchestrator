#!/usr/bin/env python3
"""Record a MOCK run of the hierarchical-factory example as an AG-UI trace (#1166).

What is REAL: the cao-server (private, loopback, its own CAO_HOME_DIR and tmux server), CAO's
ambient stream and run plane, the approval card and interrupt, the assign and handoff
terminals, and the MuJoCo factory floor (``simulation.World`` from
``examples/robotics/hierarchical-factory``, simple biped proxies, no model download).

What is MOCK: every agent. The supervisor and the three team managers are ``mock_cli``
terminals that run no model. THIS SCRIPT plays their decisions: it makes the CAO calls a real
supervisor would make (assign, handoff, a permission prompt) and the factory commands a real
manager would make (form_team, start_line, start_shipping, set_pace) against the in-process
World. No factory MCP server is involved, so the factory commands are not AG-UI events. They
are written to a side command log, aligned by tool-call ordinal.

The recording goes through the unmodified CAO Dojo ``trace_scrub.normalise`` and is refused
by ``trace_leaks.check`` if any finding remains. Nothing in this folder re-implements either.

Run it with ``run.sh``, which sets the scratch directory, the loopback port and the timeout.
It never touches port 9889.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

WT = Path(
    os.environ.get("ROBOTICS_REPLAY_CHECKOUT") or Path(__file__).resolve().parents[4]
).resolve()
TOOLS = WT / "examples" / "ag-ui" / "cao-dojo" / "tools"
FACTORY = WT / "examples" / "robotics" / "hierarchical-factory"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(FACTORY))

# Simple bipeds: no 18 MB download, no network.
os.environ["FACTORY_ROBOT_MODEL_DIR"] = tempfile.mkdtemp(prefix="no-robot-model.")

import record_trace as rt  # noqa: E402
import replay_lifecycle as lifecycle  # noqa: E402  (this directory; stdlib only)
import requests  # noqa: E402
import trace_scrub as ts  # noqa: E402
from simulation import Scene, World  # noqa: E402
from trace_leaks import check as leak_check  # noqa: E402
from trace_leaks import format_findings  # noqa: E402

CAPTURE = "robotics-mock"
PROFILE = "mock-factory-agent"
FORMATIONS = {"west": "queue", "middle": "columns", "east": "diamond"}
ROLE = {"west": "manufacturing", "middle": "assembly", "east": "shipping"}


class Driver:
    def __init__(self, base: str, listener: rt._StreamListener, server: rt._Server, work: Path):
        self.base, self.listener, self.server, self.work = base, listener, server, work
        self.run_records: List[Dict[str, Any]] = []  # (stream index, record)
        self.world = World(
            Scene.model_validate(json.loads((FACTORY / "factory.json").read_text())),
            allow_motion=False,
        )
        self.cmd_log: List[Dict[str, Any]] = []
        self.tick_seconds = 0

    # --- stream helpers --------------------------------------------------------
    def calls(self, kind: str) -> int:
        return sum(1 for f in list(self.listener.frames) if f.get("type") == kind)

    def wait_calls(self, kind: str, n: int, what: str) -> None:
        rt._wait(what, 60.0, lambda: self.calls(kind) >= n, self.server.log)

    # --- CAO calls -------------------------------------------------------------
    def status(self, tid: str) -> str:
        return requests.get(f"{self.base}/terminals/{tid}", timeout=5).json().get("status")

    def wait_idle(self, tid: str) -> None:
        rt._wait(
            f"terminal {tid} idle",
            60.0,
            lambda: self.status(tid) in ("idle", "completed"),
            self.server.log,
        )

    def spawn(self, session: str, caller: str, kind: str, message: str) -> str:
        r = requests.post(
            f"{self.base}/sessions/{session}/terminals",
            params={
                "agent_profile": PROFILE,
                "provider": "mock_cli",
                "caller_id": caller,
                "defer_init": "true",
            },
            json={"initial_message": message, "initial_message_orchestration_type": kind},
            timeout=60,
        )
        if r.status_code >= 300:
            raise rt.RecorderError(f"{kind} failed {r.status_code} {r.text}")
        return r.json()["id"]

    def run_plane(self, rid: str, resume: Optional[list] = None) -> List[Dict[str, Any]]:
        at = len(self.listener.frames)
        body = rt.run_input(rid, resume)
        resp = requests.post(
            f"{self.base}/agui/v1/run",
            json=body,
            headers={"Accept": "text/event-stream"},
            timeout=(5, 60),
        )
        resp.raise_for_status()
        frames = rt.parse_sse_data(resp.text)
        for rec in rt._run_records(CAPTURE, rid, body, frames):
            self.run_records.append({"_at": at, "rec": rec})
        return frames

    # --- factory (the commands a real manager would send) ------------------------
    def factory(self, actor: str, op: str, *args: Any) -> Dict[str, Any]:
        result = getattr(self.world, op)(actor, *args)
        self.cmd_log.append(
            {
                "after_tool_calls": self.calls("TOOL_CALL_START"),
                "actor_zone": actor,
                "operation": op,
                "arguments": [a for a in args],
                "status": result.get("status"),
                "reason": result.get("reason"),
                "sim_seconds": self.tick_seconds,
            }
        )
        return result

    def run_sim(self, seconds: int) -> None:
        for _ in range(round(seconds / self.world.scene.step_seconds)):
            self.world.tick()
        self.tick_seconds += seconds


def scenario(d: Driver) -> None:
    base = d.base
    # 1. Supervisor terminal.
    made = requests.post(
        f"{base}/sessions",
        params={
            "agent_profile": PROFILE,
            "provider": "mock_cli",
            "session_name": "factory-floor",
            "working_directory": str(d.work),
        },
        timeout=60,
    )
    made.raise_for_status()
    sup = made.json()["id"]
    session = made.json()["session_name"]
    d.wait_idle(sup)

    # 2. The supervisor asks the operator to approve robot motion (the real example's
    #    allow_motion gate). A mock_cli permission prompt; CAO turns it into an
    #    approval_card + run-plane interrupt; the operator approves it.
    requests.post(
        f"{base}/terminals/{sup}/input",
        params={
            "message": rt.MOCK_PROMPT.replace(
                "Allow write to approved-by-dojo.txt?", "Allow factory robots to move?"
            )
        },
        timeout=30,
    ).raise_for_status()
    rt._wait(
        "supervisor waiting_user_answer",
        60.0,
        lambda: d.status(sup) == "waiting_user_answer",
        d.server.log,
    )
    rt._wait("approval card", 60.0, lambda: d.listener.pending_cards(sup), d.server.log)
    f1 = d.run_plane("robotics-run-1")
    interrupt = rt._only_interrupt(CAPTURE, f1)
    f2 = d.run_plane("robotics-run-2", [rt.resume_entry(interrupt["id"], True)])
    rt.check_run2(CAPTURE, rt._finished(f2))
    d.world.allow_motion = True  # what the operator's approval enables in the example
    d.wait_idle(sup)

    # 3. Layer 1 -> 2: the supervisor assigns the three team managers (async).
    n = d.calls("TOOL_CALL_START")
    managers: Dict[str, str] = {}
    for zone in ("east", "middle", "west"):  # downstream first, as the example does
        managers[zone] = d.spawn(session, sup, "assign", f"Team {zone}: form up for {ROLE[zone]}.")
        n += 1
        d.wait_calls("TOOL_CALL_START", n, f"assign {zone} announced")
        # Layer 2 -> 3: the manager forms its team.
        d.factory(zone, "form_team", "form-1", FORMATIONS[zone])
    d.run_sim(10)
    for zone in FORMATIONS:
        assert d.world.command(zone, "form-1")["status"] == "finished", zone

    # 4. The managers start the order (downstream first).
    d.factory("east", "start_shipping", "ship-1", 16, 2, ["spin", "turn", "run"])
    d.factory("middle", "start_line", "line-1", 16, 8)
    d.factory("west", "start_line", "line-1", 16, 2)

    # 5. Run the floor; when west blocks, the supervisor hands off a pace change.
    handed = False
    for _ in range(200):
        d.run_sim(1)
        if not handed and d.world.team_status("west")["status"] == "blocked":
            handed = True
            h = d.spawn(
                session, sup, "handoff", "Team west: you are blocked; slow to team middle's pace."
            )
            d.wait_calls("TOOL_CALL_START", n + 1, "handoff announced")
            d.factory("west", "set_pace", "pace-1", 8)
        if all(d.world.team_status(z)["status"] == "done" for z in FORMATIONS):
            break
    if not handed:
        raise rt.RecorderError("west never blocked; the key moment of the example did not occur")
    state = d.world.observe()
    d.outcome = {z: t["status"] for z, t in state["teams"].items()}
    # 6. Let every call close, then shut the session down.
    for tid in list(managers.values()):
        d.wait_idle(tid)
    d.wait_idle(h)
    # The listener appends on a daemon thread: wait (bounded) for every tool call's
    # TOOL_CALL_END before tearing the session down, rather than sleeping.
    _wait_closed(d, ("TOOL_CALL_START",), "closing tool-call records")
    requests.delete(f"{base}/sessions/{session}", timeout=60)
    rt._wait(
        "sessions empty",
        60.0,
        lambda: requests.get(f"{base}/sessions", timeout=5).json() == [],
        d.server.log,
    )
    # Then for every subagent run's SUBAGENT_FINISHED, which follows the teardown.
    _wait_closed(d, tuple(lifecycle.PAIRS), "closing subagent records")


def _wait_closed(d: Driver, kinds: tuple, what: str) -> None:
    try:
        lifecycle.wait_until_closed(lambda: list(d.listener.frames), 60.0, what=what, kinds=kinds)
    except TimeoutError as exc:
        raise rt.RecorderError(f"{exc} (server log: {d.server.log})") from exc


# The side log is not an AG-UI trace, so trace_leaks does not read it. Every string in it
# must be plain words, numbers and simple punctuation (refuses a path, an e-mail address, a
# URL), must hold no dotted name such as ``build-box-3.internal`` or ``my-laptop.local``
# (refuses a host name with a domain), and no run of 8+ hex digits (refuses a long id). A
# single-label host name such as ``buildhost`` reads like a word and is NOT refused. The test
# applies the same rule to the committed file on its own (test_robotics_replay_trace.py).
_LOG_VALUE = re.compile(r"^[A-Za-z0-9 _.,;()-]*$")
_DOTTED = re.compile(r"[A-Za-z0-9-]+\.[A-Za-z][A-Za-z0-9-]*")
_HEX8 = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{8,}(?![0-9A-Fa-f])")


def command_log_findings(log: Any) -> List[str]:
    """JSON paths of command-log strings that could carry a path, e-mail, dotted host or id."""
    out: List[str] = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]")
        elif isinstance(value, str) and (
            not _LOG_VALUE.match(value) or _DOTTED.search(value) or _HEX8.search(value)
        ):
            out.append(path)

    walk(log, "$")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--raw-out", type=Path, help="private scratch only; never delivered")
    ap.add_argument("--cmdlog", type=Path, required=True)
    a = ap.parse_args()
    rt.require_editable_checkout()
    refusal = rt.port_refusal(a.port)
    if refusal:
        raise SystemExit(refusal)
    provider_version = "mock_cli 0.1.0"
    meta = rt.build_meta("mock", "mock_cli", provider_version)
    meta["captures"] = [CAPTURE]
    meta["mode"] = "mock"
    server = rt._Server("mock", a.port)
    listener = None
    try:
        server.start()
        server.cao("install", str(Path(__file__).resolve().parent / "mock-factory-agent.md"))
        work = server.root / "work" / "factory"
        work.mkdir()
        listener = rt._StreamListener(f"{server.base}/agui/v1/stream")
        rt._wait("stream opened", 20.0, lambda: len(listener.frames) >= 2, server.log)
        d = Driver(server.base, listener, server, work)
        scenario(d)
        frames = list(listener.frames)
        # Never write a trace with a lifecycle left open (a late or lost record).
        left = lifecycle.unclosed(frames)
        if left:
            print("trace refused, lifecycles left open: " + "; ".join(left))
            return 2
        merged: List[Dict[str, Any]] = []
        runs = sorted(d.run_records, key=lambda r: r["_at"])
        ri = 0
        for i, f in enumerate(frames):
            while ri < len(runs) and runs[ri]["_at"] <= i:
                merged.append(runs[ri]["rec"])
                ri += 1
            merged.append({"capture": CAPTURE, "plane": "stream", "dir": "out", "frame": f})
        merged += [r["rec"] for r in runs[ri:]]
        if a.raw_out:
            a.raw_out.write_text("\n".join(json.dumps(r) for r in merged) + "\n")
        data = ts.normalise(meta, merged)
        findings = leak_check(data)
        if findings:
            print(format_findings(findings))
            return 2
        cmdlog = {
            "label": "MOCK agents; real MuJoCo World; simple biped proxies",
            "final_team_status": d.outcome,
            "commands": d.cmd_log,
        }
        log_leaks = command_log_findings(cmdlog)
        if log_leaks:
            print("command log refused, values at: " + ", ".join(log_leaks))
            return 2
        a.out.write_bytes(data)
        a.cmdlog.write_text(json.dumps(cmdlog, indent=1) + "\n")
        print(f"wrote {a.out} ({len(data)} bytes, {len(merged)} records); outcome {d.outcome}")
        server.ok = True
        return 0
    finally:
        if listener is not None:
            listener.close()
        server.stop()


if __name__ == "__main__":
    sys.exit(main())
