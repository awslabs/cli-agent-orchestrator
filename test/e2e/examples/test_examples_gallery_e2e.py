"""End-to-end proofs for the U6 examples gallery (issue #312, Bolt 4, FR-7.1/BR-14).

Each example script is spawned as a REAL subprocess via U4's
``run_script_workflow`` (real ``asyncio.create_subprocess_exec``, real
``cao_workflow``/``urllib`` transport) — no server/tmux fixture skip, per
``test/e2e/examples/conftest.py``. Since these scripts DO call ``run_step``
(unlike U4's own M1/M2 self-contained proofs), a minimal stdlib
``http.server`` fake stands in for ``cao-server``'s ``/terminals/run-step``
route: this proves the shim's REAL HTTP client behavior end-to-end (real
socket, real JSON wire format) without requiring a live tmux-backed
cao-server or an authenticated provider CLI, mirroring FR-6.1/BR-14's
pass/fail (loop: N iterations recorded; conditional: the taken branch's step
ran, the other didn't; fan-out: all shards' steps recorded with distinct
step_ids) — each of these is asserted below via the fake server's recorded
calls, not by re-deriving them from provider output. The raw-HTTP loop
example makes an IDENTICAL call shape with no ``cao_workflow`` import.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.workflow_runtime import RunState
from cli_agent_orchestrator.services.script_runner import run_script_workflow

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]

_EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "docs" / "examples"


class _RunStepFakeHandler(BaseHTTPRequestHandler):
    """Records every POST body and answers 200 with a canned RunStepResponse."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
        self.server.recorded_calls.append(body)  # type: ignore[attr-defined]

        step_id = body.get("env_vars", {}).get("CAO_WORKFLOW_STEP_ID", "unknown")
        response = json.dumps(
            {
                "terminal_id": f"term-{step_id}",
                "last_message": f"ack:{step_id}",
                "status": "COMPLETED",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, format, *args):  # noqa: A002 — silence default stderr logging
        return


@pytest.fixture
def fake_run_step_server():
    server = HTTPServer(("127.0.0.1", 0), _RunStepFakeHandler)
    server.recorded_calls = []  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


class _RealSpec:
    """Duck-typed ScriptSpec pointing at a real example file on disk."""

    def __init__(self, path: Path, name: str):
        self.path = str(path)
        self.source = path.read_text(encoding="utf-8")
        self.name = name
        self.content_hash = "e2e"


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )
    from cli_agent_orchestrator.services import workflow_service

    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "wf.db", raising=True
    )
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    yield


@pytest.fixture
def _api_base_url(fake_run_step_server, monkeypatch):
    host, port = fake_run_step_server.server_address
    base_url = f"http://{host}:{port}"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.script_runner.API_BASE_URL", base_url, raising=True
    )
    return base_url


async def test_loop_example_records_n_iterations(_api_base_url, fake_run_step_server):
    spec = _RealSpec(_EXAMPLES_DIR / "loop_example.py", "loop-example")

    result = await run_script_workflow(spec, {}, "e2e-loop")

    assert result.state == RunState.COMPLETED
    assert result.output["iterations"] == 3
    assert len(result.output["outputs"]) == 3
    step_ids = [c["env_vars"]["CAO_WORKFLOW_STEP_ID"] for c in fake_run_step_server.recorded_calls]
    assert step_ids == ["call-1", "call-2", "call-3"]


async def test_conditional_example_runs_only_the_taken_branch(_api_base_url, fake_run_step_server):
    """IS_URGENT is a script-level constant (no env passthrough exists for
    this — U4's spawn env is a fixed identity-only allowlist), so this test
    exercises the file's real content (IS_URGENT = True) as shipped."""
    spec = _RealSpec(_EXAMPLES_DIR / "conditional_example.py", "conditional-example")

    result = await run_script_workflow(spec, {}, "e2e-conditional")

    assert result.state == RunState.COMPLETED
    assert result.output["branch_taken"] == "urgent"
    step_ids = [c["env_vars"]["CAO_WORKFLOW_STEP_ID"] for c in fake_run_step_server.recorded_calls]
    assert step_ids == ["urgent-branch"]  # the routine branch's step never ran


async def test_fanout_example_records_distinct_step_ids_per_shard(
    _api_base_url, fake_run_step_server
):
    spec = _RealSpec(_EXAMPLES_DIR / "fanout_example.py", "fanout-example")

    result = await run_script_workflow(spec, {}, "e2e-fanout")

    assert result.state == RunState.COMPLETED
    assert set(result.output["shards"].keys()) == {"alpha", "beta", "gamma"}
    step_ids = [c["env_vars"]["CAO_WORKFLOW_STEP_ID"] for c in fake_run_step_server.recorded_calls]
    assert sorted(step_ids) == ["shard-alpha", "shard-beta", "shard-gamma"]
    assert len(set(step_ids)) == 3  # pairwise distinct


async def test_loop_raw_http_example_works_without_the_shim(_api_base_url, fake_run_step_server):
    """Q2=A shim-optionality proof: same shape, zero cao_workflow import."""
    spec = _RealSpec(_EXAMPLES_DIR / "loop_raw_http_example.py", "loop-raw-http-example")

    result = await run_script_workflow(spec, {}, "e2e-loop-raw-http")

    assert result.state == RunState.COMPLETED
    assert result.output["iterations"] == 3
    assert len(result.output["outputs"]) == 3
    assert "import cao_workflow" not in spec.source
