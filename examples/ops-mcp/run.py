#!/usr/bin/env python3
"""Drive a CAO session from outside CAO, over MCP.

Spawns `cao-ops-mcp-server` as a stdio MCP server and runs one full session
lifecycle through its typed tools: discover profiles, launch a session, wait for
the dispatched turn to finish, send a follow-up, read the output, and shut down.

Nothing here builds a shell command, and nothing reads the CAO terminal-id
environment variable -- this process is not a CAO terminal and never becomes one.

Usage:
    python3 examples/ops-mcp/run.py --task "Say hello and stop."
    python3 examples/ops-mcp/run.py --profile ops_mcp_worker --provider claude_code \
        --working-directory /tmp/ops-mcp-demo --task "List the files here."

Requires `cao-server` running (default http://127.0.0.1:9889).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any, Dict, Optional

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import CallToolResult, Implementation, TextContent

# The agent is between turns and can accept input.
READY_STATUSES = frozenset({"idle", "completed"})
# The agent is working, or blocked on a prompt. Either way a turn is in flight.
ACTIVE_STATUSES = frozenset({"processing", "waiting_user_answer"})
# The agent will not become ready on its own.
DEAD_STATUSES = frozenset({"error"})

LIFECYCLE_TOOLS = (
    "list_profiles",
    "launch_session",
    "get_terminal_status",
    "send_session_message",
    "read_session_output",
    "get_session_info",
    "shutdown_session",
)


def parse_tool_result(result: CallToolResult) -> Any:
    """Extract the payload from an MCP tool result.

    Prefers ``structuredContent`` when the tool declares an output schema, and
    otherwise falls back to concatenated text blocks, parsed as JSON when
    possible. Errors are returned as a dict so callers handle one shape.
    """
    if result.structuredContent is not None:
        return result.structuredContent

    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    combined = "\n".join(texts)

    if result.isError:
        return {"success": False, "message": combined or "tool reported an error"}

    try:
        return json.loads(combined)
    except (json.JSONDecodeError, ValueError):
        return combined


async def call_tool(session: Any, name: str, **arguments: Any) -> Any:
    """Call one typed tool and return its parsed payload."""
    result = await session.call_tool(name=name, arguments=arguments)
    return parse_tool_result(result)


def _failed(payload: Any) -> bool:
    """Whether a tool payload reports failure.

    Tools that succeed either omit ``success`` or set it True, so only an
    explicit False counts as a failure.
    """
    return isinstance(payload, dict) and payload.get("success") is False


async def read_output(session: Any, terminal_id: str, *, mode: str = "full") -> str:
    """Read a terminal's output, raising when the read itself failed."""
    payload = await call_tool(session, "read_session_output", terminal_id=terminal_id, mode=mode)
    if _failed(payload):
        raise RuntimeError(f"read output failed: {payload.get('message')}")
    if isinstance(payload, dict):
        return str(payload.get("output", ""))
    return str(payload)


async def output_size(session: Any, terminal_id: str) -> int:
    """Total characters the terminal has produced so far.

    Used as a turn-boundary marker: growth past a recorded size is evidence that
    the agent produced something after the message was dispatched.
    """
    payload = await call_tool(session, "read_session_output", terminal_id=terminal_id, mode="full")
    if _failed(payload):
        raise RuntimeError(f"read output failed: {payload.get('message')}")
    if isinstance(payload, dict):
        total = payload.get("total_chars")
        if isinstance(total, int):
            return total
        return len(str(payload.get("output", "")))
    return len(str(payload))


