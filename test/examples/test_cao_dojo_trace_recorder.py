"""The CAO Dojo trace normaliser, shape projection and recorder guards (#1116).

No server: every test here runs on synthetic captures or the offline capture.
The live ``--mock --check`` e2e runs in CI's ``ag-ui-stock-client-live`` job.
"""

from __future__ import annotations

import copy
import importlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import pytest

from .cao_dojo_trace_support import offline_trace, record_trace
from .cao_dojo_trace_support import trace_scrub as scrub

META = {"schema": scrub.SCHEMA, "mode": "mock", "captures": list(scrub.CAPTURES)}


def raw_capture(
    terminal: str = "e327d8c9",
    interrupt: str | None = None,
    event: str | None = None,
    stamp: str = "2026-11-04T18:20:06.366094+00:00",
    reverse_keys: bool = False,
) -> List[Dict[str, Any]]:
    """One approve capture, shaped like the frames a live server sends."""
    interrupt = interrupt or str(uuid.uuid4())
    event = event or str(uuid.uuid4())
    card = {
        "type": "CUSTOM",
        "name": "cao.generative_ui",
        "value": {
            "event_id": event,
            "terminal_id": terminal,
            "session_name": None,
            "timestamp": stamp,
            "traceparent": None,
            "component": "approval_card",
            "props": {
                "interrupt_id": interrupt,
                "reason": "mock-cli:unknown_prompt",
                "message": "",
                "options": ["approve", "deny"],
                "provider": "mock_cli",
                "terminal_id": terminal,
            },
        },
    }
    snapshot = {
        "type": "STATE_SNAPSHOT",
        "snapshot": {
            "sessions": [{"id": "cao-p2probe", "name": "cao-p2probe", "status": "detached"}],
            "terminals": [
                {
                    "id": terminal,
                    "session_name": "cao-p2probe",
                    "provider": "mock_cli",
                    "agent_profile": "dojo-trace-mock",
                    "window": "dojo-trace-mock-cdce",
                    "status": None,
                    "last_active": stamp,
                }
            ],
            "counts": {"sessions": 1, "terminals": 1},
            "scopes": ["cao:read"],
        },
        "thread_id": "cao-dojo-trace",
        "run_id": "approve-run-1",
    }
    finished = {
        "type": "RUN_FINISHED",
        "threadId": "cao-dojo-trace",
        "runId": "approve-run-1",
        "outcome": {
            "type": "interrupt",
            "interrupts": [
                {
                    "id": interrupt,
                    "reason": "mock-cli:unknown_prompt",
                    "message": "",
                    "metadata": {
                        "provider": "mock_cli",
                        "terminalId": terminal,
                        "sessionName": None,
                    },
                }
            ],
        },
    }
    records = [
        {"capture": "approve", "plane": "stream", "dir": "out", "frame": card},
        {
            "capture": "approve",
            "plane": "run",
            "run": "approve-run-1",
            "dir": "request",
            "request": {"threadId": "x", "messages": [{"role": "user", "content": "hi"}]},
        },
        {
            "capture": "approve",
            "plane": "run",
            "run": "approve-run-1",
            "dir": "out",
            "frame": snapshot,
        },
        {
            "capture": "approve",
            "plane": "run",
            "run": "approve-run-1",
            "dir": "out",
            "frame": finished,
        },
    ]
    if reverse_keys:
        records = [_reversed(r) for r in records]
    return records


def _reversed(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _reversed(value[k]) for k in reversed(list(value))}
    if isinstance(value, list):
        return [_reversed(v) for v in value]
    return value


# --- Determinism ---------------------------------------------------------------


def test_different_identities_and_timestamps_give_identical_bytes():
    first = scrub.normalise(META, raw_capture())
    second = scrub.normalise(
        META, raw_capture(terminal="0badf00d", stamp="2027-03-01T09:00:00.000001+00:00")
    )
    assert first == second


def test_key_order_does_not_matter():
    interrupt, event = str(uuid.uuid4()), str(uuid.uuid4())
    assert scrub.normalise(META, raw_capture(interrupt=interrupt, event=event)) == scrub.normalise(
        META, raw_capture(interrupt=interrupt, event=event, reverse_keys=True)
    )


def test_normalise_is_idempotent():
    once = scrub.normalise(META, raw_capture())
    assert scrub.renormalise(once) == once
    assert scrub.renormalise(offline_trace()) == offline_trace()


