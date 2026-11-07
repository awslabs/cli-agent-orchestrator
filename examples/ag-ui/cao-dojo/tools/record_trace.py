#!/usr/bin/env python3
"""Record, check and gate the CAO Dojo AG-UI trace (#1116).

Three captures, each produced by construction against a fresh, isolated
``cao-server``:

* ``none``: one run against an empty fleet; it ends ``RUN_FINISHED`` success.
* ``approve``: an agent raises a permission prompt; run 1 returns the
  interrupt, run 2 resumes it with ``approved: true``.
* ``reject``: the same against a fresh session, with ``approved: false``.

Modes:

``--live``     Claude Code (``dojo-approver.md``), with a logged-in ``claude`` on
               PATH. Local only. This is the only mode that may write the
               published trace (``--out``).
``--mock``     ``mock_cli`` (``tools/dojo-trace-mock.md``) answering the scripted
               ``__mock_approval__`` prompt. Keyless; this is CI's required
               approve/reject/none e2e and tier 2 of the drift check.
``--offline``  No server, no tmux: the same three captures produced in-process
               by CAO's own producers (``run_plane_stream``,
               ``AgentHandoffWithApproval``, ``AmbientRun``). Tier 1 uses it
               when no published trace exists yet.

Actions (combine freely):

``--check``        compare this recording's shape with the committed shape file
                   (tier 2); exit 1 with ``DRIFT ...`` on any difference.
``--write-shape``  write this recording's shape file (``cao-dojo-trace.shape.json``).
``--out PATH``     ``--live`` only: write the published trace, stamping
                   ``_meta.shape_sha256``.
``--emit PATH``    write the normalised trace to a scratch path (any mode).
``--check-debt``   no recording: compare the published trace with the shape file.
                   ``--warn`` turns a failure into a ``::warning::`` (PRs); without
                   it the check blocks (the release path).

Every recording is normalised (``trace_scrub``), then held to the leak rule
(``trace_leaks``), the per-capture outcomes and the size budget before anything
is written. The default port is 19899; 9889 and any busy port are refused.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

TOOLS_DIR = Path(__file__).resolve().parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from trace_leaks import check as leak_check  # noqa: E402
from trace_leaks import format_findings  # noqa: E402
from trace_scrub import (  # noqa: E402
    CAO_GENERATIVE_UI,
    CAPTURES,
    RUN_IDS,
    SCHEMA,
    THREAD_ID,
    dumps,
    first_shape_difference,
    normalise,
    parse,
    run_id,
    sha256,
    shape_bytes,
)

__all__ = [
    "CAPTURES",
    "RUN_IDS",
    "SCHEMA",
    "THREAD_ID",
    "RecorderError",
    "assert_outcomes",
    "capture_offline",
    "check_budget",
    "check_debt",
    "check_run2",
    "main",
]

DOJO_DIR = TOOLS_DIR.parent
REPO_ROOT = DOJO_DIR.parent.parent.parent
TRACE_PATH = DOJO_DIR / "trace" / "cao-dojo-trace.ndjson"
SHAPE_PATH = DOJO_DIR / "trace" / "cao-dojo-trace.shape.json"
LIVE_PROFILE = DOJO_DIR / "dojo-approver.md"
MOCK_PROFILE = TOOLS_DIR / "dojo-trace-mock.md"
MOCK_BIN_DIR = REPO_ROOT / "test" / "providers" / "fixtures" / "bin"

DEFAULT_PORT = 19899
FORBIDDEN_PORT = 9889
WAIT_SECONDS = {"mock": 60.0, "live": 180.0}
SERVER_STOP_SECONDS = 15.0
# tmux reads these before TMUX_TMPDIR, so a child that inherits them reaches the
# caller's own tmux server. Never pass them to a tmux this recorder runs.
TMUX_CLIENT_VARS = ("TMUX", "TMUX_PANE")
RECORD_COMMAND = "make record-agui-trace"

# Size budget: at most 8 KiB and 16 lines per capture, 24 KiB per file.
MAX_CAPTURE_BYTES = 8 * 1024
MAX_FILE_BYTES = 24 * 1024
MAX_CAPTURE_LINES = 16

MOCK_PROMPT = "__mock_approval__ Allow write to approved-by-dojo.txt?"
LIVE_TASK = "Do your task now."
APPROVED_FILE = "approved-by-dojo.txt"

# Where the race lives, named by function rather than line so an unrelated edit to
# either file cannot make the message wrong (a test resolves each name).
RUN2_RACE_FUNCTIONS = (
    "cli_agent_orchestrator.services.agui.approval_bridge.ApprovalBridge._on_leave_waiting",
    "cli_agent_orchestrator.services.agui.run_plane.run_plane_stream",
)
RUN2_RACE_MESSAGE = (
    "run 2 RUN_FINISHED carries result.skippedResume: the approval bridge expired the "
    f"interrupt before the answer was delivered ({RUN2_RACE_FUNCTIONS[0]} -> "
    f"{RUN2_RACE_FUNCTIONS[1]}). This is the bridge-expiry race, not trace drift: a bridge "
    "fix, never an allowed difference."
)


class RecorderError(RuntimeError):
    """A recording failed a check. The message names the step and the bound."""


# --- Checks on a normalised trace ---------------------------------------------


def check_run2(capture: str, frame: Dict[str, Any]) -> None:
    """Run 2 must end clean: ``success`` and no ``result`` key at all."""
    outcome = (frame.get("outcome") or {}).get("type")
    if "result" in frame:
        result = frame.get("result") or {}
        if isinstance(result, dict) and "skippedResume" in result:
            raise RecorderError(f"{capture} {RUN2_RACE_MESSAGE}")
        raise RecorderError(f"{capture} run 2 RUN_FINISHED carries an unexpected result: {result}")
    if frame.get("type") != "RUN_FINISHED" or outcome != "success":
        raise RecorderError(
            f"{capture} run 2 expected RUN_FINISHED success, got {frame.get('type')} {outcome}"
        )


def _runs(records: Sequence[Dict[str, Any]], capture: str) -> Dict[str, List[Dict[str, Any]]]:
    runs: Dict[str, List[Dict[str, Any]]] = {}
    for record in records:
        if record.get("capture") == capture and record.get("plane") == "run":
            runs.setdefault(record["run"], []).append(record)
    return runs


def _frame_types(run: Sequence[Dict[str, Any]]) -> List[str]:
    return [r["frame"]["type"] for r in run if r.get("dir") == "out"]


def assert_outcomes(records: Sequence[Dict[str, Any]]) -> None:
    """Every capture produced exactly what it is constructed to produce."""
    seen = []
    for record in records:
        if record.get("capture") not in seen:
            seen.append(record.get("capture"))
    if seen != list(CAPTURES):
        raise RecorderError(f"captures must be {list(CAPTURES)} in order, got {seen}")

    for capture in CAPTURES:
        runs = _runs(records, capture)
        stream = [r for r in records if r.get("capture") == capture and r.get("plane") == "stream"]
        expected_runs = [run_id(capture, 1)] if capture == "none" else _run_ids_for(capture)
        if list(runs) != expected_runs:
            raise RecorderError(f"{capture}: runs {list(runs)} != {expected_runs}")
        for rid, run in runs.items():
            if run[0].get("dir") != "request":
                raise RecorderError(f"{capture} {rid}: the request must come first")
            types = _frame_types(run)
            if types != ["RUN_STARTED", "STATE_SNAPSHOT", "RUN_FINISHED"]:
                raise RecorderError(f"{capture} {rid}: frame types {types}")
            started = run[1]["frame"]
            if started.get("protocolVersion") != "1.0":
                raise RecorderError(
                    f"{capture} {rid}: protocolVersion {started.get('protocolVersion')!r}"
                )
            if started.get("threadId") != THREAD_ID or started.get("runId") != rid:
                raise RecorderError(f"{capture} {rid}: RUN_STARTED ids do not match the request")

        if capture == "none":
            if stream:
                raise RecorderError("none: no stream record expected")
            run = runs[run_id("none", 1)]
            if run[0]["request"] != {"resume": []}:
                raise RecorderError("none: run 1 must send no resume")
            snapshot = run[2]["frame"]["snapshot"]
            if snapshot.get("terminals") != [] or snapshot.get("sessions") != []:
                raise RecorderError("none: the fleet must be empty")
            finished = run[3]["frame"]
            if (finished.get("outcome") or {}).get("type") != "success" or "result" in finished:
                raise RecorderError(
                    f"none: expected a clean success, got {finished.get('outcome')}"
                )
            continue

        approved = capture == "approve"
        if len(stream) != 1:
            raise RecorderError(
                f"{capture}: expected 1 pending approval_card for the captured terminal, "
                f"got {len(stream)}"
            )
        card = stream[0]["frame"]
        value = card.get("value") or {}
        if (
            card.get("type") != "CUSTOM"
            or card.get("name") != CAO_GENERATIVE_UI
            or value.get("component") != "approval_card"
        ):
            raise RecorderError(f"{capture}: the stream record is not an approval_card")
        if records.index(stream[0]) > records.index(runs[run_id(capture, 1)][0]):
            raise RecorderError(f"{capture}: the card must arrive before run 1")

        run1 = runs[run_id(capture, 1)]
        run2 = runs[run_id(capture, 2)]
        if run1[0]["request"] != {"resume": []}:
            raise RecorderError(f"{capture}: run 1 must send no resume")
        finished1 = run1[3]["frame"]
        outcome1 = finished1.get("outcome") or {}
        interrupts = outcome1.get("interrupts") or []
        if outcome1.get("type") != "interrupt" or len(interrupts) != 1:
            raise RecorderError(f"{capture}: run 1 expected one interrupt, got {outcome1}")
        interrupt = interrupts[0]
        props = value.get("props") or {}
        if props.get("interrupt_id") != interrupt.get("id"):
            raise RecorderError(f"{capture}: card interrupt_id != run 1 interrupt id")
        if (interrupt.get("metadata") or {}).get("terminalId") != value.get("terminal_id"):
            raise RecorderError(f"{capture}: interrupt terminalId != card terminal_id")
        snapshot = run1[2]["frame"]["snapshot"]
        if len(snapshot.get("terminals") or []) != 1:
            raise RecorderError(f"{capture}: the snapshot must hold exactly one terminal")

        resume = run2[0]["request"].get("resume") or []
        expected = [
            {
                "interruptId": interrupt.get("id"),
                "status": "resolved",
                "payload": {"approved": approved},
            }
        ]
        if resume != expected:
            raise RecorderError(f"{capture}: run 2 resume {resume} != {expected}")
        check_run2(capture, run2[3]["frame"])


def _run_ids_for(capture: str) -> List[str]:
    return [run_id(capture, 1), run_id(capture, 2)]


def check_budget(data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise RecorderError(f"trace is {len(data)} bytes, over the {MAX_FILE_BYTES}-byte budget")
    _, records = parse(data)
    for capture in CAPTURES:
        lines = [dumps(r) for r in records if r.get("capture") == capture]
        size = sum(len(line.encode("utf-8")) + 1 for line in lines)
        if len(lines) > MAX_CAPTURE_LINES:
            raise RecorderError(f"{capture}: {len(lines)} lines, over {MAX_CAPTURE_LINES}")
        if size > MAX_CAPTURE_BYTES:
            raise RecorderError(f"{capture}: {size} bytes, over {MAX_CAPTURE_BYTES}")


def validate(data: bytes) -> None:
    """Leak rule, outcomes and budget. Raises ``RecorderError`` on the first failure."""
    findings = leak_check(data)
    if findings:
        raise RecorderError(format_findings(findings))
    _, records = parse(data)
    assert_outcomes(records)
    check_budget(data)


# --- Re-record debt (shape_sha256; warns on PRs, blocks the release) -----------


def check_debt(trace_path: Path = TRACE_PATH, shape_path: Path = SHAPE_PATH) -> Tuple[bool, str]:
    """Is the published trace current with the committed shape file?

    Needs no server and no credentials: it reads the two committed files.
    """
    if not shape_path.is_file():
        return (
            False,
            f"shape file missing: {_rel(shape_path)} (regenerate it with --mock --write-shape)",
        )
    shape_data = shape_path.read_bytes()
    if not trace_path.is_file():
        return False, (
            f"trace missing or stale: {_rel(trace_path)} is not recorded yet. "
            f"Record it with `{RECORD_COMMAND}` (needs a logged-in Claude Code)."
        )
    trace_data = trace_path.read_bytes()
    try:
        meta, _ = parse(trace_data)
    except (ValueError, json.JSONDecodeError) as e:
        return False, f"trace missing or stale: {_rel(trace_path)} does not parse ({e})"
    recorded = meta.get("shape_sha256")
    current = sha256(shape_data)
    if recorded != current:
        return False, (
            f"trace missing or stale: {_rel(trace_path)} was recorded against shape "
            f"{recorded}, the committed shape is {current}. Re-record with `{RECORD_COMMAND}`."
        )
    if shape_bytes(trace_data) != shape_data:
        return False, (
            f"trace missing or stale: {_rel(trace_path)} no longer projects onto "
            f"{_rel(shape_path)}. Re-record with `{RECORD_COMMAND}`."
        )
    return True, f"trace current: shape_sha256 {current}"


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# --- _meta ---------------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=30
    ).stdout.strip()


def build_meta(mode: str, provider: str, provider_version: str) -> Dict[str, Any]:
    return {
        "schema": SCHEMA,
        "cao_version": importlib.metadata.version("cli-agent-orchestrator"),
        "cao_build": _git("rev-parse", "HEAD"),
        "cao_build_dirty": bool(_git("status", "--porcelain")),
        "ag_ui_protocol": importlib.metadata.version("ag-ui-protocol"),
        "protocol_version": "1.0",
        "mode": mode,
        "provider": provider,
        "provider_version": provider_version,
        "captures": list(CAPTURES),
    }


def require_editable_checkout() -> None:
    import cli_agent_orchestrator

    package = Path(cli_agent_orchestrator.__file__).resolve()
    if REPO_ROOT.resolve() not in package.parents:
        raise RecorderError(
            f"cli_agent_orchestrator is imported from {package}, not from this checkout "
            f"({REPO_ROOT}); `_meta.cao_build` would not name the code that ran. "
            "Run `uv sync` in this checkout."
        )


# --- SSE helpers ---------------------------------------------------------------


def parse_sse_data(body: str) -> List[Dict[str, Any]]:
    """Return the JSON ``data:`` payloads of an SSE body, in order."""
    frames = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = [line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")]
        if data:
            frames.append(json.loads("\n".join(data)))
    return frames


def run_input(rid: str, resume: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    body: Dict[str, Any] = {
        "threadId": THREAD_ID,
        "runId": rid,
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume:
        body["resume"] = resume
    return body


def resume_entry(interrupt_id: str, approved: bool) -> Dict[str, Any]:
    return {"interruptId": interrupt_id, "status": "resolved", "payload": {"approved": approved}}


def _run_records(
    capture: str, rid: str, body: Dict[str, Any], frames: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = [
        {"capture": capture, "plane": "run", "run": rid, "dir": "request", "request": body}
    ]
    out.extend(
        {"capture": capture, "plane": "run", "run": rid, "dir": "out", "frame": f} for f in frames
    )
    return out


def _finished(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    for frame in reversed(frames):
        if frame.get("type") in ("RUN_FINISHED", "RUN_ERROR"):
            return frame
    raise RecorderError(
        f"run ended without RUN_FINISHED or RUN_ERROR: {[f.get('type') for f in frames]}"
    )


def _only_interrupt(capture: str, frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    finished = _finished(frames)
    interrupts = (finished.get("outcome") or {}).get("interrupts") or []
    if finished.get("type") != "RUN_FINISHED" or len(interrupts) != 1:
        raise RecorderError(f"{capture}: run 1 expected one interrupt, got {finished}")
    return interrupts[0]


# --- Offline capture (in-process, CAO's own producers) --------------------------


class _NoopDelivery:
    """Accept the decision without a terminal. The offline capture has no tmux."""

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        return None

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        return True


def capture_offline() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The three captures, produced in-process with no server and no tmux."""
    return build_meta("offline", "mock_cli", "offline"), asyncio.run(_capture_offline())


