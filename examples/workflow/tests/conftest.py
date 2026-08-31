"""Fixtures for the workflow example's deterministic tests (issue #591).

Reuses the ``_RunStepFakeHandler`` fake-HTTP-server pattern from
``test/e2e/examples/test_examples_gallery_e2e.py``: a minimal stdlib
``http.server`` stands in for cao-server's ``/terminals/run-step`` route, so
these tests exercise the real ``cao_workflow`` HTTP transport and the real
``run_script_workflow`` subprocess engine without a live tmux-backed
cao-server or an authenticated provider CLI. Extended with a
``fail_step_ids`` switch so a test can force one fan-out unit to fail
(``ShimHTTPError``) and prove the survivors are kept.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


class _RunStepFakeHandler(BaseHTTPRequestHandler):
    """Records every POST body. Answers 200 with a canned RunStepResponse,
    unless the call's step_id is in ``server.fail_step_ids`` — then answers
    500 so the shim raises ``ShimHTTPError`` for that one call."""

    def do_POST(self):  # noqa: N802 — BaseHTTPRequestHandler's naming convention
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        body = json.loads(raw.decode("utf-8"))
        self.server.recorded_calls.append(body)  # type: ignore[attr-defined]

        step_id = body.get("env_vars", {}).get("CAO_WORKFLOW_STEP_ID", "unknown")
        if step_id in self.server.fail_step_ids:  # type: ignore[attr-defined]
            payload = json.dumps({"detail": f"forced failure for step '{step_id}'"}).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        response = json.dumps(
            {
                "terminal_id": f"term-{step_id}",
                "last_message": f"ack:{step_id}",
                "status": "COMPLETED",
                # Must be present: the shim reads ``data["replayed"]`` by
                # direct indexing on purpose (BR-3/TD-7), so a fake that
                # omits it raises KeyError instead of standing in for the
                # real route. ``RunStepResponse.replayed`` defaults to
                # False and the server always serialises it; this fake
                # always executes fresh, so False is the honest value.
                "replayed": False,
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
    server.fail_step_ids = set()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    """Isolate every test from the real ``~/.aws/cli-agent-orchestrator`` DB.

    ``run_script_workflow`` journals to the configured SQLite file — without
    this override these tests would write into whatever CAO home the machine
    running them has configured (mirrors the ``_temp_db`` fixture in
    ``test/e2e/examples/test_examples_gallery_e2e.py``).
    """
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
def api_base_url(fake_run_step_server, monkeypatch):
    """Point ``script_runner.API_BASE_URL`` at the fake server for this test."""
    host, port = fake_run_step_server.server_address
    base_url = f"http://{host}:{port}"
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.script_runner.API_BASE_URL", base_url, raising=True
    )
    return base_url