def test_one_interrupt_id_maps_to_one_token_across_planes():
    _, records = scrub.parse(scrub.normalise(META, raw_capture()))
    card, _, _, finished = records
    token = card["frame"]["value"]["props"]["interrupt_id"]
    assert token == finished["frame"]["outcome"]["interrupts"][0]["id"]
    assert scrub.TOKEN_RE.match(token) and token.startswith("id-")
    assert card["frame"]["value"]["terminal_id"] == "terminal-1"
    assert records[2]["frame"]["snapshot"]["sessions"][0] == {
        "id": "session-1",
        "name": "session-1",
        "status": "detached",
    }


def test_named_identities_get_kind_prefixed_tokens():
    _, records = scrub.parse(scrub.normalise(META, raw_capture()))
    row = records[2]["frame"]["snapshot"]["terminals"][0]
    assert (row["id"], row["session_name"], row["agent_profile"], row["window"]) == (
        "terminal-1",
        "session-1",
        "profile-1",
        "window-1",
    )


def test_a_runner_id_is_tokenised_and_null_stays_null():
    """A runner id is an operator-chosen label (it can look like a host name)."""
    raw = raw_capture()
    rows = raw[2]["frame"]["snapshot"]["terminals"]
    rows[0]["runner_id"] = "build-box-3.internal"
    rows.append({**rows[0], "id": "ffffffff", "runner_id": None})
    rows.append({**rows[0], "id": "eeeeeeee", "runner_id": "build-box-3.internal"})
    _, records = scrub.parse(scrub.normalise(META, raw))
    out = records[2]["frame"]["snapshot"]["terminals"]
    assert [r["runner_id"] for r in out] == ["runner-1", None, "runner-1"]
    assert "build-box-3" not in scrub.normalise(META, raw).decode()


def test_ag_ui_1_0_attribution_ids_are_remapped():
    """#1232's frames carry ``eventId``, ``subagentRunId`` and ``parentSubagentRunId``.

    Each is a per-run identity, so it is remapped through the ``id`` table: one
    raw value, one token, on both planes.
    """
    raw = raw_capture()
    card, finished = raw[0]["frame"], raw[3]["frame"]
    card["metadata"] = {"cao": {"eventId": str(uuid.uuid4()), "terminalId": "e327d8c9"}}
    card["subagentRunId"] = "terminal:e327d8c9"
    card["parentSubagentRunId"] = "session:0123456789abcdef"
    finished["outcome"]["interrupts"][0]["subagentRunId"] = "terminal:e327d8c9"
    _, records = scrub.parse(scrub.normalise(META, raw))
    out_card, out_finished = records[0]["frame"], records[3]["frame"]
    for value in (
        out_card["metadata"]["cao"]["eventId"],
        out_card["subagentRunId"],
        out_card["parentSubagentRunId"],
    ):
        assert scrub.TOKEN_RE.match(value) and value.startswith("id-"), value
    assert out_card["subagentRunId"] == out_finished["outcome"]["interrupts"][0]["subagentRunId"]
    assert out_card["subagentRunId"] != out_card["parentSubagentRunId"]


def test_raw_and_rewrapped_customs_are_dropped():
    raw = raw_capture()
    extra = [
        {"type": "RAW", "event": {"x": 1}},
        {"type": "CUSTOM", "name": "cao.raw", "value": {}},
        {"type": "CUSTOM", "name": "cao.message_delivery", "value": {}},
    ]
    for frame in extra:
        raw.insert(
            3,
            {
                "capture": "approve",
                "plane": "run",
                "run": "approve-run-1",
                "dir": "out",
                "frame": frame,
            },
        )
    raw[-1]["frame"]["rawEvent"] = {"secret": "/Users/x"}
    _, records = scrub.parse(scrub.normalise(META, raw))
    types = [(r.get("frame") or {}).get("type") for r in records]
    assert "RAW" not in types
    assert all((r.get("frame") or {}).get("name") in (None, "cao.generative_ui") for r in records)
    assert "rawEvent" not in records[-1]["frame"]