async def _capture_offline() -> List[Dict[str, Any]]:
    from cli_agent_orchestrator.security.auth import SCOPE_ADMIN, SCOPE_READ, SCOPE_WRITE
    from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
    from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval
    from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream
    from cli_agent_orchestrator.services.ui_state_service import build_dashboard_snapshot

    scopes = [SCOPE_READ, SCOPE_WRITE, SCOPE_ADMIN]
    emitter = RecordingUiEmitter()
    construct = AgentHandoffWithApproval(emitter=emitter, answer_delivery=_NoopDelivery())
    fleet: Dict[str, List[Dict[str, Any]]] = {"sessions": [], "terminals": []}

    def snapshot() -> Dict[str, Any]:
        return build_dashboard_snapshot(fleet["sessions"], fleet["terminals"], scopes)

    async def run(rid: str, resume: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
        body = run_input(rid, resume)
        chunks = [
            chunk
            async for chunk in run_plane_stream(
                input_data=body, approval_construct=construct, snapshot_fn=snapshot
            )
        ]
        return parse_sse_data("".join(chunks))

    records: List[Dict[str, Any]] = []
    rid = run_id("none", 1)
    records += _run_records("none", rid, run_input(rid), await run(rid))

    for capture in ("approve", "reject"):
        terminal_id = uuid.uuid4().hex[:8]
        session = f"cao-dojo-trace-{capture}"
        fleet["sessions"] = [{"id": session, "name": session, "status": "detached"}]
        fleet["terminals"] = [
            {
                "id": terminal_id,
                "tmux_session": session,
                "tmux_window": f"dojo-trace-mock-{uuid.uuid4().hex[:4]}",
                "provider": "mock_cli",
                "agent_profile": "dojo-trace-mock",
                "last_active": datetime.now(timezone.utc),
            }
        ]
        before = len(emitter.intents)
        construct.on_provider_waiting(
            terminal_id=terminal_id,
            provider="mock_cli",
            raw_prompt=f"APPROVAL_REQUIRED: {MOCK_PROMPT}",
            session_name=None,
        )
        intent = emitter.intents[before]
        # The record agui.base.InProcessUiEmitter appends to the event log.
        event = {
            "id": str(uuid.uuid4()),
            "kind": "other",
            "terminal_id": intent["terminal_id"],
            "session_name": intent["session_name"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "detail": {"ui": {"component": intent["component"], "props": intent["props"]}},
        }
        card = _ambient_card(event)
        records.append({"capture": capture, "plane": "stream", "dir": "out", "frame": card})

        rid1 = run_id(capture, 1)
        frames1 = await run(rid1)
        records += _run_records(capture, rid1, run_input(rid1), frames1)
        interrupt = _only_interrupt(capture, frames1)

        rid2 = run_id(capture, 2)
        resume = [resume_entry(interrupt["id"], capture == "approve")]
        records += _run_records(capture, rid2, run_input(rid2, resume), await run(rid2, resume))
        fleet["sessions"], fleet["terminals"] = [], []
    return records


def _ambient_card(event: Dict[str, Any]) -> Dict[str, Any]:
    """The approval card the ambient stream sends for ``event``, as the server frames it.

    ``GET /agui/v1/stream`` maps each event-log record through ``AmbientRun.feed``
    and encodes the result with the SDK's ``EventEncoder``; this does the same, so
    the offline card and a ``--mock`` card have the same keys.
    """
    from ag_ui.encoder import EventEncoder

    from cli_agent_orchestrator.services.agui.ambient_run import AmbientRun

    encoder = EventEncoder(accept="text/event-stream")
    run = AmbientRun(THREAD_ID, "offline-ambient", incarnation_of=lambda _session: None)
    frames = parse_sse_data("".join(encoder.encode(e) for _, e in run.feed(event)))
    cards = [f for f in frames if f.get("type") == "CUSTOM" and f.get("name") == CAO_GENERATIVE_UI]
    if len(cards) != 1:
        raise RecorderError(f"expected one approval card from AmbientRun.feed, got {len(cards)}")
    return cards[0]


# --- Server capture (--mock / --live) -----------------------------------------


def port_refusal(port: int) -> Optional[str]:
    """Why ``port`` cannot be used, or ``None`` if it is free."""
    if port == FORBIDDEN_PORT:
        return f"port {FORBIDDEN_PORT} is the default cao-server port; pass a different --port"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return f"port {port} already answers; pass a free --port"
    # Bind the way cao-server (uvicorn) does, with SO_REUSEADDR, so a port a
    # previous run left in TIME_WAIT counts as free. A live listener still fails
    # the connect probe above, and still blocks this bind.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as e:
            return f"port {port} cannot be bound ({e}); pass a free --port"
    return None


def _bin(name: str) -> str:
    sibling = Path(sys.executable).with_name(name)
    if sibling.exists():
        return str(sibling)
    found = shutil.which(name)
    if not found:
        raise RecorderError(f"{name} not found next to {sys.executable} or on PATH")
    return found


class _StreamListener:
    """Collects ``/agui/v1/stream`` frames on a daemon thread."""

    def __init__(self, url: str) -> None:
        import requests

        self.frames: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._response = requests.get(url, stream=True, timeout=(5, None))
        self._response.raise_for_status()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        data: List[str] = []
        try:
            for line in self._response.iter_lines(decode_unicode=True):
                if line is None:
                    continue
                if line == "":
                    if data:
                        try:
                            frame = json.loads("\n".join(data))
                        except json.JSONDecodeError:
                            frame = None
                        if isinstance(frame, dict):
                            with self._lock:
                                self.frames.append(frame)
                    data = []
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip())
        except Exception:
            return

    def pending_cards(self, terminal_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            frames = list(self.frames)
        return [
            f
            for f in frames
            if f.get("type") == "CUSTOM"
            and f.get("name") == CAO_GENERATIVE_UI
            and (f.get("value") or {}).get("component") == "approval_card"
            and (f.get("value") or {}).get("terminal_id") == terminal_id
            and "resolved" not in ((f.get("value") or {}).get("props") or {})
        ]

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:
            pass


def _wait(what: str, bound: float, probe: Callable[[], Any], log: Optional[Path] = None) -> Any:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.25)
    where = f" (server log: {log})" if log else ""
    raise RecorderError(f"{what} within {bound:.0f}s{where}")


class _Server:
    """An isolated cao-server: fresh CAO_HOME_DIR, private tmux, loopback only."""

    def __init__(self, mode: str, port: int) -> None:
        self.mode = mode
        self.port = port
        self.base = f"http://127.0.0.1:{port}"
        self.root = Path(tempfile.mkdtemp(prefix="cao-dojo-trace."))
        self.log = self.root / "server.log"
        self.env = dict(os.environ)
        for name in TMUX_CLIENT_VARS:
            self.env.pop(name, None)
        for name in ("cao", "tmux", "work"):
            (self.root / name).mkdir()
        self.env.update(
            CAO_HOME_DIR=str(self.root / "cao"),
            TMUX_TMPDIR=str(self.root / "tmux"),
            CAO_AGUI_ENABLED="1",
            CAO_API_HOST="127.0.0.1",
            CAO_API_PORT=str(port),
        )
        if mode == "mock":
            # Keyless: nothing needs the real HOME, so nothing may write to it.
            (self.root / "home").mkdir()
            self.env.update(
                HOME=str(self.root / "home"),
                CAO_MOCK_CLI_SCRIPTED_PROMPTS="1",
                PATH=f"{MOCK_BIN_DIR}{os.pathsep}{self.env.get('PATH', '')}",
            )
        self.proc: Optional[subprocess.Popen] = None
        self.ok = False

    def start(self) -> None:
        import requests

        with open(self.log, "wb") as log:
            self.proc = subprocess.Popen(
                [_bin("cao-server"), "--host", "127.0.0.1", "--port", str(self.port)],
                env=self.env,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=str(self.root / "work"),
                start_new_session=True,
            )

        def healthy() -> bool:
            if self.proc is not None and self.proc.poll() is not None:
                raise RecorderError(f"cao-server exited {self.proc.returncode} (log: {self.log})")
            try:
                return requests.get(f"{self.base}/health", timeout=2).status_code == 200
            except requests.RequestException:
                return False

        _wait("cao-server did not become healthy", 60.0, healthy, self.log)
        lock = json.loads((self.root / "cao" / "server.lock").read_text())
        installed = importlib.metadata.version("cli-agent-orchestrator")
        if lock.get("version") != installed:
            raise RecorderError(
                f"server.lock version {lock.get('version')!r} != installed {installed!r}"
            )

    def cao(self, *args: str) -> None:
        result = subprocess.run(
            [_bin("cao"), *args], env=self.env, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise RecorderError(f"cao {' '.join(args)} failed: {result.stdout}{result.stderr}")

    def tmux_socket(self) -> Optional[Path]:
        """The private tmux server's socket, or None when there is no private one.

        Derived from this server's own ``TMUX_TMPDIR``, the path tmux itself uses
        (``$TMUX_TMPDIR/tmux-$UID/default``). Without a ``TMUX_TMPDIR`` there is no
        private server to name, and the default one is never this recorder's.
        """
        tmpdir = self.env.get("TMUX_TMPDIR")
        if not tmpdir:
            return None
        return Path(tmpdir) / f"tmux-{os.getuid()}" / "default"

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=SERVER_STOP_SECONDS)
            except Exception:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    self.proc.wait(timeout=SERVER_STOP_SECONDS)
                except Exception:
                    pass
        # Name the socket with -S and drop TMUX/TMUX_PANE here, at the call, rather
        # than rely on __init__ having done it: inside tmux, TMUX overrides
        # TMUX_TMPDIR, so a caller that skipped __init__ would otherwise reach
        # another server. A missing tmux or a timeout must not escape the caller's
        # `finally` and hide the RecorderError that brought it here.
        socket_path = self.tmux_socket()
        if socket_path is not None:
            env = {k: v for k, v in self.env.items() if k not in TMUX_CLIENT_VARS}
            try:
                subprocess.run(
                    ["tmux", "-S", str(socket_path), "kill-server"],
                    env=env,
                    capture_output=True,
                    timeout=15,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as e:
                print(f"[record_trace] tmux kill-server failed: {e}", file=sys.stderr)
        if self.ok:
            shutil.rmtree(self.root, ignore_errors=True)
        else:
            print(
                f"[record_trace] kept {self.root} for inspection (server log: {self.log})",
                file=sys.stderr,
            )


def capture_server(mode: str, port: int) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    import requests

    refusal = port_refusal(port)
    if refusal:
        raise RecorderError(refusal)
    bound = WAIT_SECONDS[mode]
    if mode == "live":
        provider, profile, profile_name = "claude_code", LIVE_PROFILE, "dojo-approver"
        version_cmd = ["claude", "--version"]
    else:
        provider, profile, profile_name = "mock_cli", MOCK_PROFILE, "dojo-trace-mock"
        version_cmd = [str(MOCK_BIN_DIR / "mock_cli"), "--version"]
    try:
        provider_version = subprocess.run(
            version_cmd, capture_output=True, text=True, timeout=30, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as e:
        raise RecorderError(f"`{' '.join(version_cmd)}` failed: {e}")
    meta = build_meta(mode, provider, provider_version)

    server = _Server(mode, port)
    listener: Optional[_StreamListener] = None
    try:
        server.start()
        server.cao("install", str(profile))
        base = server.base

        def run(rid: str, resume: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
            response = requests.post(
                f"{base}/agui/v1/run",
                json=run_input(rid, resume),
                headers={"Accept": "text/event-stream"},
                timeout=(5, bound),
            )
            response.raise_for_status()
            return parse_sse_data(response.text)

        def status(terminal_id: str) -> str:
            return requests.get(f"{base}/terminals/{terminal_id}", timeout=5).json().get("status")

        def output(terminal_id: str) -> str:
            response = requests.get(
                f"{base}/terminals/{terminal_id}/output", params={"mode": "full"}, timeout=10
            )
            return response.json().get("output", "")

        def sessions() -> List[Dict[str, Any]]:
            return requests.get(f"{base}/sessions", timeout=5).json()

        records: List[Dict[str, Any]] = []
        rid = run_id("none", 1)
        if sessions():
            raise RecorderError("none: the fleet is not empty before the first capture")
        records += _run_records("none", rid, run_input(rid), run(rid))

        listener = _StreamListener(f"{base}/agui/v1/stream")
        for capture in ("approve", "reject"):
            approved = capture == "approve"
            work = server.root / "work" / capture
            work.mkdir()
            created = requests.post(
                f"{base}/sessions",
                params={
                    "agent_profile": profile_name,
                    "provider": provider,
                    "session_name": f"dojo-trace-{capture}",
                    "working_directory": str(work),
                },
                timeout=bound,
            )
            if created.status_code >= 300:
                raise RecorderError(
                    f"{capture}: launch failed {created.status_code} {created.text}"
                )
            terminal = created.json()
            terminal_id, session_name = terminal["id"], terminal["session_name"]
            _wait(
                f"{capture}: terminal {terminal_id} did not reach idle",
                bound,
                lambda: status(terminal_id) in ("idle", "completed"),
                server.log,
            )
            message = MOCK_PROMPT if mode == "mock" else LIVE_TASK
            requests.post(
                f"{base}/terminals/{terminal_id}/input", params={"message": message}, timeout=30
            ).raise_for_status()
            _wait(
                f"{capture}: terminal {terminal_id} did not reach waiting_user_answer",
                bound,
                lambda: status(terminal_id) == "waiting_user_answer",
                server.log,
            )
            cards = _wait(
                f"{capture}: expected 1 pending approval_card for {terminal_id}, got 0",
                bound,
                lambda: listener.pending_cards(terminal_id),
                server.log,
            )
            if len(cards) != 1:
                raise RecorderError(
                    f"{capture}: expected 1 pending approval_card for {terminal_id}, got {len(cards)}"
                )
            records.append({"capture": capture, "plane": "stream", "dir": "out", "frame": cards[0]})

            rid1 = run_id(capture, 1)
            frames1 = run(rid1)
            records += _run_records(capture, rid1, run_input(rid1), frames1)
            interrupt = _only_interrupt(capture, frames1)

            rid2 = run_id(capture, 2)
            resume = [resume_entry(interrupt["id"], approved)]
            frames2 = run(rid2, resume)
            records += _run_records(capture, rid2, run_input(rid2, resume), frames2)
            check_run2(capture, _finished(frames2))

            # Side effects: asserted, not recorded.
            if mode == "mock":
                answer = "y" if approved else "n"

                def answered() -> str:
                    pane = output(terminal_id)
                    return (
                        pane if ("> MOCK: answered" in pane or "unexpected answer" in pane) else ""
                    )

                seen = _wait(
                    f"{capture}: the pane never showed the answer", bound, answered, server.log
                )
                if "unexpected answer" in seen or f"> MOCK: answered {answer}" not in seen:
                    raise RecorderError(
                        f"{capture}: expected `> MOCK: answered {answer}` in the pane; "
                        "CAO's decision translation changed"
                    )
            else:
                _wait(
                    f"{capture}: the agent did not finish after the answer",
                    bound,
                    lambda: status(terminal_id) in ("idle", "completed"),
                    server.log,
                )
                written = (work / APPROVED_FILE).exists()
                if written != approved:
                    raise RecorderError(
                        f"{capture}: {APPROVED_FILE} {'missing' if approved else 'written'}"
                    )

            requests.delete(f"{base}/sessions/{session_name}", timeout=60)
            _wait(
                f"{capture}: GET /sessions did not empty after shutdown",
                bound,
                lambda: sessions() == [],
                server.log,
            )
        server.ok = True
        return meta, records
    finally:
        if listener is not None:
            listener.close()
        server.stop()


# --- CLI -----------------------------------------------------------------------


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    print(f"[record_trace] wrote {_rel(path)} ({len(data)} bytes)")


def _drift(committed: bytes, live: bytes, mode: str) -> str:
    diff = first_shape_difference(json.loads(committed), json.loads(live))
    return (
        f"DRIFT {diff}\n"
        f"The {mode} recording no longer matches {_rel(SHAPE_PATH)}. If the change is "
        "intended, regenerate the shape keylessly with\n"
        "  uv run python examples/ag-ui/cao-dojo/tools/record_trace.py --mock --write-shape\n"
        "and commit it. The published trace is recorded with a logged-in Claude Code "
        f"(`{RECORD_COMMAND}`); if you cannot run that, say so in the PR and a maintainer "
        "re-records before release."
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--live", action="store_true", help="Claude Code (local, credentialed)")
    modes.add_argument("--mock", action="store_true", help="mock_cli (keyless; CI)")
    modes.add_argument("--offline", action="store_true", help="in-process, no server")
    modes.add_argument("--check-debt", action="store_true", help="is the published trace current?")
    parser.add_argument("--check", action="store_true", help="tier 2: compare with the shape file")
    parser.add_argument("--write-shape", action="store_true", help="write the shape file")
    parser.add_argument("--out", type=Path, help="--live only: write the published trace here")
    parser.add_argument("--emit", type=Path, help="write the normalised trace to a scratch path")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--allow-dirty", action="store_true", help="record cao_build_dirty: true")
    parser.add_argument("--warn", action="store_true", help="--check-debt: warn instead of fail")
    parser.add_argument("--trace", type=Path, default=TRACE_PATH, help=argparse.SUPPRESS)
    parser.add_argument("--shape", type=Path, default=SHAPE_PATH, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.check_debt:
        ok, message = check_debt(args.trace, args.shape)
        if ok:
            print(f"[record_trace] {message}")
            return 0
        if args.warn:
            print(f"::warning::{message}")
            return 0
        print(f"::error::{message}")
        return 1

    if not (args.live or args.mock or args.offline):
        parser.error("choose --live, --mock, --offline or --check-debt")
    if args.out and not args.live:
        parser.error("--out writes the published trace, which is recorded --live only")
    if args.emit and args.emit.resolve() == args.trace.resolve():
        parser.error("--emit is a scratch path; the published trace is written with --live --out")
    mode = "live" if args.live else "mock" if args.mock else "offline"

    try:
        if mode != "offline":
            require_editable_checkout()
        if args.out and not args.allow_dirty and _git("status", "--porcelain"):
            raise RecorderError(
                "the checkout is dirty; commit first, or pass --allow-dirty to record "
                "cao_build_dirty: true"
            )
        if mode == "offline":
            meta, raw = capture_offline()
        else:
            meta, raw = capture_server(mode, args.port)
        data = normalise(meta, raw)
        validate(data)
        shape = shape_bytes(data)

        if args.emit:
            _write(args.emit, data)
        if args.write_shape:
            _write(args.shape, shape)
        if args.check:
            if not args.shape.is_file():
                raise RecorderError(f"shape file missing: {_rel(args.shape)}")
            committed = args.shape.read_bytes()
            if committed != shape:
                print(_drift(committed, shape, mode), file=sys.stderr)
                return 1
            print(f"[record_trace] {mode}: shape matches {_rel(args.shape)}")
        if args.out:
            if not args.shape.is_file() or args.shape.read_bytes() != shape:
                if not args.shape.is_file():
                    raise RecorderError(f"shape file missing: {_rel(args.shape)}")
                print(_drift(args.shape.read_bytes(), shape, mode), file=sys.stderr)
                raise RecorderError(
                    "the live recording does not project onto the committed shape, so "
                    "`--live` and `--mock` would disagree; refusing to write the trace"
                )
            meta = dict(meta, shape_sha256=sha256(shape))
            published = normalise(meta, parse(data)[1])
            validate(published)
            _write(args.out, published)
        print(f"[record_trace] {mode}: none/approve/reject OK, leak rule clean")
        return 0
    except RecorderError as e:
        print(f"[record_trace] FAIL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
