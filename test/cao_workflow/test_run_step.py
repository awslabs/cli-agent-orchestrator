"""Unit tests for cao_workflow.run_step (BR-1, BR-3..BR-6, BR-9, BR-17).

Every test mocks the transport (``cao_workflow._transport._post``) — no real
socket, no running server (mirrors U4's "mock the boundary, assert behavior"
pattern per cicd-pipeline.md).
"""

from __future__ import annotations

import json
import threading

import pytest

import cao_workflow
from cao_workflow._transport import URLError, _Response

_ENV = {
    "CAO_WORKFLOW_RUN_ID": "run-1",
    "CAO_WORKFLOW_GENERATION": "1",
    "CAO_API_BASE_URL": "http://localhost:9889",
}


@pytest.fixture(autouse=True)
def _reset_counter(monkeypatch):
    """Each test gets a fresh call-order counter (module-global, BR-3)."""
    import cao_workflow._counter as counter_mod

    monkeypatch.setattr(counter_mod, "_counter", 0)


@pytest.fixture
def full_env(monkeypatch):
    for k, v in _ENV.items():
        monkeypatch.setenv(k, v)


def _success_response(terminal_id="term-1", last_message="hi", status="COMPLETED"):
    return _Response(
        status=200,
        body=json.dumps(
            {"terminal_id": terminal_id, "last_message": last_message, "status": status}
        ),
    )


class TestIdentityResolution:
    def test_missing_run_id_raises_before_any_http(self, monkeypatch):
        monkeypatch.delenv("CAO_WORKFLOW_RUN_ID", raising=False)
        monkeypatch.setenv("CAO_WORKFLOW_GENERATION", "1")
        monkeypatch.setenv("CAO_API_BASE_URL", "http://localhost:9889")
        calls = []
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: calls.append(1))

        with pytest.raises(cao_workflow.ShimIdentityError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert calls == []
        assert "CAO_WORKFLOW_RUN_ID" in str(exc_info.value)

    def test_missing_generation_message_never_echoes_present_run_id(self, monkeypatch):
        monkeypatch.setenv("CAO_WORKFLOW_RUN_ID", "super-secret-run-id")
        monkeypatch.delenv("CAO_WORKFLOW_GENERATION", raising=False)
        monkeypatch.setenv("CAO_API_BASE_URL", "http://localhost:9889")

        with pytest.raises(cao_workflow.ShimIdentityError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert "super-secret-run-id" not in str(exc_info.value)
        assert "CAO_WORKFLOW_GENERATION" in str(exc_info.value)

    def test_missing_base_url_raises(self, monkeypatch):
        monkeypatch.setenv("CAO_WORKFLOW_RUN_ID", "run-1")
        monkeypatch.setenv("CAO_WORKFLOW_GENERATION", "1")
        monkeypatch.delenv("CAO_API_BASE_URL", raising=False)

        with pytest.raises(cao_workflow.ShimIdentityError):
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")


class TestStepKeyResolution:
    def test_sequential_calls_get_call_n_keys(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _success_response())

        h1 = cao_workflow.run_step("kiro_cli", "reviewer", "one")
        h2 = cao_workflow.run_step("kiro_cli", "reviewer", "two")

        assert h1.step_id == "call-1"
        assert h2.step_id == "call-2"

    def test_explicit_step_id_used_verbatim(self, full_env, monkeypatch):
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _success_response())

        handle = cao_workflow.run_step("kiro_cli", "reviewer", "x", step_id="shard-7")

        assert handle.step_id == "shard-7"

    def test_concurrent_calls_get_distinct_keys_no_duplicates(self, full_env, monkeypatch):
        """BR-3: N threads calling with step_id=None never produce duplicate keys."""
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: _success_response())

        n = 50
        results = []
        results_lock = threading.Lock()

        def worker():
            handle = cao_workflow.run_step("kiro_cli", "reviewer", "x")
            with results_lock:
                results.append(handle.step_id)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(results) == n
        assert len(set(results)) == n  # pairwise distinct, no gaps checked below
        assert set(results) == {f"call-{i}" for i in range(1, n + 1)}


class TestBR17ReuseTerminalIdReject:
    def test_reuse_terminal_id_rejected_client_side_zero_http(self, full_env, monkeypatch):
        calls = []
        monkeypatch.setattr(cao_workflow, "_post", lambda *a, **k: calls.append(1))

        with pytest.raises(cao_workflow.ShimError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi", reuse_terminal_id="term-1")

        assert calls == []
        assert "reuse_terminal_id" in str(exc_info.value)


class TestTransportErrorTaxonomy:
    def test_urlerror_wraps_to_shim_transport_error_no_retry(self, full_env, monkeypatch):
        call_count = []

        def fake_post(*args, **kwargs):
            call_count.append(1)
            raise URLError("connection refused")

        monkeypatch.setattr(cao_workflow, "_post", fake_post)

        with pytest.raises(cao_workflow.ShimTransportError):
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert len(call_count) == 1  # no retry attempted

    def test_non_200_raises_shim_http_error_with_status_and_body(self, full_env, monkeypatch):
        monkeypatch.setattr(
            cao_workflow, "_post", lambda *a, **k: _Response(status=500, body="boom")
        )

        with pytest.raises(cao_workflow.ShimHTTPError) as exc_info:
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")

        assert exc_info.value.status == 500
        assert exc_info.value.body == "boom"

    def test_malformed_json_on_200_propagates_unwrapped(self, full_env, monkeypatch):
        monkeypatch.setattr(
            cao_workflow, "_post", lambda *a, **k: _Response(status=200, body="not json")
        )

        with pytest.raises(json.JSONDecodeError):
            cao_workflow.run_step("kiro_cli", "reviewer", "hi")


class TestSuccessPath:
    def test_returns_step_handle_from_response(self, full_env, monkeypatch):
        captured_body = {}

        def fake_post(url, body, timeout=None):
            captured_body.update(body)
            return _success_response(terminal_id="term-42", last_message="done", status="COMPLETED")

        monkeypatch.setattr(cao_workflow, "_post", fake_post)

        handle = cao_workflow.run_step("kiro_cli", "reviewer", "review this")

        assert handle.terminal_id == "term-42"
        assert handle.output == "done"
        assert handle.status == "COMPLETED"
        assert captured_body["env_vars"] == {
            "CAO_WORKFLOW_RUN_ID": "run-1",
            "CAO_WORKFLOW_GENERATION": "1",
            "CAO_WORKFLOW_STEP_ID": "call-1",
        }
        assert captured_body["provider"] == "kiro_cli"
        assert captured_body["agent"] == "reviewer"
        assert captured_body["prompt"] == "review this"