def test_timestamps_are_synthesised_and_keep_their_type():
    raw = raw_capture()
    raw[0]["frame"]["timestamp"] = 1767225600123  # an integer AG-UI timestamp
    _, records = scrub.parse(scrub.normalise(META, raw))
    assert records[0]["frame"]["timestamp"] == int(scrub.CLOCK_EPOCH.timestamp() * 1000)
    assert records[0]["frame"]["value"]["timestamp"] == "2026-01-01T00:00:01.000000+00:00"
    assert records[2]["frame"]["snapshot"]["terminals"][0]["last_active"] == (
        "2026-01-01T00:00:02.000000+00:00"
    )


def test_a_float_epoch_timestamp_is_synthesised_too():
    """A float timestamp is replaced like any other, and stays a float."""
    first, second = raw_capture(), raw_capture()
    first[0]["frame"]["timestamp"] = 1767225600.123
    second[0]["frame"]["timestamp"] = 1799999999.999
    out = [
        scrub.parse(scrub.normalise(META, raw))[1][0]["frame"]["timestamp"]
        for raw in (first, second)
    ]
    assert out[0] == out[1] == int(scrub.CLOCK_EPOCH.timestamp() * 1000)
    assert isinstance(out[0], float)
    assert scrub.renormalise(scrub.normalise(META, first)) == scrub.normalise(META, first)


def test_requests_are_reduced_to_resume():
    raw = raw_capture()
    raw[1]["request"] = {
        "threadId": "t",
        "messages": [{"role": "user", "content": "hi"}],
        "resume": [
            {
                "interruptId": "8d2a15cf-dfbc-4078-bdfe-c095495d781a",
                "status": "resolved",
                "payload": {"approved": True, "editedArgs": "rm -rf"},
                "metadata": {"x": 1},
            }
        ],
    }
    _, records = scrub.parse(scrub.normalise(META, raw))
    assert records[1]["request"] == {
        "resume": [{"interruptId": "id-3", "status": "resolved", "payload": {"approved": True}}]
    }


def test_seq_numbers_each_capture_from_one():
    _, records = scrub.parse(offline_trace())
    for capture in scrub.CAPTURES:
        seqs = [r["seq"] for r in records if r["capture"] == capture]
        assert seqs == list(range(1, len(seqs) + 1))


def test_trace_lines_are_canonical_json():
    text = offline_trace().decode()
    assert text.endswith("\n") and "\r" not in text
    for line in text.strip().split("\n"):
        assert scrub.dumps(json.loads(line)) == line


# --- Shape projection (the tier 2 baseline) ------------------------------------


def _as_live(data: bytes) -> bytes:
    """Rewrite every allowed-difference field the way a Claude Code recording has it."""
    meta, records = scrub.parse(data)
    meta = dict(meta, mode="live", provider="claude_code", provider_version="2.1.310 (Claude Code)")
    meta["cao_build"], meta["cao_build_dirty"] = "f" * 40, False
    for record in records:
        frame = record.get("frame") or {}
        for interrupt in (frame.get("outcome") or {}).get("interrupts") or []:
            interrupt["reason"] = "claude-code:permission_request"
            interrupt["metadata"]["provider"] = "claude_code"
        props = (frame.get("value") or {}).get("props")
        if props:
            props["reason"], props["provider"] = "claude-code:permission_request", "claude_code"
        for row in (frame.get("snapshot") or {}).get("terminals") or []:
            row["provider"] = "claude_code"
    return scrub.normalise(meta, records)


def test_the_projection_drops_every_allowed_difference():
    """Rewriting every allowed-difference field leaves the shape bytes unchanged.

    This proves the projection drops the allowed fields. It does not prove a real
    live recording differs from a mock one in no other way: only the
    `--live` recorder's refusal to write a non-projecting trace, and the
    release-path debt check, catch that.
    """
    assert scrub.shape_bytes(_as_live(offline_trace())) == scrub.shape_bytes(offline_trace())


def test_the_shape_ignores_the_published_traces_own_hash():
    meta, records = scrub.parse(offline_trace())
    stamped = scrub.normalise(dict(meta, shape_sha256="0" * 64), records)
    assert scrub.shape_bytes(stamped) == scrub.shape_bytes(offline_trace())


def test_a_new_key_is_drift():
    meta, records = scrub.parse(offline_trace())
    changed = copy.deepcopy(records)
    for record in changed:
        for interrupt in ((record.get("frame") or {}).get("outcome") or {}).get("interrupts") or []:
            interrupt["expiresAt"] = "2026-01-01T00:10:00.000000+00:00"
    diff = scrub.first_shape_difference(
        scrub.shape(offline_trace()), scrub.shape(scrub.normalise(meta, changed))
    )
    assert diff is not None
    assert "approve approve-run-1 RUN_FINISHED" in diff
    assert "expiresAt live-only" in diff


