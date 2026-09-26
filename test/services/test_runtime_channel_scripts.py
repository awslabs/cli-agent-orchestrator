"""Script execution over the runtime channel (#745, item 6).

Covers the bridge-side executor (real subprocess: exit code, stdout/stderr
capture, timeout, cancel) and the server-side remote-drive selection + outcome
mapping through the shared finalize path.
"""

import asyncio

import pytest

from cli_agent_orchestrator.runtime_channel.bridge import Bridge


@pytest.fixture()
def bridge():
    return Bridge("ws://unused", "worker-x", "tok")


class TestBridgeScriptExecutor:
    @pytest.mark.asyncio
    async def test_clean_exit_captures_stdout_and_rc0(self, bridge):
        result = await bridge._run_script(
            "op1",
            "import sys; print('hello-out'); sys.stderr.write('warn'); sys.exit(0)",
            {"PATH": __import__("os").environ.get("PATH", "")},
            timeout=30.0,
            term_grace=5.0,
        )
        assert result["returncode"] == 0
        assert "hello-out" in result["stdout"]
        assert "warn" in result["stderr"]
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_nonzero_exit_reported(self, bridge):
        result = await bridge._run_script(
            "op2", "import sys; sys.exit(3)", {"PATH": ""}, timeout=30.0, term_grace=5.0
        )
        assert result["returncode"] == 3
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_timeout_terminates_and_flags(self, bridge):
        result = await bridge._run_script(
            "op3",
            "import time; time.sleep(30)",
            {"PATH": ""},
            timeout=0.5,
            term_grace=2.0,
        )
        assert result["timed_out"] is True
        # The process must not still be tracked once _run_script returns.
        assert "op3" not in bridge._script_procs

    @pytest.mark.asyncio
    async def test_env_is_constructed_not_inherited(self, bridge):
        # Only the supplied env reaches the child — no ambient leakage.
        import os

        os.environ["CAO_LEAK_PROBE"] = "should-not-appear"
        try:
            result = await bridge._run_script(
                "op4",
                "import os; print(os.environ.get('CAO_LEAK_PROBE', 'ABSENT'))",
                {"PATH": os.environ.get("PATH", "")},
                timeout=30.0,
                term_grace=5.0,
            )
        finally:
            del os.environ["CAO_LEAK_PROBE"]
        assert "ABSENT" in result["stdout"]

    @pytest.mark.asyncio
    async def test_cancel_running_script(self, bridge):
        task = asyncio.ensure_future(
            bridge._run_script(
                "op5", "import time; time.sleep(30)", {"PATH": ""}, timeout=30.0, term_grace=2.0
            )
        )
        await asyncio.sleep(0.3)
        proc = bridge._script_procs.get("op5")
        assert proc is not None
        await bridge._terminate_process(proc, 2.0)
        result = await task
        assert result["returncode"] != 0  # terminated