async def wait_for_turn(
    session: Any,
    terminal_id: str,
    *,
    baseline_status: str,
    baseline_output_size: Optional[int],
    timeout: float = 120.0,
    interval: float = 2.0,
    sleep: Any = asyncio.sleep,
    now: Any = time.monotonic,
) -> str:
    """Wait for the turn dispatched *after* the recorded baseline to finish.

    A status enum alone cannot prove a turn ran. ``idle`` may predate delivery of
    a queued message, and ``completed`` may belong to the *previous* turn -- the
    messaging tool only promises the message was queued, so a cached status can
    still hold its pre-dispatch value. Accepting the first ready enum therefore
    reads the wrong turn's output.

    So a ready status is accepted only alongside evidence that this turn ran:

    * a status in ``ACTIVE_STATUSES`` observed while waiting, or
    * output produced past ``baseline_output_size``, or
    * ``completed`` when the baseline was not already a ready state -- reaching
      completed requires dispatched input, so it cannot predate the first turn.

    Returns the ready status. Raises RuntimeError on a dead status and
    TimeoutError when no dispatched turn finishes in time.
    """
    deadline = now() + timeout
    saw_activity = False
    status = "unknown"
    baseline_was_ready = baseline_status in READY_STATUSES

    while now() < deadline:
        payload = await call_tool(session, "get_terminal_status", terminal_id=terminal_id)
        if _failed(payload):
            raise RuntimeError(f"status check failed: {payload.get('message')}")

        status = payload.get("status", "unknown") if isinstance(payload, dict) else "unknown"

        if status in DEAD_STATUSES:
            raise RuntimeError(f"terminal {terminal_id} reached status {status!r}")

        if status in ACTIVE_STATUSES:
            saw_activity = True
        elif status in READY_STATUSES:
            if saw_activity:
                return status
            if status == "completed" and not baseline_was_ready:
                return status
            if baseline_output_size is not None:
                if await output_size(session, terminal_id) > baseline_output_size:
                    return status

        await sleep(interval)

    raise TimeoutError(
        f"terminal {terminal_id} was {status!r} after {timeout:.0f}s "
        "with no evidence the dispatched turn ran"
    )


async def session_is_absent(session: Any, session_name: str) -> bool:
    """Whether CAO no longer has this session.

    An already-absent session counts as verified cleanup: the goal is that no
    session is left running, not that this process performed the removal. The
    ops-MCP server currently identifies this case with its canonical not-found
    message; every other failed lookup leaves cleanup unverified.
    """
    payload = await call_tool(session, "get_session_info", session_name=session_name)
    if _failed(payload):
        expected = (
            f"Get session info for '{session_name}' failed: Session '{session_name}' not found"
        )
        if payload.get("message") == expected:
            return True
        raise RuntimeError(f"cleanup verification lookup failed: {payload.get('message')}")
    return not isinstance(payload, dict)