def test_a_changed_closed_value_is_drift():
    meta, records = scrub.parse(offline_trace())
    for record in records:
        for interrupt in ((record.get("frame") or {}).get("outcome") or {}).get("interrupts") or []:
            interrupt["metadata"]["options"] = ["approve", "deny", "edit"]
    diff = scrub.first_shape_difference(
        scrub.shape(offline_trace()), scrub.shape(scrub.normalise(meta, records))
    )
    assert diff is not None and "metadata.options" in diff


def test_the_allowed_differences_are_never_closed_values():
    for field in scrub.ALLOWED_DIFFERENCES:
        assert not any(closed.endswith(field) for closed in scrub.CLOSED_VALUE_PATHS), field


# --- Run 2 (the bridge-expiry race tripwire) -----------------------------------


def test_run2_with_skipped_resume_names_the_bridge_expiry_race():
    frame = {
        "type": "RUN_FINISHED",
        "outcome": {"type": "success"},
        "result": {"skippedResume": [{"interruptId": "id-2", "reason": "expired"}]},
    }
    with pytest.raises(record_trace.RecorderError) as e:
        record_trace.check_run2("approve", frame)
    message = str(e.value)
    assert "bridge-expiry race, not trace drift" in message
    assert "approval_bridge" in message and "run_plane" in message
    # Names functions, not line numbers, so an unrelated edit cannot make it wrong.
    for name in record_trace.RUN2_RACE_FUNCTIONS:
        assert name in message
        assert callable(_resolve(name)), name


def _resolve(dotted: str) -> Any:
    """Import the longest module prefix of ``dotted``, then walk the attributes."""
    parts = dotted.split(".")
    for cut in range(len(parts), 0, -1):
        try:
            obj = importlib.import_module(".".join(parts[:cut]))
        except ImportError:
            continue
        for attr in parts[cut:]:
            obj = getattr(obj, attr)
        return obj
    raise ImportError(dotted)


def test_run2_clean_success_passes():
    record_trace.check_run2("approve", {"type": "RUN_FINISHED", "outcome": {"type": "success"}})


@pytest.mark.parametrize(
    "frame",
    [
        {"type": "RUN_FINISHED", "outcome": {"type": "success"}, "result": {"x": 1}},
        {"type": "RUN_FINISHED", "outcome": {"type": "interrupt", "interrupts": []}},
        {"type": "RUN_ERROR", "message": "x"},
    ],
)
def test_run2_anything_else_fails(frame):
    with pytest.raises(record_trace.RecorderError):
        record_trace.check_run2("reject", frame)


# --- Outcomes and budget -------------------------------------------------------


def test_the_offline_capture_has_the_constructed_outcomes():
    _, records = scrub.parse(offline_trace())
    record_trace.assert_outcomes(records)
    record_trace.check_budget(offline_trace())


def test_a_swapped_decision_fails_the_outcome_check():
    _, records = scrub.parse(offline_trace())
    for record in records:
        if record["capture"] == "approve" and record.get("dir") == "request":
            for entry in record["request"]["resume"]:
                entry["payload"]["approved"] = False
    with pytest.raises(record_trace.RecorderError, match="approve: run 2 resume"):
        record_trace.assert_outcomes(records)


def test_a_missing_card_fails_the_outcome_check():
    _, records = scrub.parse(offline_trace())
    records = [r for r in records if not (r["capture"] == "reject" and r["plane"] == "stream")]
    with pytest.raises(record_trace.RecorderError, match="expected 1 pending approval_card"):
        record_trace.assert_outcomes(records)


def test_the_size_budget_is_enforced():
    meta, records = scrub.parse(offline_trace())
    records[0]["request"]["resume"] = [{"interruptId": "id-1", "status": "x" * 9000}]
    with pytest.raises(record_trace.RecorderError, match="bytes, over"):
        record_trace.check_budget(scrub.normalise(meta, records))


# --- Ports ---------------------------------------------------------------------


def _env_outside_tmux() -> Dict[str, str]:
    """This process's environment with no route to the tmux server it may run in."""
    return {k: v for k, v in os.environ.items() if k not in record_trace.TMUX_CLIENT_VARS}


