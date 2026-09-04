"""Tests for MCP server handoff logic.

Single-seam refactor (issue #312, N0): ``_handoff_impl`` was rewritten from a
six-call client-side loop into ONE call to ``POST /terminals/run-step``. These
tests preserve every OBSERVABLE behavior of the old suite (BR-8) — codex banner
content, no-banner for other providers, supervisor id from env, codex fast-fail
when CAO_TERMINAL_ID is unset, terminal_id surfacing, success on completion —
but assert them against the new single-call design rather than the old internal
mocks. (BR-8 explicitly makes observable behavior, not caller code, the
contract; the caller is deliberately rewritten.)
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.orchestration import (
    _HANDOFF_CREATE_TIMEOUT_S,
    HandoffContext,
    _handoff_impl,
    _shape_handoff_message,
)


def _ctx(provider, session_name=None, caller_id=None, allowed_tools=None):
    """Build a HandoffContext for mocking _resolve_handoff_provider."""
    return HandoffContext(
        provider=provider,
        session_name=session_name,
        caller_id=caller_id,
        allowed_tools=allowed_tools,
    )


def _ok_run_step_response(terminal_id="dev-term", last_message="task done"):
    """Build a mocked 200 response from POST /terminals/run-step."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "terminal_id": terminal_id,
        "last_message": last_message,
        "status": "completed",
    }
    resp.raise_for_status.return_value = None
    return resp


class TestShapeHandoffMessage:
    """The codex prompt-shaping that stays caller-side (was _send_direct_input_handoff)."""

    def test_codex_prepends_banner_with_supervisor_id(self):
        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            shaped = _shape_handoff_message("codex", "Implement hello world")
        assert shaped.startswith("[CAO Handoff]")
        assert "a1b2c3d4" in shaped
        assert "Implement hello world" in shaped
        assert "Do NOT use send_message" in shaped
        # Original message must appear in full AFTER the banner.
        assert shaped.endswith("Implement hello world")

    def test_non_codex_message_unchanged(self):
        for provider in ("claude_code", "kiro_cli"):
            assert _shape_handoff_message(provider, "Implement hello world") == (
                "Implement hello world"
            )

    def test_codex_no_env_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="CAO_TERMINAL_ID not set"):
                _shape_handoff_message("codex", "Do task")


