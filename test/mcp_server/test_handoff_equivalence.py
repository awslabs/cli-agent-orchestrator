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

from cli_agent_orchestrator.mcp_server.server import HandoffContext
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.agent_step import run_agent_step
from cli_agent_orchestrator.services.terminal_service import OutputMode

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
        from cli_agent_orchestrator.mcp_server.server import _handoff_impl
        from cli_agent_orchestrator.plugins import PluginRegistry

        app.state.plugin_registry = PluginRegistry()
        test_client = TestClient(app, headers={"Host": "localhost"})

        def _handoff_via_endpoint():
            # Route the MCP client's requests.post at /terminals/run-step into
            # the in-process TestClient (the single combined HTTP seam).
            def fake_post(url, json=None, timeout=None, **kwargs):
                assert url.endswith("/terminals/run-step")
                return test_client.post("/terminals/run-step", json=json)

            with patch(
                "cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value=""
            ):
                with patch(
                    "cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider",
                    return_value=HandoffContext("kiro_cli", None, None, None),
                ):
                    with patch(
                        "cli_agent_orchestrator.mcp_server.server.requests"
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
        from cli_agent_orchestrator.mcp_server.server import _handoff_impl
        from cli_agent_orchestrator.plugins import PluginRegistry

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
                "cli_agent_orchestrator.mcp_server.server._get_cleanup_nudge", return_value=""
            ):
                with patch(
                    "cli_agent_orchestrator.mcp_server.server._resolve_handoff_provider",
                    return_value=HandoffContext("kiro_cli", None, None, None),
                ):
                    with patch(
                        "cli_agent_orchestrator.mcp_server.server.requests"
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