def _bare_server(env: Dict[str, str], root: Path) -> Any:
    """A ``_Server`` built without ``__init__``: the case stop() must be safe for."""
    server = record_trace._Server.__new__(record_trace._Server)
    server.proc, server.ok, server.root, server.log, server.env = (
        None,
        True,
        root,
        root / "log",
        env,
    )
    return server


@pytest.fixture
def private_process_tmux(monkeypatch):
    """Give this process a private, existing TMUX_TMPDIR, and no TMUX or TMUX_PANE.

    tmux ignores a TMUX_TMPDIR it cannot resolve and falls back to /tmp, which
    is the default socket, so the directory must exist. It sits under /tmp to
    stay inside the unix socket path limit (about 104 bytes on macOS). Even a
    stop() that regressed to a bare `tmux kill-server` on this process's
    environment then finds only a private socket.
    """
    for name in record_trace.TMUX_CLIENT_VARS:
        monkeypatch.delenv(name, raising=False)
    path = Path(tempfile.mkdtemp(prefix="cdt-p.", dir="/tmp"))
    os.chmod(path, 0o700)
    assert path.is_dir()
    monkeypatch.setenv("TMUX_TMPDIR", str(path))
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_the_private_process_tmux_dir_exists_and_has_no_tmux_route(private_process_tmux):
    """The guard holds only if tmux can resolve the directory and nothing points elsewhere."""
    assert Path(os.environ["TMUX_TMPDIR"]) == private_process_tmux
    assert private_process_tmux.is_dir()
    assert not any(name in os.environ for name in record_trace.TMUX_CLIENT_VARS)


