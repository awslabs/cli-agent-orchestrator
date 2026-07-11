"""Unit tests for cao-mcp-server command resolution."""

from unittest.mock import patch

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_MODULE,
    resolve_cao_mcp_command,
    resolve_mcp_server_config,
)

MOD = "cli_agent_orchestrator.utils.mcp_resolution"


class TestPassthrough:
    """Non-bundled commands are never rewritten."""

    def test_passthrough_for_non_cao_command(self):
        cmd, args = resolve_cao_mcp_command("uvx", ["--from", "pkg", "thing"])
        assert cmd == "uvx"
        assert args == ["--from", "pkg", "thing"]

    def test_passthrough_with_nonempty_args_returns_copy(self):
        """Passthrough must copy the args list, not alias the caller's."""
        original = ["--from", "pkg"]
        _, args = resolve_cao_mcp_command("uvx", original)
        assert args == original
        args.append("mutated")
        assert original == ["--from", "pkg"]


class TestSiblingScript:
    """_sibling_script finds the console script next to sys.executable."""

    def test_returns_path_when_script_exists(self, tmp_path):
        from cli_agent_orchestrator.utils.mcp_resolution import (
            _SCRIPT_FILENAME,
            _sibling_script,
        )

        fake_python = tmp_path / "python3"
        fake_python.touch()
        (tmp_path / _SCRIPT_FILENAME).touch()
        with patch(f"{MOD}.sys") as mock_sys:
            mock_sys.executable = str(fake_python)
            assert _sibling_script() == str(tmp_path / _SCRIPT_FILENAME)

    def test_returns_empty_when_script_missing(self, tmp_path):
        from cli_agent_orchestrator.utils.mcp_resolution import _sibling_script

        fake_python = tmp_path / "python3"
        fake_python.touch()
        with patch(f"{MOD}.sys") as mock_sys:
            mock_sys.executable = str(fake_python)
            assert _sibling_script() == ""

    def test_returns_empty_for_empty_sys_executable(self):
        from cli_agent_orchestrator.utils.mcp_resolution import _sibling_script

        with patch(f"{MOD}.sys") as mock_sys:
            mock_sys.executable = ""
            assert _sibling_script() == ""


class TestArgsPreservation:
    """Caller-supplied args survive resolution in every tier."""

    def test_script_resolution_preserves_args(self):
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", ["--log-level", "debug"])
        assert cmd == "/venv/bin/cao-mcp-server"
        assert args == ["--log-level", "debug"]

    def test_module_fallback_appends_args_after_module(self):
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value=None),
            patch(f"{MOD}.sys") as mock_sys,
        ):
            mock_sys.executable = "/venv/bin/python3"
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", ["--log-level", "debug"])
        assert cmd == "/venv/bin/python3"
        assert args == ["-m", CAO_MCP_SERVER_MODULE, "--log-level", "debug"]

    def test_resolved_args_are_a_copy(self):
        original = ["--flag"]
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            _, args = resolve_cao_mcp_command("cao-mcp-server", original)
        args.append("mutated")
        assert original == ["--flag"]


class TestRuntimeResolution:
    """persisted=False (default): prefer the interpreter-sibling script."""

    def test_prefers_sibling_over_path(self):
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value="/usr/local/bin/cao-mcp-server"),
        ):
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", [])
        assert cmd == "/venv/bin/cao-mcp-server"
        assert args == []

    def test_falls_back_to_path_when_no_sibling(self):
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value="/usr/local/bin/cao-mcp-server"),
        ):
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", [])
        assert cmd == "/usr/local/bin/cao-mcp-server"
        assert args == []

    def test_falls_back_to_module_entrypoint(self):
        """No sibling, nothing on PATH → run the module via the interpreter."""
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value=None),
            patch(f"{MOD}.sys") as mock_sys,
        ):
            mock_sys.executable = "/usr/bin/python3"
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", [])
        assert cmd == "/usr/bin/python3"
        assert args == ["-m", CAO_MCP_SERVER_MODULE]

    def test_module_fallback_tolerates_empty_sys_executable(self):
        """A frozen/embedded interpreter (sys.executable == '') must not crash."""
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value=None),
            patch(f"{MOD}.sys") as mock_sys,
        ):
            mock_sys.executable = ""
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", [])
        assert cmd == "python3"  # best-effort fallback
        assert args == ["-m", CAO_MCP_SERVER_MODULE]


