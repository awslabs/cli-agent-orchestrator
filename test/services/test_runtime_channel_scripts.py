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
        """The body is the caller's code, in a pod that may host other work."""
        import os

        result = await bridge._run_script(
            "op-mode-5",
            '#!/bin/sh\nstat -f %Lp "$0" 2>/dev/null || stat -c %a "$0"\n',
            {"PATH": os.environ.get("PATH", "")},
            timeout=30.0,
            term_grace=5.0,
            mode="executable",
        )
        assert result["stdout"].strip() == "700"


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
