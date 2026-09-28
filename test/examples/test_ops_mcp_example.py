"""Protocol-level tests for the ops-mcp example.

These run in CI without provider credentials and without a running cao-server:
``run_lifecycle`` only needs an object with ``call_tool``, so a scriptable double
stands in for a live MCP session. A live-provider run is gated separately.

Two classes carry regressions for review findings on PR #647:
``TestTurnEvidence`` (a ready status that predates the dispatched turn must not
be accepted) and ``TestVerifiedCleanup`` (an unverified cleanup must fail).
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import pytest
from mcp.types import CallToolResult, TextContent

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "ops-mcp"
RUN_PY = EXAMPLE_DIR / "run.py"


def _mentions_terminal_id(node: ast.AST) -> bool:
    """Whether any literal inside this node is the CAO terminal-id name."""
    return any(
        isinstance(child, ast.Constant) and child.value == "CAO_TERMINAL_ID"
        for child in ast.walk(node)
    )


def _load_run_module() -> Any:
    """Import examples/ops-mcp/run.py by path (examples/ is not a package)."""
    spec = importlib.util.spec_from_file_location("ops_mcp_example_run", RUN_PY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


run = _load_run_module()


def _text_result(text: str, *, is_error: bool = False) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=is_error)


def _step(values: Sequence[Any], nth: int) -> Any:
    """Value for the nth (1-based) call; the last value repeats forever."""
    return values[min(nth, len(values)) - 1]


class FakeOps:
    """Scriptable stand-in for an initialized MCP ClientSession.

    ``statuses`` and ``output_sizes`` are consumed one entry per matching call,
    with the final entry repeating, which is what lets a test express "the
    terminal reported the previous turn's state twice before this turn started".
    """

    def __init__(
        self,
        *,
        profiles: Sequence[str] = ("ops_mcp_worker",),
        statuses: Sequence[str] = ("processing", "completed"),
        output_sizes: Sequence[int] = (0,),
        last_output: str = "done",
        launch_session_name: Optional[str] = "cao-demo",
        launch_terminal_id: Optional[str] = "abc123",
        send_succeeds: bool = True,
        read_succeeds: bool = True,
        shutdown_succeeds: bool = True,
        session_absent: bool = True,
        session_info_failure_message: Optional[str] = None,
    ) -> None:
        self._profiles = profiles
        self._statuses = statuses
        self._output_sizes = output_sizes
        self._last_output = last_output
        self._launch_session_name = launch_session_name
        self._launch_terminal_id = launch_terminal_id
        self._send_succeeds = send_succeeds
        self._read_succeeds = read_succeeds
        self._shutdown_succeeds = shutdown_succeeds
        self._session_absent = session_absent
        self._session_info_failure_message = session_info_failure_message
        self.calls: List[Dict[str, Any]] = []

    def _nth(self, name: str) -> int:
        return len([c for c in self.calls if c["name"] == name])

    async def call_tool(self, *, name: str, arguments: Dict[str, Any]) -> CallToolResult:
        self.calls.append({"name": name, "arguments": arguments})

        if name == "list_profiles":
            entries = ", ".join('{"name": "%s"}' % p for p in self._profiles)
            return _text_result('{"success": true, "profiles": [%s]}' % entries)

        if name == "launch_session":
            return _text_result(
                '{"success": true, "session_name": %s, "terminal_id": %s}'
                % (
                    json_or_null(self._launch_session_name),
                    json_or_null(self._launch_terminal_id),
                )
            )

        if name == "get_terminal_status":
            return _text_result('{"status": "%s"}' % _step(self._statuses, self._nth(name)))

        if name == "read_session_output":
            if not self._read_succeeds:
                return _text_result('{"success": false, "message": "extraction failed"}')
            if arguments.get("mode") == "last":
                return _text_result(
                    '{"success": true, "output": "%s", "truncated": false}' % self._last_output
                )
            size = _step(self._output_sizes, self._nth(name))
            return _text_result('{"success": true, "output": "", "total_chars": %d}' % size)

        if name == "send_session_message":
            if not self._send_succeeds:
                return _text_result('{"success": false, "message": "terminal is busy"}')
            return _text_result('{"success": true, "terminal_id": "abc123"}')

        if name == "shutdown_session":
            if not self._shutdown_succeeds:
                return _text_result('{"success": false, "message": "shutdown failed"}')
            return _text_result('{"success": true}')

        if name == "get_session_info":
            if self._session_info_failure_message is not None:
                return _text_result(
                    '{"success": false, "message": %s}'
                    % json_or_null(self._session_info_failure_message)
                )
            if self._session_absent:
                return _text_result(
                    '{"success": false, "message": '
                    "\"Get session info for 'cao-demo' failed: Session 'cao-demo' not found\"}"
                )
            return _text_result('{"name": "cao-demo", "terminals": [{"id": "abc123"}]}')

        raise AssertionError(f"unexpected tool call: {name}")

    @property
    def call_names(self) -> List[str]:
        return [call["name"] for call in self.calls]

    def count(self, name: str) -> int:
        return self._nth(name)


def json_or_null(value: Optional[str]) -> str:
    return "null" if value is None else '"%s"' % value


async def _no_sleep(_seconds: float) -> None:
    return None


class TestParseToolResult:
    def test_structured_content_wins_when_present(self) -> None:
        result = CallToolResult(
            content=[TextContent(type="text", text='{"ignored": true}')],
            structuredContent={"chosen": True},
        )
        assert run.parse_tool_result(result) == {"chosen": True}

    def test_json_text_is_decoded(self) -> None:
        assert run.parse_tool_result(_text_result('{"a": 1}')) == {"a": 1}

    def test_non_json_text_is_returned_verbatim(self) -> None:
        assert run.parse_tool_result(_text_result("not json")) == "not json"

    def test_error_result_becomes_a_failure_dict(self) -> None:
        assert run.parse_tool_result(_text_result("boom", is_error=True)) == {
            "success": False,
            "message": "boom",
        }

    def test_error_result_with_no_text_still_reports_failure(self) -> None:
        payload = run.parse_tool_result(CallToolResult(content=[], isError=True))
        assert payload["success"] is False
        assert payload["message"]


class TestTurnEvidence:
    """Regression: a ready status that predates the dispatched turn is not the turn.

    Review finding on PR #647 -- ``idle`` can precede deferred delivery, and
    ``completed`` can belong to the previous turn, so the first ready enum
    observed is not proof that the dispatched message was handled.
    """

    @pytest.mark.asyncio
    async def test_idle_before_delivery_is_not_accepted_at_launch(self) -> None:
        ops = FakeOps(statuses=("idle", "idle", "processing", "idle"))
        status = await run.wait_for_turn(
            ops,
            "abc123",
            baseline_status="unknown",
            baseline_output_size=None,
            sleep=_no_sleep,
        )
        assert status == "idle"
        # It must have kept polling past the two pre-delivery idles.
        assert ops.count("get_terminal_status") == 4

    @pytest.mark.asyncio
    async def test_completed_at_launch_is_accepted_without_activity(self) -> None:
        """Reaching completed requires dispatched input, so it cannot predate turn one."""
        ops = FakeOps(statuses=("completed",))
        status = await run.wait_for_turn(
            ops,
            "abc123",
            baseline_status="unknown",
            baseline_output_size=None,
            sleep=_no_sleep,
        )
        assert status == "completed"
        assert ops.count("get_terminal_status") == 1

    @pytest.mark.asyncio
    async def test_previous_turns_completed_is_rejected_on_a_follow_up(self) -> None:
        """The reported reproduction: a stale completed must not end the wait."""
        ops = FakeOps(
            statuses=("completed", "completed", "processing", "completed"),
            output_sizes=(100,),  # no growth: the agent produced nothing new yet
        )
        status = await run.wait_for_turn(
            ops,
            "abc123",
            baseline_status="completed",
            baseline_output_size=100,
            sleep=_no_sleep,
        )
        assert status == "completed"
        assert ops.count("get_terminal_status") == 4

    @pytest.mark.asyncio
    async def test_output_growth_is_accepted_as_turn_evidence(self) -> None:
        ops = FakeOps(statuses=("completed",), output_sizes=(150,))
        status = await run.wait_for_turn(
            ops,
            "abc123",
            baseline_status="completed",
            baseline_output_size=100,
            sleep=_no_sleep,
        )
        assert status == "completed"
        assert ops.count("get_terminal_status") == 1

    @pytest.mark.asyncio
    async def test_no_evidence_ever_arrives_raises_timeout(self) -> None:
        ops = FakeOps(statuses=("completed",), output_sizes=(100,))
        clock = iter([0.0, 1.0, 2.0, 99.0])
        with pytest.raises(TimeoutError, match="no evidence the dispatched turn ran"):
            await run.wait_for_turn(
                ops,
                "abc123",
                baseline_status="completed",
                baseline_output_size=100,
                timeout=10.0,
                sleep=_no_sleep,
                now=lambda: next(clock),
            )

    @pytest.mark.asyncio
    async def test_error_status_raises_instead_of_waiting_out_the_timeout(self) -> None:
        ops = FakeOps(statuses=("error",))
        with pytest.raises(RuntimeError, match="reached status 'error'"):
            await run.wait_for_turn(
                ops,
                "abc123",
                baseline_status="unknown",
                baseline_output_size=None,
                sleep=_no_sleep,
            )

    @pytest.mark.asyncio
    async def test_waiting_user_answer_counts_as_activity(self) -> None:
        ops = FakeOps(statuses=("waiting_user_answer", "idle"))
        status = await run.wait_for_turn(
            ops,
            "abc123",
            baseline_status="unknown",
            baseline_output_size=None,
            sleep=_no_sleep,
        )
        assert status == "idle"

    @pytest.mark.asyncio
    async def test_failed_status_call_raises(self) -> None:
        class Broken(FakeOps):
            async def call_tool(self, *, name: str, arguments: Dict[str, Any]) -> CallToolResult:
                self.calls.append({"name": name, "arguments": arguments})
                return _text_result('{"success": false, "message": "no such terminal"}')

        with pytest.raises(RuntimeError, match="no such terminal"):
            await run.wait_for_turn(
                Broken(),
                "abc123",
                baseline_status="unknown",
                baseline_output_size=None,
                sleep=_no_sleep,
            )


class TestVerifiedCleanup:
    """Regression: cleanup must be verified, not assumed.

    Review finding on PR #647 -- recording ``shutdown_ok = false`` and returning
    normally let automation see success while a session stayed alive.
    """

    @pytest.mark.asyncio
    async def test_unverified_cleanup_fails_the_run(self) -> None:
        ops = FakeOps(shutdown_succeeds=True, session_absent=False)
        with pytest.raises(RuntimeError, match="cleanup not verified"):
            await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)

    @pytest.mark.asyncio
    async def test_failed_shutdown_with_absent_session_counts_as_verified(self) -> None:
        """An already-absent session is the goal state, however it got there."""
        ops = FakeOps(shutdown_succeeds=False, session_absent=True)
        report = await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)
        assert report["shutdown_reported"] is False
        assert report["cleanup_verified"] is True

    @pytest.mark.asyncio
    async def test_cleanup_check_does_not_mask_the_original_failure(self) -> None:
        ops = FakeOps(read_succeeds=False, session_absent=False)
        with pytest.raises(RuntimeError, match="read output failed"):
            await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)
        assert ops.count("shutdown_session") == 1

    @pytest.mark.asyncio
    async def test_cleanup_is_verified_after_a_successful_run(self) -> None:
        ops = FakeOps()
        report = await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)
        assert report["cleanup_verified"] is True
        assert ops.call_names[-1] == "get_session_info"

    @pytest.mark.asyncio
    async def test_failed_cleanup_lookup_is_not_accepted_as_absence(self) -> None:
        ops = FakeOps(
            session_info_failure_message="Get session info for 'cao-demo' failed: HTTP 503"
        )
        with pytest.raises(RuntimeError, match="cleanup verification lookup failed: .*HTTP 503"):
            await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)


class TestRunLifecycle:
    @pytest.mark.asyncio
    async def test_full_lifecycle_calls_tools_in_order(self) -> None:
        ops = FakeOps()
        report = await run.run_lifecycle(
            ops, profile="ops_mcp_worker", task="do the thing", sleep=_no_sleep
        )
        assert ops.call_names == [
            "list_profiles",
            "launch_session",
            "get_terminal_status",
            "get_terminal_status",
            "read_session_output",
            "shutdown_session",
            "get_session_info",
        ]
        assert report["session_name"] == "cao-demo"
        assert report["terminal_id"] == "abc123"
        assert report["output"] == "done"

    @pytest.mark.asyncio
    async def test_launch_passes_task_and_working_directory(self) -> None:
        ops = FakeOps()
        await run.run_lifecycle(
            ops,
            profile="ops_mcp_worker",
            task="do the thing",
            working_directory="/tmp/demo",
            provider="mock_cli",
            sleep=_no_sleep,
        )
        launch = next(c for c in ops.calls if c["name"] == "launch_session")
        assert launch["arguments"]["initial_message"] == "do the thing"
        assert launch["arguments"]["working_directory"] == "/tmp/demo"
        assert launch["arguments"]["provider"] == "mock_cli"
        assert launch["arguments"]["agent_profile"] == "ops_mcp_worker"

    @pytest.mark.asyncio
    async def test_follow_up_captures_current_output_before_sending(self) -> None:
        ops = FakeOps(statuses=("processing", "completed", "processing", "completed"))
        await run.run_lifecycle(
            ops,
            profile="ops_mcp_worker",
            task="first",
            follow_up="second",
            sleep=_no_sleep,
        )
        names = ops.call_names
        # The pre-dispatch output snapshot is recorded before delivery.
        snapshot_read = names.index("read_session_output")
        assert snapshot_read < names.index("send_session_message")

    @pytest.mark.asyncio
    async def test_follow_up_waits_past_stale_completed_status(self) -> None:
        ops = FakeOps(
            statuses=("completed", "completed", "processing", "completed"),
            output_sizes=(200,),
        )
        report = await run.run_lifecycle(
            ops,
            profile="ops_mcp_worker",
            task="first",
            follow_up="second",
            sleep=_no_sleep,
        )
        send_step = next(step for step in report["steps"] if step["step"] == "send_session_message")
        assert ops.count("get_terminal_status") == 4
        assert send_step["detail"]["baseline_chars"] == 200

    @pytest.mark.asyncio
    async def test_failed_follow_up_send_is_reported(self) -> None:
        ops = FakeOps(send_succeeds=False)
        with pytest.raises(RuntimeError, match="terminal is busy"):
            await run.run_lifecycle(
                ops,
                profile="ops_mcp_worker",
                task="first",
                follow_up="second",
                sleep=_no_sleep,
            )
        assert ops.count("shutdown_session") == 1

    @pytest.mark.asyncio
    async def test_uninstalled_profile_fails_before_launching_anything(self) -> None:
        ops = FakeOps(profiles=("something_else",))
        with pytest.raises(RuntimeError, match="cao install examples/ops-mcp"):
            await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)
        assert ops.call_names == ["list_profiles"]

    @pytest.mark.asyncio
    async def test_launch_without_identity_is_reported_and_nothing_is_shut_down(self) -> None:
        ops = FakeOps(launch_session_name=None, launch_terminal_id=None)
        with pytest.raises(RuntimeError, match="no session identity"):
            await run.run_lifecycle(ops, profile="ops_mcp_worker", task="x", sleep=_no_sleep)
        assert "shutdown_session" not in ops.call_names


class TestExternalOperatorContract:
    def test_example_never_reads_cao_terminal_id(self) -> None:
        """#592: the caller must not depend on CAO terminal context.

        Asserted against the AST rather than the text, so prose mentioning the
        variable cannot fail the test and a real read cannot hide in a comment.
        """
        tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
        literals = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "CAO_TERMINAL_ID"
        ]
        assert len(literals) == 1, "expected exactly one mention, in the advisory check"

        # A membership test is fine; fetching the value is not.
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                assert not _mentions_terminal_id(node), "must not index os.environ for it"
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in {"getenv", "get"}:
                    assert not _mentions_terminal_id(node), "must not fetch it from the environment"

    def test_example_declares_the_tools_it_depends_on(self) -> None:
        assert set(run.LIFECYCLE_TOOLS) == {
            "list_profiles",
            "launch_session",
            "get_terminal_status",
            "send_session_message",
            "read_session_output",
            "get_session_info",
            "shutdown_session",
        }

    def test_fixture_profile_name_is_prefixed_to_avoid_collisions(self) -> None:
        profile = EXAMPLE_DIR / "ops_mcp_worker.md"
        assert profile.is_file()
        assert "name: ops_mcp_worker" in profile.read_text(encoding="utf-8")