def test_stop_reaps_a_server_that_ignores_sigterm(monkeypatch, tmp_path, private_process_tmux):
    """After SIGKILL the process is waited for, so no zombie outlives stop()."""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ],
        start_new_session=True,
    )
    server = _bare_server(dict(_env_outside_tmux(), TMUX_TMPDIR=str(tmp_path)), tmp_path / "root")
    server.proc = proc
    monkeypatch.setattr(record_trace, "SERVER_STOP_SECONDS", 0.5)
    try:
        time.sleep(0.2)  # let the child install its handler
        server.stop()
        assert proc.returncode is not None, "stop() returned without reaping the server"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def _tmux(socket_path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run tmux against one named socket, never the caller's own server."""
    return subprocess.run(
        ["tmux", "-f", "/dev/null", "-S", str(socket_path), *args],
        env=_env_outside_tmux(),
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")
def test_stop_kills_its_own_private_tmux_server_and_no_other(private_process_tmux):
    """Inside tmux, TMUX overrides TMUX_TMPDIR. stop() must name its own socket anyway.

    Both servers here are private. ``decoy`` stands in for the tmux server the
    caller runs in: the recorder's environment carries ``TMUX`` pointing at it,
    as an inherited environment would. A bare ``tmux kill-server`` would end
    the decoy and leave the recorder's own server running.
    """
    # Short paths: a unix socket path is limited to about 104 bytes on macOS.
    base = Path(tempfile.mkdtemp(prefix="cdt.", dir="/tmp"))
    decoy = base / "decoy"
    tmux_tmpdir = base / "tmux"
    own_dir = tmux_tmpdir / f"tmux-{os.getuid()}"
    own_dir.mkdir(parents=True, mode=0o700)
    own = own_dir / "default"
    try:
        for socket_path in (decoy, own):
            started = _tmux(socket_path, "new-session", "-d", "-s", "keep", "sleep 600")
            assert started.returncode == 0, started.stderr
        decoy_pid = _tmux(decoy, "display-message", "-p", "#{pid}").stdout.strip()
        env = dict(
            _env_outside_tmux(),
            TMUX_TMPDIR=str(tmux_tmpdir),
            TMUX=f"{decoy},{decoy_pid},0",
            TMUX_PANE="%0",
        )
        server = _bare_server(env, base / "root")
        assert server.tmux_socket() == own

        server.stop()

        assert _tmux(decoy, "has-session").returncode == 0, "stop() reached the outside server"
        assert _tmux(own, "has-session").returncode != 0, "stop() left its own server running"
    finally:
        for socket_path in (decoy, own):
            _tmux(socket_path, "kill-server")
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.parametrize(
    "error",
    [FileNotFoundError("tmux"), subprocess.TimeoutExpired(["tmux"], 15)],
    ids=["tmux-missing", "tmux-timeout"],
)
def test_a_failing_tmux_does_not_escape_stop(monkeypatch, tmp_path, capsys, error):
    """stop() runs in a `finally`; a tmux failure there must not hide the real error."""

    def run(argv: List[str], **_: Any) -> None:
        raise error

    monkeypatch.setattr(record_trace.subprocess, "run", run)
    server = _bare_server(
        dict(_env_outside_tmux(), TMUX_TMPDIR=str(tmp_path / "tmux")), tmp_path / "root"
    )
    with pytest.raises(record_trace.RecorderError, match="the real failure"):
        try:
            raise record_trace.RecorderError("the real failure")
        finally:
            server.stop()
    assert "tmux kill-server failed" in capsys.readouterr().err


def test_stop_without_a_private_tmux_dir_runs_no_tmux(monkeypatch, tmp_path):
    """No TMUX_TMPDIR means no private server, so stop() must not guess one."""
    calls: List[List[str]] = []
    monkeypatch.setattr(record_trace.subprocess, "run", lambda argv, **_: calls.append(list(argv)))
    env = {k: v for k, v in _env_outside_tmux().items() if k != "TMUX_TMPDIR"}
    server = _bare_server(env, tmp_path / "root")
    assert server.tmux_socket() is None
    server.stop()
    assert calls == []


def test_the_default_cao_port_is_refused():
    assert "9889" in record_trace.port_refusal(9889)


def test_a_busy_port_is_refused():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        refusal = record_trace.port_refusal(port)
    assert refusal is not None and str(port) in refusal


def test_the_default_port_is_not_9889_and_is_configurable():
    assert record_trace.DEFAULT_PORT != 9889
    with pytest.raises(SystemExit):
        record_trace.main(["--mock", "--port", "not-a-port"])


def test_a_port_left_in_time_wait_by_a_previous_run_is_free():
    """A back-to-back re-run must not be refused: cao-server binds with SO_REUSEADDR."""
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    client = socket.create_connection(("127.0.0.1", port))
    served, _ = listener.accept()
    served.close()  # the server side closes first, so its end sits in TIME_WAIT
    client.close()
    listener.close()
    assert record_trace.port_refusal(port) is None


# --- Re-record debt (shape_sha256; warns on PRs, blocks the release) -----------


def _committed_pair(tmp_path, stamp: str | None):
    shape = tmp_path / "shape.json"
    trace = tmp_path / "trace.ndjson"
    shape.write_bytes(scrub.shape_bytes(offline_trace()))
    if stamp is not None:
        meta, records = scrub.parse(offline_trace())
        trace.write_bytes(scrub.normalise(dict(meta, shape_sha256=stamp), records))
    return trace, shape


def test_debt_reports_a_missing_trace(tmp_path):
    trace, shape = _committed_pair(tmp_path, None)
    ok, message = record_trace.check_debt(trace, shape)
    assert not ok and message.startswith("trace missing or stale")
    assert "make record-agui-trace" in message


def test_debt_reports_a_stale_hash(tmp_path):
    trace, shape = _committed_pair(tmp_path, "0" * 64)
    ok, message = record_trace.check_debt(trace, shape)
    assert not ok and "trace missing or stale" in message


def test_debt_passes_when_current(tmp_path):
    trace, shape = _committed_pair(tmp_path, scrub.sha256(scrub.shape_bytes(offline_trace())))
    assert record_trace.check_debt(trace, shape) == (
        True,
        f"trace current: shape_sha256 {scrub.sha256(shape.read_bytes())}",
    )


def test_debt_reports_a_trace_that_no_longer_projects(tmp_path):
    trace, shape = _committed_pair(tmp_path, None)
    meta, records = scrub.parse(offline_trace())
    records = records[:-1]
    trace.write_bytes(
        scrub.normalise(dict(meta, shape_sha256=scrub.sha256(shape.read_bytes())), records)
    )
    ok, message = record_trace.check_debt(trace, shape)
    assert not ok and "no longer projects" in message


def test_debt_warns_on_prs_and_blocks_on_the_release_path(tmp_path, capsys):
    trace, shape = _committed_pair(tmp_path, None)
    common = ["--check-debt", "--trace", str(trace), "--shape", str(shape)]
    assert record_trace.main([*common, "--warn"]) == 0
    assert capsys.readouterr().out.startswith("::warning::trace missing or stale")
    assert record_trace.main(common) == 1
    assert capsys.readouterr().out.startswith("::error::trace missing or stale")


def test_the_release_debt_check_runs_on_a_bare_stdlib_python(tmp_path):
    """publish-to-pypi.yml runs this with a plain `python` and no uv or site-packages.

    ``-I -S`` drops PYTHONPATH, user site and site-packages, so any non-stdlib
    import that ``--check-debt`` reaches fails here, not at release time.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            str(record_trace.TOOLS_DIR / "record_trace.py"),
            "--check-debt",
            "--trace",
            str(tmp_path / "missing.ndjson"),
            "--shape",
            str(record_trace.SHAPE_PATH),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 1, result.stderr
    assert result.stdout.startswith("::error::trace missing or stale"), result.stdout


# --- CLI guards ----------------------------------------------------------------


def test_only_live_may_write_the_published_trace(tmp_path):
    with pytest.raises(SystemExit) as e:
        record_trace.main(["--mock", "--out", str(tmp_path / "t.ndjson")])
    assert e.value.code == 2


def test_emit_refuses_the_published_trace_path():
    with pytest.raises(SystemExit) as e:
        record_trace.main(["--offline", "--emit", str(record_trace.TRACE_PATH)])
    assert e.value.code == 2


def test_offline_check_reports_drift_against_a_changed_shape(tmp_path, capsys):
    doctored = json.loads(scrub.shape_bytes(offline_trace()))
    doctored["records"][1]["paths"].remove("frame.protocolVersion")
    shape = tmp_path / "shape.json"
    shape.write_text(json.dumps(doctored, sort_keys=True, indent=1) + "\n")
    assert record_trace.main(["--offline", "--check", "--shape", str(shape)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("DRIFT none none-run-1 RUN_STARTED: key frame.protocolVersion live-only")
    assert "--mock --write-shape" in err and "make record-agui-trace" in err


def test_an_offline_capture_runs_no_git(monkeypatch):
    """Tier 1 must work in a tree that is not a git checkout (a `git archive` export)."""

    def no_git(*args: str) -> str:
        raise AssertionError(f"git {' '.join(args)} was run for an offline capture")

    monkeypatch.setattr(record_trace, "_git", no_git)
    meta = record_trace.build_meta("offline", "mock_cli", "offline")
    assert meta["cao_build"] is None and meta["cao_build_dirty"] is None


def test_a_server_capture_outside_a_git_checkout_names_no_build(monkeypatch):
    def not_a_checkout(*args: str) -> str:
        raise subprocess.CalledProcessError(128, ["git", *args])

    monkeypatch.setattr(record_trace, "_git", not_a_checkout)
    meta = record_trace.build_meta("mock", "mock_cli", "mock")
    assert meta["cao_build"] is None and meta["cao_build_dirty"] is None


def test_out_outside_a_git_checkout_is_refused_before_any_capture(monkeypatch, tmp_path, capsys):
    """`--out` must name the build it records (`_meta.cao_build`), so it refuses a non-checkout.

    The refusal comes before the capture: nothing is launched and no file is written.
    """

    def no_capture(*args: Any) -> None:
        raise AssertionError("capture_server ran although --out was refused")

    monkeypatch.setattr(record_trace, "require_editable_checkout", lambda: None)
    monkeypatch.setattr(record_trace, "_in_git_work_tree", lambda: False)
    monkeypatch.setattr(record_trace, "capture_server", no_capture)
    out = tmp_path / "t.ndjson"

    assert record_trace.main(["--live", "--out", str(out)]) == 1
    assert "run it in a git checkout" in capsys.readouterr().err
    assert not out.exists()


def test_offline_write_shape_writes_the_projection_to_the_given_path(tmp_path):
    shape = tmp_path / "shape.json"
    assert record_trace.main(["--offline", "--write-shape", "--shape", str(shape)]) == 0
    assert shape.read_bytes() == scrub.shape_bytes(offline_trace())
    assert shape.read_bytes() == record_trace.SHAPE_PATH.read_bytes()


def test_offline_emit_writes_the_normalised_trace(tmp_path):
    out = tmp_path / "offline.ndjson"
    assert record_trace.main(["--offline", "--emit", str(out)]) == 0
    _, records = scrub.parse(out.read_bytes())
    record_trace.assert_outcomes(records)