class TestBridgeScriptModes:
    """Two kinds of user code reach this executor, and they are not interchangeable.

    A workflow script is Python the server runs under its own interpreter. A flow
    pre-script is an executable file whose shebang chooses one — `docs/flows.md`'s
    worked example is `#!/bin/bash`. Handing that to `sys.executable` produces a
    Python SyntaxError blamed on the user's health check, so the mode is carried in
    the protocol rather than sniffed from the body.
    """

    @pytest.mark.asyncio
    async def test_executable_mode_honours_the_shebang(self, bridge):
        import os

        result = await bridge._run_script(
            "op-mode-1",
            '#!/bin/sh\necho \'{"execute": true, "output": {}}\'\n',
            {"PATH": os.environ.get("PATH", "")},
            timeout=30.0,
            term_grace=5.0,
            mode="executable",
        )
        assert result["returncode"] == 0
        assert '"execute": true' in result["stdout"]

    @pytest.mark.asyncio
    async def test_a_shell_script_under_python_mode_would_fail(self, bridge):
        """The reason the mode exists, stated as a test rather than a comment."""
        import os

        result = await bridge._run_script(
            "op-mode-2",
            "#!/bin/sh\necho hi\n",
            {"PATH": os.environ.get("PATH", "")},
            timeout=30.0,
            term_grace=5.0,
        )
        assert result["returncode"] != 0

    @pytest.mark.asyncio
    async def test_default_mode_is_still_python(self, bridge):
        result = await bridge._run_script(
            "op-mode-3",
            "import sys; print(sys.version_info[0])",
            {"PATH": ""},
            timeout=30.0,
            term_grace=5.0,
        )
        assert result["returncode"] == 0
        assert result["stdout"].strip() == "3"

    @pytest.mark.asyncio
    async def test_an_unknown_mode_is_refused_not_guessed(self, bridge):
        result = await bridge._run_script(
            "op-mode-4", "print('x')", {"PATH": ""}, timeout=30.0, term_grace=5.0, mode="perl"
        )
        assert result["returncode"] is None
        assert "unsupported script mode" in result["stderr"]

    @pytest.mark.asyncio
    async def test_the_script_file_is_owner_only(self, bridge):
        """The body is the caller's code, in a pod that may host other work.

        The mode is read with ``ls -l`` rather than ``stat``: the two ``stat``
        implementations disagree about both flags and output. GNU's ``-f`` means
        "filesystem" and *succeeds*, printing something that is not a mode, so a
        BSD-first probe with an ``||`` fallback emits two lines on Linux and the
        assertion fails on a file that is correctly 0700 (review finding 9 on
        #802). POSIX fixes the first ten characters of ``ls -l``, and anything an
        implementation adds for xattrs or ACLs comes after them.
        """
        import os

        result = await bridge._run_script(
            "op-mode-5",
            '#!/bin/sh\nls -l "$0"\n',
            {"PATH": os.environ.get("PATH", "")},
            timeout=30.0,
            term_grace=5.0,
            mode="executable",
        )
        assert result["returncode"] == 0, result["stderr"]
        assert result["stdout"].strip()[:10] == "-rwx------", result["stdout"]


class TestServerRemoteDriveSelection:
    def test_no_runtime_env_is_local(self, monkeypatch):
        from cli_agent_orchestrator.services import script_runner

        monkeypatch.delenv("CAO_SCRIPT_RUNTIME", raising=False)
        assert script_runner._remote_script_runtime() is None

    def test_configured_but_disconnected_still_selects_the_runtime(self, monkeypatch):
        """A disconnect must not relocate user code into the server container.

        The earlier behaviour returned None here, so a runtime that was briefly
        down turned into a local spawn beside the central database — the exact
        placement ``CAO_SCRIPT_RUNTIME`` exists to prevent, and the opposite of
        what the design and the EKS runbook promise ("disconnect → explicit
        failure"). The remote path reports the disconnect as a failed run, which
        is retryable; a run that quietly succeeded in the wrong pod is not
        (Copilot review on #802, finding 7).
        """
        from cli_agent_orchestrator.services import script_runner

        monkeypatch.setenv("CAO_SCRIPT_RUNTIME", "worker-not-connected")
        assert script_runner._remote_script_runtime() == "worker-not-connected"

    def test_configured_and_connected_selects_runtime(self, monkeypatch):
        from cli_agent_orchestrator.runtime_channel.registry import runtime_registry
        from cli_agent_orchestrator.services import script_runner

        async def send_text(_):
            pass

        conn = runtime_registry.register("worker-live", send_text)
        try:
            monkeypatch.setenv("CAO_SCRIPT_RUNTIME", "worker-live")
            assert script_runner._remote_script_runtime() == "worker-live"
        finally:
            runtime_registry.unregister("worker-live", conn)

    def test_the_public_seams_delegate_rather_than_duplicate(self, monkeypatch):
        """`flow_service` asks the same question for a pre-script (#745).

        If the public wrapper read the env itself, patching one name would move
        workflow scripts and leave pre-scripts behind — two answers to "where does
        user code run" is the bug this seam prevents.
        """
        from cli_agent_orchestrator.services import script_runner

        monkeypatch.setattr(script_runner, "_remote_script_runtime", lambda: "worker-answer")
        assert script_runner.remote_script_runtime() == "worker-answer"
        monkeypatch.setattr(script_runner, "_script_callback_env", lambda env: {**env, "seen": "1"})
        assert script_runner.script_callback_env({})["seen"] == "1"

    def test_callback_env_rewrites_api_base_url(self, monkeypatch):
        from cli_agent_orchestrator.services import script_runner

        monkeypatch.setenv("CAO_ADVERTISED_URL", "http://cao-server:9889")
        out = script_runner._script_callback_env(
            {"CAO_API_BASE_URL": "http://127.0.0.1:9889", "PATH": "/usr/bin"}
        )
        assert out["CAO_API_BASE_URL"] == "http://cao-server:9889"
        assert out["PATH"] == "/usr/bin"

    def test_callback_env_untouched_without_advertised(self, monkeypatch):
        from cli_agent_orchestrator.services import script_runner

        monkeypatch.delenv("CAO_ADVERTISED_URL", raising=False)
        env = {"CAO_API_BASE_URL": "http://127.0.0.1:9889"}
        assert script_runner._script_callback_env(env) == env