async def run_lifecycle(
    session: Any,
    *,
    profile: str,
    task: str,
    provider: Optional[str] = None,
    session_name: Optional[str] = None,
    working_directory: Optional[str] = None,
    follow_up: Optional[str] = None,
    timeout: float = 120.0,
    sleep: Any = asyncio.sleep,
    now: Any = time.monotonic,
) -> Dict[str, Any]:
    """Run one CAO session lifecycle over an initialized MCP session.

    Transport-agnostic on purpose: ``session`` only needs ``call_tool``, so the
    ordering, turn-evidence and cleanup guarantees below are testable without a
    live server.

    The launched session is always shut down, and cleanup is *verified* -- an
    unverified cleanup fails the run rather than reporting success.
    """
    report: Dict[str, Any] = {"steps": [], "session_name": None, "terminal_id": None}

    def record(step: str, detail: Any) -> None:
        report["steps"].append({"step": step, "detail": detail})

    profiles = await call_tool(session, "list_profiles")
    if _failed(profiles):
        raise RuntimeError(f"could not list profiles: {profiles.get('message')}")
    names = (
        [p.get("name") for p in profiles.get("profiles", [])] if isinstance(profiles, dict) else []
    )
    record("list_profiles", {"count": len(names)})
    if profile not in names:
        raise RuntimeError(
            f"profile {profile!r} is not installed. Install it with: "
            f"cao install examples/ops-mcp/{profile}.md"
        )

    launch = await call_tool(
        session,
        "launch_session",
        agent_profile=profile,
        provider=provider,
        session_name=session_name,
        working_directory=working_directory,
        initial_message=task,
    )
    if _failed(launch):
        raise RuntimeError(f"launch failed: {launch.get('message')}")

    report["session_name"] = launch.get("session_name")
    report["terminal_id"] = launch.get("terminal_id")
    record(
        "launch_session",
        {"session_name": report["session_name"], "terminal_id": report["terminal_id"]},
    )

    if not report["session_name"] or not report["terminal_id"]:
        raise RuntimeError(f"launch returned no session identity: {launch}")

    terminal_id = report["terminal_id"]

    try:
        # No pre-dispatch baseline exists here: the task was delivered inside
        # launch_session. Evidence must come from activity or a completed turn.
        status = await wait_for_turn(
            session,
            terminal_id,
            baseline_status="unknown",
            baseline_output_size=None,
            timeout=timeout,
            sleep=sleep,
            now=now,
        )
        record("wait_for_turn", {"status": status, "evidence": "launch turn"})

        if follow_up:
            # Retain the pre-dispatch size for reporting. Follow-up completion
            # requires observed activity because rolling output can be reset.
            previous_output_size = await output_size(session, terminal_id)

            sent = await call_tool(
                session,
                "send_session_message",
                terminal_id=terminal_id,
                message=follow_up,
            )
            if _failed(sent):
                raise RuntimeError(f"follow-up failed: {sent.get('message')}")
            record(
                "send_session_message",
                {
                    "queued": True,
                    "previous_output_chars": previous_output_size,
                    "baseline_chars": previous_output_size,
                },
            )

            status = await wait_for_turn(
                session,
                terminal_id,
                baseline_status=status,
                baseline_output_size=None,
                timeout=timeout,
                interval=0.5,
                sleep=sleep,
                now=now,
            )
            record("wait_for_turn", {"status": status, "evidence": "follow-up turn"})

        text = await read_output(session, terminal_id, mode="last")
        report["output"] = text
        record("read_session_output", {"chars": len(text)})
    finally:
        # Always clean up, even when a step above raised, and confirm the
        # session is actually gone rather than trusting the return value.
        shutdown = await call_tool(session, "shutdown_session", session_name=report["session_name"])
        report["shutdown_reported"] = not _failed(shutdown)
        report["cleanup_verified"] = await session_is_absent(session, report["session_name"])
        record(
            "shutdown_session",
            {
                "reported": report["shutdown_reported"],
                "verified_gone": report["cleanup_verified"],
            },
        )
        if not report["cleanup_verified"]:
            print(
                f"warning: session {report['session_name']} may still be running",
                file=sys.stderr,
            )

    # Reached only when the body succeeded; an earlier exception propagates and
    # is not masked by this check.
    if not report["cleanup_verified"]:
        raise RuntimeError(
            f"cleanup not verified: session {report['session_name']} is still present"
        )

    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Drive a CAO session over cao-ops-mcp.")
    parser.add_argument(
        "--profile", default="ops_mcp_worker", help="installed agent profile to launch"
    )
    parser.add_argument(
        "--provider", default=None, help="provider override (default: profile or CAO default)"
    )
    parser.add_argument(
        "--session-name", default=None, help="session name (default: CAO generates one)"
    )
    parser.add_argument(
        "--working-directory", default=None, help="absolute working directory for the session"
    )
    parser.add_argument("--task", required=True, help="initial task delivered on launch")
    parser.add_argument(
        "--follow-up", default=None, help="optional second message sent after the first turn"
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0, help="seconds to wait for each turn"
    )
    parser.add_argument(
        "--server-command", default="cao-ops-mcp-server", help="stdio MCP server to spawn"
    )
    return parser


async def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # This process is an external operator, not a CAO terminal. Saying so keeps
    # an inherited value from making the example look like it needs one.
    if "CAO_TERMINAL_ID" in os.environ:
        print(
            "note: CAO_TERMINAL_ID is set in this shell; this example ignores it", file=sys.stderr
        )

    params = StdioServerParameters(command=args.server_command, args=[])

    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            client_info=Implementation(name="cao-ops-mcp-example", version="1.0.0"),
        ) as session:
            init = await session.initialize()
            print(f"connected to {init.serverInfo.name} (protocol {init.protocolVersion})")

            available = {tool.name for tool in (await session.list_tools()).tools}
            missing = [name for name in LIFECYCLE_TOOLS if name not in available]
            if missing:
                print(f"server is missing expected tools: {', '.join(missing)}", file=sys.stderr)
                return 2

            try:
                report = await run_lifecycle(
                    session,
                    profile=args.profile,
                    provider=args.provider,
                    session_name=args.session_name,
                    working_directory=args.working_directory,
                    task=args.task,
                    follow_up=args.follow_up,
                    timeout=args.timeout,
                )
            except (RuntimeError, TimeoutError) as exc:
                print(f"lifecycle failed: {exc}", file=sys.stderr)
                return 1

    for entry in report["steps"]:
        print(f"  {entry['step']}: {json.dumps(entry['detail'])}")
    print(f"\nsession {report['session_name']} output:\n{report.get('output', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