class TestPersistedResolution:
    """persisted=True: prefer the stable PATH launcher (survives upgrades)."""

    def test_prefers_path_over_sibling(self):
        """The versioned sibling path would go stale on upgrade; prefer PATH."""
        with (
            patch(f"{MOD}._sibling_script", return_value="/versioned/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value="/home/u/.local/bin/cao-mcp-server"),
        ):
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", [], persisted=True)
        assert cmd == "/home/u/.local/bin/cao-mcp-server"
        assert args == []

    def test_falls_back_to_sibling_when_not_on_path(self):
        with (
            patch(f"{MOD}._sibling_script", return_value="/versioned/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            cmd, args = resolve_cao_mcp_command("cao-mcp-server", [], persisted=True)
        assert cmd == "/versioned/venv/bin/cao-mcp-server"
        assert args == []


class TestResolveMcpServerConfig:
    """resolve_mcp_server_config wraps the resolver over a config dict."""

    def test_resolves_command_and_preserves_other_keys(self):
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value="/usr/local/bin/cao-mcp-server"),
        ):
            out = resolve_mcp_server_config(
                {
                    "type": "stdio",
                    "command": "cao-mcp-server",
                    "args": [],
                    "env": {"CAO_TERMINAL_ID": "abc"},
                }
            )
        assert out["command"] == "/usr/local/bin/cao-mcp-server"
        assert out["args"] == []
        assert out["type"] == "stdio"
        assert out["env"] == {"CAO_TERMINAL_ID": "abc"}

    def test_does_not_mutate_input(self):
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value="/usr/local/bin/cao-mcp-server"),
        ):
            original = {"command": "cao-mcp-server", "args": []}
            resolve_mcp_server_config(original)
        assert original["command"] == "cao-mcp-server"

    def test_idempotent_after_resolution(self):
        """Re-resolving an already-resolved config is a no-op passthrough.

        Matters because install_service resolves, then the OpenCode translator
        may resolve again; the second pass must not double-transform.
        """
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value="/usr/local/bin/cao-mcp-server"),
        ):
            once = resolve_mcp_server_config({"command": "cao-mcp-server", "args": []})
            twice = resolve_mcp_server_config(once)
        assert once == twice
        assert twice["command"] == "/usr/local/bin/cao-mcp-server"

    def test_persisted_forwarded(self):
        with (
            patch(f"{MOD}._sibling_script", return_value="/versioned/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value="/stable/bin/cao-mcp-server"),
        ):
            out = resolve_mcp_server_config(
                {"command": "cao-mcp-server", "args": []}, persisted=True
            )
        assert out["command"] == "/stable/bin/cao-mcp-server"

    def test_custom_server_passthrough(self):
        out = resolve_mcp_server_config(
            {"type": "stdio", "command": "my-server", "args": ["--port", "9000"]}
        )
        assert out["command"] == "my-server"
        assert out["args"] == ["--port", "9000"]

    def test_commandless_url_server_passes_through_untouched(self):
        """A url/transport server (no command) must not gain command/args keys.

        Providers emit every present key, so injecting command=""/args=[] into
        an http/sse entry would corrupt it (e.g. codex would emit an invalid
        empty command override).
        """
        original = {"type": "http", "url": "https://example.com/mcp"}
        out = resolve_mcp_server_config(original)
        assert out == original
        assert "command" not in out
        assert "args" not in out
        # Still a copy, not the same object.
        out["type"] = "mutated"
        assert original["type"] == "http"

    def test_command_without_args_key_does_not_gain_args_for_passthrough(self):
        """A non-cao command entry that omits args stays args-less."""
        out = resolve_mcp_server_config({"type": "stdio", "command": "my-server"})
        assert out["command"] == "my-server"
        assert "args" not in out