class TestCancellationNamesTheRunningOperation:
    """One operation identity from the controller through to the subprocess.

    The driver minted a UUID for ``record.remote_script`` and
    ``RuntimeConnection.send_command`` independently minted another for the frame
    it actually sent. The bridge indexes the subprocess under the op_id of the
    command it received, so ``cancel_script_run`` relayed
    ``CANCEL_SCRIPT(target_op_id=...)`` for an operation that runtime had never
    heard of: the runtime answered "not running", the record journalled
    CANCELLED, and the script kept running to completion in the worker pod
    (review finding 1 on #802).
    """

    @pytest.mark.asyncio
    async def test_a_caller_supplied_op_id_is_the_one_on_the_wire(self):
        from cli_agent_orchestrator.runtime_channel.protocol import (
            CommandOutcome,
            CommandResultFrame,
            CommandType,
            decode_frame,
        )
        from cli_agent_orchestrator.runtime_channel.registry import RuntimeConnection

        sent = []

        async def send_text(raw):
            sent.append(decode_frame(raw))

        conn = RuntimeConnection("worker-x", send_text)
        task = asyncio.ensure_future(
            conn.send_command(
                CommandType.RUN_SCRIPT, {"script": "pass"}, timeout=10.0, op_id="chosen-op"
            )
        )
        await asyncio.sleep(0)
        assert [f.op_id for f in sent] == ["chosen-op"]
        # And the future is keyed by that same id, so the runtime's result lands.
        conn.resolve(
            CommandResultFrame(op_id="chosen-op", outcome=CommandOutcome.OK, payload={"rc": 0})
        )
        assert (await task).payload == {"rc": 0}

    @pytest.mark.asyncio
    async def test_without_one_an_id_is_still_minted(self):
        """Every other caller passes nothing and must keep working."""
        from cli_agent_orchestrator.runtime_channel.protocol import CommandType, decode_frame
        from cli_agent_orchestrator.runtime_channel.registry import RuntimeConnection

        sent = []

        async def send_text(raw):
            sent.append(decode_frame(raw))

        conn = RuntimeConnection("worker-x", send_text)
        task = asyncio.ensure_future(
            conn.send_command(CommandType.INPUT, {"text": "hi"}, timeout=0.2)
        )
        await asyncio.sleep(0)
        assert len(sent) == 1 and sent[0].op_id
        task.cancel()

    @pytest.mark.asyncio
    async def test_the_recorded_op_id_is_the_dispatched_one(self, monkeypatch):
        """What cancel will name must be what the runtime was told."""
        from unittest.mock import AsyncMock

        from cli_agent_orchestrator.services import script_runner

        seen = {}

        class _Conn:
            async def send_command(self, command_type, payload, **kwargs):
                # Captured here rather than after the call: the driver clears
                # record.remote_script in its own `finally`.
                seen["dispatched"] = kwargs.get("op_id")
                seen["recorded"] = record.remote_script
                from cli_agent_orchestrator.runtime_channel.protocol import (
                    CommandOutcome,
                    CommandResultFrame,
                )

                return CommandResultFrame(
                    op_id=kwargs.get("op_id") or "generated",
                    outcome=CommandOutcome.OK,
                    payload={"returncode": 0, "stdout": "", "stderr": "", "timed_out": False},
                )

        monkeypatch.setattr(
            "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.get_runtime",
            lambda runtime_id: _Conn(),
        )
        monkeypatch.setattr(script_runner, "_interpret_and_finalize", AsyncMock(return_value=None))

        record = script_runner.ScriptRunRecord(
            run_id="run-1",
            workflow_name="wf",
            state=script_runner.RunState.RUNNING,
            cancelled=False,
            current_step_id=None,
            step_states={},
            process=None,
            generation="g1",
            started_at="2026-09-21T00:00:00",
            finished_at=None,
        )
        script = __import__("tempfile").NamedTemporaryFile("w", suffix=".py", delete=False)
        script.write("print('x')\n")
        script.close()

        await script_runner._drive_process_remote(record, "worker-x", script.name, {"PATH": ""})

        assert seen["recorded"] == ("worker-x", seen["dispatched"])
        assert seen["dispatched"], "an op_id must be dispatched, not left to the transport"

    @pytest.mark.asyncio
    async def test_a_cancel_for_the_dispatched_op_reaches_the_subprocess(self, bridge):
        """The bridge half, driven through the frames rather than around them."""
        from cli_agent_orchestrator.runtime_channel.protocol import (
            CommandFrame,
            CommandOutcome,
            CommandType,
        )

        run = asyncio.ensure_future(
            bridge._execute(
                CommandFrame(
                    op_id="op-shared",
                    terminal_id=None,
                    type=CommandType.RUN_SCRIPT,
                    payload={
                        "script": "import time; time.sleep(30)",
                        "env": {"PATH": ""},
                        "timeout": 30.0,
                        "term_grace": 2.0,
                    },
                )
            )
        )
        for _ in range(100):
            if "op-shared" in bridge._script_procs:
                break
            await asyncio.sleep(0.05)
        assert "op-shared" in bridge._script_procs, "the subprocess is indexed by the frame's op_id"

        outcome, payload, _ = await bridge._execute(
            CommandFrame(
                op_id="op-cancel",
                terminal_id=None,
                type=CommandType.CANCEL_SCRIPT,
                payload={"target_op_id": "op-shared", "term_grace": 2.0},
            )
        )
        assert outcome == CommandOutcome.CANCEL_REQUESTED
        assert payload == {"cancelled": True}

        _, result, _ = await asyncio.wait_for(run, timeout=15)
        assert result["returncode"] != 0, "the script must not survive its own cancellation"

    @pytest.mark.asyncio
    async def test_a_cancel_for_an_unknown_op_says_so_instead_of_guessing(self, bridge):
        """The symptom the mismatch produced: a cancel that terminates nothing.

        Keeping this explicit is what makes the identity bug visible rather than
        silent — with two ids, this was the answer on every remote cancel.
        """
        from cli_agent_orchestrator.runtime_channel.protocol import CommandFrame, CommandType

        outcome, payload, _ = await bridge._execute(
            CommandFrame(
                op_id="op-cancel",
                terminal_id=None,
                type=CommandType.CANCEL_SCRIPT,
                payload={"target_op_id": "never-dispatched", "term_grace": 2.0},
            )
        )
        assert payload == {"cancelled": False, "reason": "not running"}
