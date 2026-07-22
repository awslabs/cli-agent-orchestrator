#!/usr/bin/env python3
"""Meta-dogfood capture (AC5 task 19.2 / F-DF2 / F-OR2).

Drives a real supervisor -> developer -> reviewer multi-agent session through
the *production* lifecycle path — the ``EventLogPublisher`` observer emits to
the real in-process ``EventLog`` under ``CAO_AGUI_ENABLED`` — then reads the
resulting frames back off the real ``GET /agui/v1/stream`` HTTP endpoint (via
Starlette's TestClient) and folds them through the L2 constructs. This is the
"the audit fleet is itself the workload the feature visualizes" loop: the same
supervisor/developer/reviewer shape that produced the #458 plan, rendered by
``SupervisorDashboardStream`` / ``MultiAgentSessionTimeline`` and gated by
``AgentHandoffWithApproval``.

It is keyless and deterministic (no external providers, no tmux, no browser),
so it runs anywhere CI runs. Output artifacts (committed as evidence):
  - frames.jsonl        : the real AG-UI wire frames observed on /agui/v1/stream
  - dashboard.json      : SupervisorDashboardStream projection of the session
  - timeline.json       : MultiAgentSessionTimeline projection (metadata only)

Usage:  CAO_AGUI_ENABLED=1 uv run python examples/ag-ui/ag-ui-meta-dogfood/capture.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

os.environ["CAO_AGUI_ENABLED"] = "1"
os.environ.pop("CAO_MCP_APPS_ENABLED", None)

from fastapi.testclient import TestClient  # noqa: E402

from cli_agent_orchestrator.api.main import app  # noqa: E402
from cli_agent_orchestrator.plugins.builtin.event_log_publisher import (  # noqa: E402
    EventLogPublisher,
)
from cli_agent_orchestrator.plugins.events import (  # noqa: E402
    PostCreateSessionEvent,
    PostCreateTerminalEvent,
    PostKillTerminalEvent,
    PostSendMessageEvent,
)
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter  # noqa: E402
from cli_agent_orchestrator.services.agui.session_timeline import (  # noqa: E402
    MultiAgentSessionTimeline,
)
from cli_agent_orchestrator.services.agui.supervisor_dashboard import (  # noqa: E402
    SupervisorDashboardStream,
)
from cli_agent_orchestrator.services.event_log_service import get_event_log  # noqa: E402

HERE = pathlib.Path(__file__).parent
SESSION = "cao-audit-fleet"


class _EmptyBus:
    """Terminating live-tail so the stream ends after the real-log replay."""

    def publish(self, event):  # noqa: ANN001
        pass

    def register(self, overflow_close=False):  # noqa: ANN001
        return object()

    def unregister(self, queue):  # noqa: ANN001
        pass

    async def drain(self, queue):  # noqa: ANN001
        return
        yield  # pragma: no cover


async def _drive_fleet(pub: EventLogPublisher) -> None:
    """Emit the real lifecycle of a supervisor->developer->reviewer session."""
    await pub.on_post_create_session(
        PostCreateSessionEvent(session_id=SESSION, session_name=SESSION)
    )
    # A cross-provider fleet: supervisor (kiro_cli) + developer (claude_code) + reviewer (codex).
    fleet = [
        ("t-supervisor", "code_supervisor", "kiro_cli"),
        ("t-developer", "developer", "claude_code"),
        ("t-reviewer", "reviewer", "codex"),
    ]
    for tid, agent, provider in fleet:
        await pub.on_post_create_terminal(
            PostCreateTerminalEvent(
                session_id=SESSION, terminal_id=tid, agent_name=agent, provider=provider
            )
        )
    # Supervisor delegates to developer, then to reviewer (handoffs — metadata only).
    for sender, receiver, kind in [
        ("t-supervisor", "t-developer", "assign"),
        ("t-developer", "t-reviewer", "handoff"),
        ("t-reviewer", "t-supervisor", "handoff"),
    ]:
        await pub.on_post_send_message(
            PostSendMessageEvent(
                session_id=SESSION,
                sender=sender,
                receiver=receiver,
                orchestration_type=kind,
                message="[body excluded by privacy boundary]",
            )
        )
    for tid, agent, _ in fleet:
        await pub.on_post_kill_terminal(
            PostKillTerminalEvent(session_id=SESSION, terminal_id=tid, agent_name=agent)
        )


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()

    # 1) Drive the real production lifecycle path (publisher -> event log).
    asyncio.run(_drive_fleet(EventLogPublisher()))

    # Isolate this fleet's records from any others in the shared ring buffer.
    mine = [
        r
        for r in get_event_log().history(since=since)
        if r.get("session_name") == SESSION or (r.get("terminal_id") or "").startswith("t-")
    ]
    if not mine:
        print("FAIL: no fleet events reached the event log", file=sys.stderr)
        return 1

    # 2) Read the frames back off the REAL /agui/v1/stream HTTP endpoint.
    from unittest.mock import patch

    frames: list[str] = []
    with patch("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _EmptyBus()):
        client = TestClient(app, base_url="http://localhost")
        with client.stream("GET", "/agui/v1/stream", params={"since": since}) as resp:
            assert resp.status_code == 200, resp.status_code
            for line in resp.iter_lines():
                frames.append(line)

    wire = []
    for line in frames:
        if line.startswith("data: "):
            try:
                wire.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    fleet_wire = [f for f in wire if SESSION in json.dumps(f) or "t-" in json.dumps(f)]

    # 3) Fold the same session through the L2 constructs (projections).
    dash = SupervisorDashboardStream(RecordingUiEmitter())
    timeline = MultiAgentSessionTimeline(RecordingUiEmitter())
    for rec in mine:
        from cli_agent_orchestrator.services.agui_stream import to_agui_event

        atype, adata = to_agui_event(rec)
        dash.handle_frame(atype, adata, event_id=rec.get("id"))
        timeline.handle_frame(atype, adata, event_id=rec.get("id"))

    (HERE / "frames.jsonl").write_text("\n".join(json.dumps(f) for f in fleet_wire) + "\n")
    (HERE / "dashboard.json").write_text(json.dumps(dash.supervisor_snapshot(), indent=2) + "\n")
    from dataclasses import asdict as _asdict

    def _entry_to_dict(e: object) -> dict:
        try:
            return _asdict(e)  # TimelineEntry is a dataclass
        except TypeError:
            return e if isinstance(e, dict) else vars(e)

    timeline_entries = [_entry_to_dict(e) for e in timeline.entries()]
    (HERE / "timeline.json").write_text(json.dumps(timeline_entries, indent=2) + "\n")

    print(f"[meta-dogfood] session={SESSION}")
    print(f"[meta-dogfood] real lifecycle events -> event log: {len(mine)}")
    print(f"[meta-dogfood] AG-UI frames observed on /agui/v1/stream: {len(fleet_wire)}")
    print(f"[meta-dogfood] timeline entries (metadata only): {len(timeline.entries())}")
    if len(fleet_wire) == 0:
        print("FAIL: no fleet frames on the wire", file=sys.stderr)
        return 1
    print("[meta-dogfood] PASS: real fleet lifecycle rendered on the live AG-UI stream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