class TestHandoffMessageContext:
    """Handoff sends the shaped prompt to the run-step endpoint."""

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_codex_provider_sends_banner_to_endpoint(self, mock_provider, _nudge):
        """Codex handoff posts the [CAO Handoff] banner as the prompt."""
        mock_provider.return_value = _ctx("codex")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "a1b2c3d4"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                mock_requests.post.return_value = _ok_run_step_response()
                mock_requests.Timeout = Exception

                result = asyncio.run(_handoff_impl("developer", "Implement hello world"))

        assert result.success is True
        # Exactly one combined call replaces the former six round-trips.
        mock_requests.post.assert_called_once()
        url = mock_requests.post.call_args[0][0]
        assert url.endswith("/terminals/run-step")
        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt.startswith("[CAO Handoff]")
        assert "a1b2c3d4" in sent_prompt
        assert "Implement hello world" in sent_prompt
        assert "Do NOT use send_message" in sent_prompt

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_claude_code_provider_no_banner(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("claude_code")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception

            result = asyncio.run(_handoff_impl("developer", "Implement hello world"))

        assert result.success is True
        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt == "Implement hello world"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_kiro_cli_provider_no_banner(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception

            result = asyncio.run(_handoff_impl("developer", "Implement hello world"))

        assert result.success is True
        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt == "Implement hello world"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_codex_banner_supervisor_id_from_env(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("codex")

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "c0ffee01"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                mock_requests.post.return_value = _ok_run_step_response()
                mock_requests.Timeout = Exception

                asyncio.run(_handoff_impl("developer", "Build feature X"))

        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert "c0ffee01" in sent_prompt
        assert "Build feature X" in sent_prompt

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_codex_fast_fail_when_no_env(self, mock_provider):
        """Codex handoff with no CAO_TERMINAL_ID fails visibly and never posts a
        step (issue #284) — never tell a worker its supervisor is 'unknown'."""
        mock_provider.return_value = _ctx("codex")

        with patch.dict(os.environ, {}, clear=True):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                mock_requests.Timeout = Exception
                result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "CAO_TERMINAL_ID not set" in result.message
        # Fast-fail: no step is run at all.
        mock_requests.post.assert_not_called()
        # No terminal was created, so none to surface.
        assert result.terminal_id is None

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_codex_original_message_preserved(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("codex")
        original = "Implement the task described in /path/to/task.md. Write tests."

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "deadbeef"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                mock_requests.post.return_value = _ok_run_step_response()
                mock_requests.Timeout = Exception
                asyncio.run(_handoff_impl("developer", original))

        sent_prompt = mock_requests.post.call_args[1]["json"]["prompt"]
        assert sent_prompt.endswith(original)

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_terminal_id_none_when_provider_resolution_fails(self, mock_provider):
        """When provider resolution fails (no terminal created), report none."""
        mock_provider.side_effect = Exception("session not found")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "Handoff failed" in result.message
        assert result.terminal_id is None
        mock_requests.post.assert_not_called()


class TestHandoffOutcomes:
    """Success/failure outcome semantics preserved through the single endpoint."""

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_success_returns_output_and_terminal_id(self, mock_provider, _nudge):
        """On success the worker output + terminal id are surfaced; the server
        owns teardown (the request asks for teardown=True)."""
        mock_provider.return_value = _ctx("kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(
                terminal_id="dev-t1", last_message="done"
            )
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is True
        assert result.output == "done"
        assert result.terminal_id == "dev-t1"
        # The single combined call requests server-side teardown.
        assert mock_requests.post.call_args[1]["json"]["teardown"] is True

    @patch("cli_agent_orchestrator.utils.orchestration.get_local_bearer", return_value="tok")
    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_attaches_bearer_when_auth_enabled(self, mock_provider, _nudge, _bearer):
        """Review on PR #634: the run-step POST carries the local bearer when configured."""
        mock_provider.return_value = _ctx("kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            asyncio.run(_handoff_impl("developer", "Do task"))

        assert mock_requests.post.call_args[1]["headers"] == {"Authorization": "Bearer tok"}

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_use_worktree_defaults_to_false_in_the_payload(self, mock_provider, _nudge):
        """issue #100 Phase 1: unconditionally present in the payload (unlike
        the Optional fields above) so the server always sees an explicit
        value, matching RunStepRequest's own unconditional default."""
        mock_provider.return_value = _ctx("kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            asyncio.run(_handoff_impl("developer", "Do task"))

        assert mock_requests.post.call_args[1]["json"]["use_worktree"] is False

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_use_worktree_true_reaches_the_payload(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            asyncio.run(_handoff_impl("developer", "Do task", use_worktree=True))

        assert mock_requests.post.call_args[1]["json"]["use_worktree"] is True

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_endpoint_504_maps_to_timeout_result(self, mock_provider):
        """A 504 (worker ran long) becomes a timeout failure and reads the live
        terminal id from the STRUCTURED detail field (not a regex scrape)."""
        mock_provider.return_value = _ctx("kiro_cli")

        timeout_resp = MagicMock()
        timeout_resp.status_code = 504
        timeout_resp.json.return_value = {
            "detail": {
                "message": "step on terminal a1b2c3d4 did not complete within 600s",
                "kind": "timeout",
                "terminal_id": "a1b2c3d4",
            }
        }
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = timeout_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", timeout=600))

        assert result.success is False
        assert "timed out after 600 seconds" in result.message
        assert result.terminal_id == "a1b2c3d4"

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_endpoint_502_maps_to_worker_errored_result(self, mock_provider):
        """A 502 (worker CRASHED) is reported as an error — NOT as a timeout —
        so a fast crash is not mislabeled as an N-second timeout."""
        mock_provider.return_value = _ctx("kiro_cli")

        crash_resp = MagicMock()
        crash_resp.status_code = 502
        crash_resp.json.return_value = {
            "detail": {
                "message": "terminal a1b2c3d4 reached ERROR status",
                "kind": "error",
                "terminal_id": "a1b2c3d4",
            }
        }
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = crash_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", timeout=600))

        assert result.success is False
        assert "worker errored" in result.message
        assert "timed out" not in result.message
        assert result.terminal_id == "a1b2c3d4"

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_legacy_string_detail_still_scrapes_terminal_id(self, mock_provider):
        """Backward-compat: an older server returning a plain-string detail still
        yields the terminal id via the regex fallback."""
        mock_provider.return_value = _ctx("kiro_cli")

        legacy_resp = MagicMock()
        legacy_resp.status_code = 504
        legacy_resp.json.return_value = {
            "detail": "step on terminal a1b2c3d4 did not complete within 600s"
        }
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = legacy_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", timeout=600))

        assert result.success is False
        assert result.terminal_id == "a1b2c3d4"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_malformed_200_surfaces_failure(self, mock_provider, _nudge):
        """A 200 with no last_message must be a failure, not a silent
        success-with-None."""
        mock_provider.return_value = _ctx("kiro_cli")

        bad_resp = MagicMock()
        bad_resp.status_code = 200
        bad_resp.json.return_value = {"terminal_id": "dev-t1"}  # no last_message
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = bad_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "malformed" in result.message
        assert result.terminal_id == "dev-t1"

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_endpoint_500_maps_to_failure_result(self, mock_provider):
        mock_provider.return_value = _ctx("kiro_cli")

        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.json.return_value = {"detail": "Failed to run step: boom"}
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = err_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is False
        assert "Handoff failed" in result.message
        assert "boom" in result.message


class TestHandoffContextPropagation:
    """Regression (PR #320): the run-step payload must carry the supervisor's
    session_name, caller_id and inherited allowed_tools so the worker is created
    in the SAME tmux session with #284 callback routing + tool inheritance — the
    observable behavior the old six-call _create_terminal path provided."""

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_supervisor_context_in_payload(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx(
            "kiro_cli",
            session_name="cao-a1b2c3d4",
            caller_id="sup-abc",
            allowed_tools=["fs_read", "fs_write"],
        )

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is True
        payload = mock_requests.post.call_args[1]["json"]
        assert payload["session_name"] == "cao-a1b2c3d4"
        assert payload["caller_id"] == "sup-abc"
        assert payload["allowed_tools"] == ["fs_read", "fs_write"]

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_no_supervisor_omits_session_and_caller(self, mock_provider, _nudge):
        """Outside a CAO terminal there is no supervisor: the payload omits
        session_name/caller_id/allowed_tools so the server auto-creates a fresh
        session (new_session=True)."""
        mock_provider.return_value = _ctx("kiro_cli")  # all context None

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is True
        payload = mock_requests.post.call_args[1]["json"]
        assert "session_name" not in payload
        assert "caller_id" not in payload
        assert "allowed_tools" not in payload


class TestHandoffModelOverride:
    """handoff's own `model` parameter -- an explicit per-call model override
    for the worker, threaded through to the run-step payload."""

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_mcode_omits_kiro_engine_and_forwards_model(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("mcode")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            result = asyncio.run(
                _handoff_impl(
                    "report_generator",
                    "Create a report template",
                    engine="v2",
                    model="MiniMax-M2.1",
                )
            )

        assert result.success is True
        payload = mock_requests.post.call_args[1]["json"]
        assert "engine" not in payload
        assert payload["model"] == "MiniMax-M2.1"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_model_is_forwarded_in_payload(self, mock_provider, _nudge):
        mock_provider.return_value = _ctx("claude_code")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", model="fable-5"))

        assert result.success is True
        payload = mock_requests.post.call_args[1]["json"]
        assert payload["model"] == "fable-5"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    def test_omitted_model_is_absent_from_payload(self, mock_provider, _nudge):
        """No model given -> no 'model' key at all (not None), matching the
        existing convention for every other optional field on this payload
        (session_name/caller_id/allowed_tools/working_directory above)."""
        mock_provider.return_value = _ctx("claude_code")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response()
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task"))

        assert result.success is True
        payload = mock_requests.post.call_args[1]["json"]
        assert "model" not in payload


class TestResolveHandoffProvider:
    """_resolve_handoff_provider extracts the full supervisor context (not just
    the provider) from the supervisor terminal metadata."""

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_child_allowed_tools")
    @patch("cli_agent_orchestrator.utils.orchestration.resolve_provider")
    def test_inside_cao_terminal_extracts_context(self, mock_resolve, mock_child_tools):
        from cli_agent_orchestrator.utils.orchestration import _resolve_handoff_provider

        mock_resolve.return_value = "kiro_cli"
        mock_child_tools.return_value = "fs_read,fs_write"
        meta = MagicMock()
        meta.status_code = 200
        meta.json.return_value = {
            "provider": "kiro_cli",
            "session_name": "cao-sup",
            "allowed_tools": ["fs_read", "fs_write", "execute_bash"],
        }
        meta.raise_for_status.return_value = None

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "c0ffee01"}):
            with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
                mock_requests.get.return_value = meta
                ctx = _resolve_handoff_provider("developer")

        assert ctx.provider == "kiro_cli"
        assert ctx.session_name == "cao-sup"
        assert ctx.caller_id == "c0ffee01"
        assert ctx.allowed_tools == ["fs_read", "fs_write"]

    @patch("cli_agent_orchestrator.utils.orchestration.resolve_provider")
    def test_outside_cao_terminal_yields_empty_context(self, mock_resolve):
        from cli_agent_orchestrator.utils.orchestration import _resolve_handoff_provider

        mock_resolve.return_value = "kiro_cli"
        with patch.dict(os.environ, {}, clear=True):
            ctx = _resolve_handoff_provider("developer")

        assert ctx.provider == "kiro_cli"
        assert ctx.session_name is None
        assert ctx.caller_id is None
        assert ctx.allowed_tools is None


class TestHandoffEarlyTerminalId:
    """``on_terminal_id`` / ``wait`` -- review on PR #634 (issue #616 partial
    mitigation: recoverable terminal_id + a non-blocking mode, NOT server-side
    idempotency). Neither is passed by the MCP ``handoff`` tool -- every test
    above exercises the default (``on_terminal_id=None, wait=True``) path and
    passes unchanged, which is what proves this class's new branches are
    additive rather than a behavior change to the existing tool."""

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_on_terminal_id_called_before_run_step_completes(
        self, mock_create, mock_provider, _nudge
    ):
        """The callback fires with the REAL terminal_id before the (still
        blocking) run-step call -- an operator who kills here has an id to
        recover with, unlike the default path where it is only known at the
        very end."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")
        seen = []

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", on_terminal_id=seen.append))

        assert seen == ["dev-t1"]
        assert result.success is True
        assert result.terminal_id == "dev-t1"
        assert mock_create.call_args[1]["create_timeout"] == _HANDOFF_CREATE_TIMEOUT_S

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_on_terminal_id_exception_is_swallowed(self, mock_create, mock_provider, _nudge):
        """A broken callback (e.g. a stderr write that fails) must not break
        the handoff itself, mirroring run_agent_step's own on_terminal_created
        callback contract."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")

        def _boom(_tid):
            raise RuntimeError("stderr closed")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            result = asyncio.run(_handoff_impl("developer", "Do task", on_terminal_id=_boom))

        assert result.success is True
        assert result.terminal_id == "dev-t1"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_waiting_path_reuses_the_created_terminal_not_a_fresh_one(
        self, mock_create, mock_provider, _nudge
    ):
        """The run-step payload carries reuse_terminal_id, and omits every
        field run_agent_step documents as 'ignored when reusing' -- they were
        already applied at create time above."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            asyncio.run(_handoff_impl("developer", "Do task", on_terminal_id=lambda _: None))

        payload = mock_requests.post.call_args[1]["json"]
        assert payload["reuse_terminal_id"] == "dev-t1"
        # False is the honest value, not a leftover: run_agent_step ignores
        # `teardown` entirely once reuse_terminal_id is set (created_here is
        # always False for a reuse call), so the server cannot act on this
        # regardless -- see the dedicated teardown tests below for what
        # actually tears the terminal down (an explicit _delete_terminal_impl
        # call on success).
        assert payload["teardown"] is False
        for ignored_field in (
            "session_name",
            "caller_id",
            "allowed_tools",
            "working_directory",
            "use_worktree",
            "model",
        ):
            assert ignored_field not in payload

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    @patch("cli_agent_orchestrator.utils.orchestration._delete_terminal_impl")
    def test_waiting_path_tears_down_the_terminal_on_success(
        self, mock_delete, mock_create, mock_provider, _nudge
    ):
        """Regression (review on commit 3952889): run_agent_step
        silently never tears down a reused terminal, no matter what the
        payload's `teardown` field says (created_here is always False for a
        reuse call). Without this explicit call, every successful wait=True
        handoff leaked its worker terminal."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")
        mock_delete.return_value = {"success": True, "message": "Terminal dev-t1 deleted"}

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            result = asyncio.run(
                _handoff_impl("developer", "Do task", on_terminal_id=lambda _: None)
            )

        assert result.success is True
        mock_delete.assert_called_once_with("dev-t1")

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    @patch("cli_agent_orchestrator.utils.orchestration._delete_terminal_impl")
    def test_waiting_path_leaves_the_terminal_alive_on_failure(
        self, mock_delete, mock_create, mock_provider
    ):
        """A failed/timed-out wait must NOT tear the terminal down -- that
        would defeat the entire point of surfacing terminal_id early (the
        operator needs it alive to inspect/recover with status/result/
        cancel)."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")

        timeout_resp = MagicMock()
        timeout_resp.status_code = 504
        timeout_resp.json.return_value = {
            "detail": {
                "message": "step did not complete",
                "kind": "timeout",
                "terminal_id": "dev-t1",
            }
        }
        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = timeout_resp
            mock_requests.Timeout = Exception
            result = asyncio.run(
                _handoff_impl("developer", "Do task", timeout=60, on_terminal_id=lambda _: None)
            )

        assert result.success is False
        assert result.terminal_id == "dev-t1"
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    @patch("cli_agent_orchestrator.utils.orchestration._delete_terminal_impl")
    def test_waiting_path_teardown_failure_does_not_mask_success(
        self, mock_delete, mock_create, mock_provider, _nudge
    ):
        """Mirrors run_agent_step's own teardown philosophy ('never let
        cleanup mask a settled step'): a cleanup failure here must not turn
        an already-successful handoff into a reported failure."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")
        mock_delete.return_value = {"success": False, "message": "cleanup pending"}

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            result = asyncio.run(
                _handoff_impl("developer", "Do task", on_terminal_id=lambda _: None)
            )

        assert result.success is True
        mock_delete.assert_called_once_with("dev-t1")

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_waiting_path_still_forwards_engine_since_reuse_validates_it(
        self, mock_create, mock_provider, _nudge
    ):
        """Unlike the fields above, `engine` is NOT ignored on reuse --
        run_agent_step validates it against what got persisted at create
        time -- so it must still be forwarded here to match."""
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            asyncio.run(
                _handoff_impl("developer", "Do task", engine="v2", on_terminal_id=lambda _: None)
            )

        assert mock_requests.post.call_args[1]["json"]["engine"] == "v2"

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_waiting_path_omits_engine_for_a_non_kiro_provider(
        self, mock_create, mock_provider, _nudge
    ):
        """The sibling above pins the KIRO case; this pins the other side of it.

        `engine` is a Kiro-only concept, so #625 tightened every forwarding
        site to `provider == kiro_cli`. The reuse path is forwarded for the
        same reason it is forwarded at all -- run_agent_step validates it
        against what got PERSISTED -- and the create path only ever persists
        engine for kiro_cli, so sending it for another provider could only
        ever mismatch. Without this test the non-Kiro half of that guard is
        unpinned: dropping the provider check leaves the Kiro test above
        passing.
        """
        mock_provider.return_value = _ctx("mcode")
        mock_create.return_value = ("dev-t1", "mcode")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            asyncio.run(
                _handoff_impl("developer", "Do task", engine="v2", on_terminal_id=lambda _: None)
            )

        assert "engine" not in mock_requests.post.call_args[1]["json"]

    @patch("cli_agent_orchestrator.utils.orchestration._get_cleanup_nudge", return_value="")
    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_waiting_path_forwards_idempotency_key_to_create_terminal(
        self, mock_create, mock_provider, _nudge
    ):
        """A supplied key reaches _create_terminal, not just the run-step reuse
        payload (which run_agent_step would ignore anyway -- the key protects the
        CREATE step, not the reuse step).

        Review on PR #634. Drives `_handoff_impl` directly on purpose: the
        `--idempotency-key` CLI flag that used to reach this was removed, because
        keying creation alone cannot make a handoff retry safe. The parameter and
        this test are retained for #715, which adds the durable run record and
        re-exposes the key once a retry can resolve to an existing RUN.
        """
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            mock_requests.post.return_value = _ok_run_step_response(terminal_id="dev-t1")
            mock_requests.Timeout = Exception
            asyncio.run(
                _handoff_impl(
                    "developer",
                    "Do task",
                    on_terminal_id=lambda _: None,
                    idempotency_key="retry-1",
                )
            )

        assert mock_create.call_args[1]["idempotency_key"] == "retry-1"

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    @patch("cli_agent_orchestrator.utils.orchestration._send_direct_input_handoff")
    def test_no_wait_sends_input_and_returns_without_calling_run_step(
        self, mock_send, mock_create, mock_provider
    ):
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")

        with patch("cli_agent_orchestrator.utils.orchestration.requests") as mock_requests:
            result = asyncio.run(_handoff_impl("developer", "Do task", wait=False))

        mock_send.assert_called_once_with("dev-t1", "kiro_cli", "Do task")
        mock_requests.post.assert_not_called()
        assert result.success is True
        assert result.terminal_id == "dev-t1"
        assert result.output is None
        assert "not waiting" in result.message
        assert "cao agent status dev-t1" in result.message
        assert "cao agent result dev-t1" in result.message
        assert "cao agent cancel --delete dev-t1" in result.message

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    @patch("cli_agent_orchestrator.utils.orchestration._send_direct_input_handoff")
    def test_no_wait_still_fires_on_terminal_id(self, mock_send, mock_create, mock_provider):
        mock_provider.return_value = _ctx("kiro_cli")
        mock_create.return_value = ("dev-t1", "kiro_cli")
        seen = []

        asyncio.run(_handoff_impl("developer", "Do task", wait=False, on_terminal_id=seen.append))

        assert seen == ["dev-t1"]

    @patch("cli_agent_orchestrator.utils.orchestration._resolve_handoff_provider")
    @patch("cli_agent_orchestrator.utils.orchestration._create_terminal")
    def test_codex_fast_fail_applies_to_no_wait_too(self, mock_create, mock_provider):
        """The codex CAO_TERMINAL_ID guard runs before ANY path branches --
        including --no-wait, which must not create an orphan terminal either."""
        mock_provider.return_value = _ctx("codex")

        with patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(_handoff_impl("developer", "Do task", wait=False))

        assert result.success is False
        assert "CAO_TERMINAL_ID not set" in result.message
        mock_create.assert_not_called()
