"""Bolt-1 DoD equivalence test (issue #312, N0, BR-8 / RD-1.2).

Asserts that the two step callers drive an IDENTICAL step sequence through the
shared run_agent_step substrate:

  - the engine path: a direct in-process ``run_agent_step(...)`` call;
  - the handoff path: ``_handoff_impl`` -> ``POST /terminals/run-step`` ->
    ``run_agent_step(...)`` (the MCP client's six-call loop collapsed to one).

Both must produce the same ordered terminal-layer calls (create -> readiness
wait -> send_input -> completion poll -> get_output LAST -> delete). No latency
assertion (Q7=A): equivalence is proven functionally, not by timing.

Completion is a ``status_monitor.get_status`` poll (issue #409a: a post-input
IDLE is a valid completion signal alongside COMPLETED), NOT a second
``wait_until_status`` call — the recorder captures that poll as ``"poll"``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.agent_step import run_agent_step
from cli_agent_orchestrator.services.terminal_service import OutputMode
from cli_agent_orchestrator.utils import orchestration
from cli_agent_orchestrator.utils.orchestration import HandoffContext

_STEP = "cli_agent_orchestrator.services.agent_step"


class _SequenceRecorder:
    """Records the ordered terminal-layer calls run_agent_step makes."""

    def __init__(self):
        self.calls = []

    def install(self):
        tid = "abc12345"

        async def _create(*a, **k):
            self.calls.append(("create", a, k))
            t = MagicMock()
            t.id = tid
            return t

        def _send(terminal_id, prompt, *a, **k):
            self.calls.append(("send_input", terminal_id, prompt))
            return True

        def _get_output(terminal_id, mode):
            self.calls.append(("get_output", terminal_id, mode))
            return "the worker output"

        def _delete(terminal_id, *a, **k):
            self.calls.append(("delete", terminal_id))
            return True

        async def _wait(terminal_id, target, **k):
            self.calls.append(("wait", terminal_id, target))
            return True

        def _poll(terminal_id):
            # The post-input completion poll (issue #409a). Returns COMPLETED on
            # the first read so the wait settles immediately.
            self.calls.append(("poll", terminal_id))
            return TerminalStatus.COMPLETED

        return [
            patch(f"{_STEP}.terminal_service.create_terminal", new=AsyncMock(side_effect=_create)),
            patch(f"{_STEP}.terminal_service.send_input", side_effect=_send),
            patch(f"{_STEP}.terminal_service.get_output", side_effect=_get_output),
            patch(f"{_STEP}.terminal_service.delete_terminal", side_effect=_delete),
            patch(f"{_STEP}.wait_until_status", new=AsyncMock(side_effect=_wait)),
            patch(f"{_STEP}.status_monitor.get_status", side_effect=_poll),
        ]

    def kinds(self):
        """The ordered sequence of call kinds (ignoring exact args)."""
        return [c[0] for c in self.calls]

    def send_prompts(self):
        """The ordered prompts passed to send_input (the step's actual input)."""
        return [c[2] for c in self.calls if c[0] == "send_input"]


def _run_recorded(coro_factory):
    rec = _SequenceRecorder()
    patches = rec.install()
    for p in patches:
        p.start()
    try:
        asyncio.run(coro_factory())
    finally:
        for p in reversed(patches):
            p.stop()
    return rec


class TestEngineHandoffEquivalence:
    def test_engine_path_step_sequence(self):
        rec = _run_recorded(lambda: run_agent_step("kiro_cli", "developer", "do the task"))
        assert rec.kinds() == [
            "create",
            "wait",  # readiness
            "send_input",
            "poll",  # completion (status_monitor.get_status; issue #409a)
            "get_output",
            "delete",
        ]
        # Extraction is in LAST mode (provider extract_last_message path).
        assert ("get_output", "abc12345", OutputMode.LAST) in rec.calls

    def test_handoff_path_drives_identical_sequence(self):
        """_handoff_impl over the endpoint must converge on the SAME substrate
        sequence as the engine's direct call (BR-8)."""
        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry
        from cli_agent_orchestrator.utils.orchestration import _handoff_impl

        app.state.plugin_registry = PluginRegistry()
        test_client = TestClient(app, headers={"Host": "localhost"})

        def _handoff_via_endpoint():
            # Route the MCP client's requests.post at /terminals/run-step into
            # the in-process TestClient (the single combined HTTP seam).
            def fake_post(url, json=None, timeout=None, **kwargs):
                assert url.endswith("/terminals/run-step")
                return test_client.post("/terminals/run-step", json=json)

            with patch(
                "cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value=""
            ):
                with patch(
                    "cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider",
                    return_value=HandoffContext("kiro_cli", None, None, None),
                ):
                    with patch(
                        "cli_agent_orchestrator.utils.orchestration.requests"
                    ) as mock_requests:
                        mock_requests.post.side_effect = fake_post
                        mock_requests.Timeout = Exception
                        return asyncio.run(_handoff_impl("developer", "do the task"))

        rec = _SequenceRecorder()
        patches = rec.install()
        for p in patches:
            p.start()
        try:
            result = _handoff_via_endpoint()
        finally:
            for p in reversed(patches):
                p.stop()

        assert result.success is True
        assert result.output == "the worker output"
        assert rec.kinds() == [
            "create",
            "wait",
            "send_input",
            "poll",
            "get_output",
            "delete",
        ]

    def test_both_paths_match(self):
        """Direct comparison: engine and handoff sequences are identical."""
        engine = _run_recorded(lambda: run_agent_step("kiro_cli", "developer", "do the task"))

        from fastapi.testclient import TestClient

        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.plugins import PluginRegistry
        from cli_agent_orchestrator.utils.orchestration import _handoff_impl

        app.state.plugin_registry = PluginRegistry()
        test_client = TestClient(app, headers={"Host": "localhost"})

        handoff = _SequenceRecorder()
        patches = handoff.install()
        for p in patches:
            p.start()
        try:

            def fake_post(url, json=None, timeout=None, **kwargs):
                return test_client.post("/terminals/run-step", json=json)

            with patch(
                "cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value=""
            ):
                with patch(
                    "cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider",
                    return_value=HandoffContext("kiro_cli", None, None, None),
                ):
                    with patch(
                        "cli_agent_orchestrator.utils.orchestration.requests"
                    ) as mock_requests:
                        mock_requests.post.side_effect = fake_post
                        mock_requests.Timeout = Exception
                        asyncio.run(_handoff_impl("developer", "do the task"))
        finally:
            for p in reversed(patches):
                p.stop()

        assert engine.kinds() == handoff.kinds()
        # Beyond call *kinds* (true by construction since both call the same
        # run_agent_step), assert the actual ARGS match: the prompt sent to the
        # worker is identical across paths. Identical kinds alone could not catch
        # a divergent prompt/timeout — this does.
        assert engine.send_prompts() == handoff.send_prompts() == ["do the task"]


# --- Issue #1203: the handoff result states whether the worker was torn down. ---


def _run_step_response(status_code, body):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


_TEARDOWN_PAYLOAD = {"agent": "dev", "prompt": "x", "job_id": "j1"}


class TestHandoffTeardownReporting:
    """A success says whether the worker was torn down. A timeout or worker error
    names the terminal and job, says it was not torn down, and tells the caller
    nothing about deleting it or whether it still runs."""

    @pytest.mark.parametrize("body", [{"torn_down": True}, {"torn_down": False}, {}])
    def test_success_states_torn_down_only_when_the_server_said_so(self, body):
        response = _run_step_response(200, {"terminal_id": "t1", "last_message": "ok", **body})
        with (
            patch.object(orchestration, "_get_cleanup_nudge", return_value=""),
            patch.object(orchestration.requests, "post", return_value=response),
        ):
            res = asyncio.run(
                orchestration._run_step_and_build_result(
                    _TEARDOWN_PAYLOAD, "dev", "kiro_cli", 600, 0.0
                )
            )
        assert res.success is True
        if body.get("torn_down") is True:
            assert "Worker terminal t1 was torn down." in res.message
        else:
            # Explicit False and a missing field (older server) both read as not torn down.
            assert "Worker terminal t1 was not torn down." in res.message
            assert "was torn down" not in res.message

    def test_timeout_names_terminal_and_job_and_says_not_torn_down(self):
        detail = {"message": "step did not complete", "kind": "timeout", "terminal_id": "t1"}
        with patch.object(
            orchestration.requests, "post", return_value=_run_step_response(504, {"detail": detail})
        ):
            res = asyncio.run(
                orchestration._run_step_and_build_result(
                    _TEARDOWN_PAYLOAD, "dev", "kiro_cli", 600, 0.0
                )
            )
        assert res.success is False
        assert res.terminal_id == "t1"
        assert res.job_id == "j1"
        assert res.pending is None
        assert res.message == (
            "Handoff timed out after 600 seconds (job j1). Worker terminal t1 was not torn down."
        )

    def test_worker_error_names_terminal_and_job_and_says_not_torn_down(self):
        detail = {
            "message": "terminal t1 reached ERROR status",
            "kind": "error",
            "terminal_id": "t1",
        }
        with patch.object(
            orchestration.requests, "post", return_value=_run_step_response(502, {"detail": detail})
        ):
            res = asyncio.run(
                orchestration._run_step_and_build_result(
                    _TEARDOWN_PAYLOAD, "dev", "kiro_cli", 600, 0.0
                )
            )
        assert res.success is False
        assert res.terminal_id == "t1"
        assert res.job_id == "j1"
        assert res.pending is None
        assert res.message == (
            "Handoff failed: worker errored (terminal t1 reached ERROR status) (job j1). "
            "Worker terminal t1 was not torn down."
        )

    @pytest.mark.parametrize(
        "status_code,kind",
        [(504, "timeout"), (502, "error")],
    )
    def test_failure_message_gives_no_delete_instruction_and_no_liveness_claim(
        self, status_code, kind
    ):
        detail = {"message": "boom", "kind": kind, "terminal_id": "t1"}
        with patch.object(
            orchestration.requests,
            "post",
            return_value=_run_step_response(status_code, {"detail": detail}),
        ):
            res = asyncio.run(
                orchestration._run_step_and_build_result(
                    _TEARDOWN_PAYLOAD, "dev", "kiro_cli", 600, 0.0
                )
            )
        assert "delete_terminal" not in res.message
        assert "still running" not in res.message
        assert "is running" not in res.message

    @pytest.mark.parametrize("torn_down", [True, False])
    def test_cleanup_nudge_is_shown_only_when_the_worker_was_not_torn_down(self, torn_down):
        response = _run_step_response(
            200, {"terminal_id": "t1", "last_message": "ok", "torn_down": torn_down}
        )
        with (
            patch.object(orchestration, "_get_cleanup_nudge", return_value=" NOTE: nudge"),
            patch.object(orchestration.requests, "post", return_value=response),
        ):
            res = asyncio.run(
                orchestration._run_step_and_build_result(
                    _TEARDOWN_PAYLOAD, "dev", "kiro_cli", 600, 0.0
                )
            )
        assert ("NOTE: nudge" in res.message) is (not torn_down)


class TestHandoffEarlyPathReportsRealDelete:
    """The early-terminal-id path deletes the worker itself after success, so its
    message must reflect that delete, not the server's "not torn down"."""

    def _run(self, delete_result):
        with (
            patch.object(orchestration, "_get_cleanup_nudge", return_value=" NOTE: nudge"),
            patch.object(orchestration, "_resolve_handoff_provider") as mock_provider,
            patch.object(orchestration, "_create_terminal") as mock_create,
            patch.object(orchestration, "synchronous_create_timeout", return_value=(5.0, 430.0)),
            patch.object(
                orchestration, "_delete_terminal_impl", return_value=delete_result
            ) as mock_delete,
            patch.object(orchestration.requests, "post") as mock_post,
        ):
            mock_provider.return_value = HandoffContext(
                provider="kiro_cli", session_name=None, caller_id=None, allowed_tools=None
            )
            mock_create.return_value = ("dev-t1", "kiro_cli")
            mock_post.return_value = _run_step_response(
                200, {"terminal_id": "dev-t1", "last_message": "ok"}
            )
            res = asyncio.run(
                orchestration._handoff_impl("developer", "Do task", on_terminal_id=lambda _: None)
            )
        mock_delete.assert_called_once()
        return res

    def test_successful_delete_reports_torn_down_and_no_nudge(self):
        res = self._run({"success": True, "message": "deleted"})
        assert res.success is True
        assert "Worker terminal dev-t1 was torn down." in res.message
        assert "not torn down" not in res.message
        assert "NOTE: nudge" not in res.message

    def test_failed_delete_reports_not_torn_down_and_keeps_nudge(self):
        res = self._run({"success": False, "message": "boom"})
        assert res.success is True
        assert "Worker terminal dev-t1 was not torn down." in res.message
        assert "was torn down" not in res.message
        assert "NOTE: nudge" in res.message
