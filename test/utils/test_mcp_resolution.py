"""Unit tests for cao-mcp-server command resolution."""

from unittest.mock import patch

from cli_agent_orchestrator.constants import (
    SERVER_COORDINATE_ENV_VARS,
    server_coordinate_env,
)
from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_MODULE,
    apply_server_coordinates,
    is_bundled_cao_server,
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

    def test_resolves_command_and_preserves_other_keys(self, default_cao_server):
        # default_cao_server: this asserts the exact runtime env of a DEFAULT
        # server's bundled entry, which a developer's exported CAO_HOME_DIR
        # would otherwise add a key to (#1066).
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


class TestServerCoordinateEnv:
    """``constants.server_coordinate_env`` reports only what is actually set."""

    def test_empty_for_a_default_server(self, default_cao_server):
        assert server_coordinate_env() == {}

    def test_reports_only_the_vars_that_are_set(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        assert server_coordinate_env() == {"CAO_API_PORT": "9899"}

    def test_reports_all_three(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_HOST", "10.0.0.4")
        monkeypatch.setenv("CAO_API_PORT", "9899")
        monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao-trial")
        assert server_coordinate_env() == {
            "CAO_API_HOST": "10.0.0.4",
            "CAO_API_PORT": "9899",
            "CAO_HOME_DIR": "/srv/cao-trial",
        }

    def test_whitespace_only_is_unset(self, monkeypatch, default_cao_server):
        """Matches how ``CAO_HOME_DIR`` itself is read in constants."""
        monkeypatch.setenv("CAO_HOME_DIR", "   ")
        assert server_coordinate_env() == {}

    def test_never_reports_the_defaults(self, default_cao_server):
        """A default server must not advertise 127.0.0.1:9889 as an override.

        An entry carrying the defaults explicitly is not byte-identical to one
        that omits them, and would also pin a port the next CAO may not use.
        """
        assert "CAO_API_PORT" not in server_coordinate_env()
        assert SERVER_COORDINATE_ENV_VARS == ("CAO_API_HOST", "CAO_API_PORT", "CAO_HOME_DIR")


class TestApplyServerCoordinates:
    """``apply_server_coordinates`` is the one place the coordinates are merged."""

    def test_identity_for_a_default_server(self, default_cao_server):
        original = {"command": "/venv/bin/cao-mcp-server", "args": []}
        assert apply_server_coordinates(original) is original

    def test_adds_the_set_coordinates(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        out = apply_server_coordinates({"command": "x", "env": {"CAO_TERMINAL_ID": "t1"}})
        assert out["env"] == {"CAO_API_PORT": "9899", "CAO_TERMINAL_ID": "t1"}

    def test_adds_env_to_an_entry_that_had_none(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        out = apply_server_coordinates({"command": "x"})
        assert out["env"] == {"CAO_API_PORT": "9899"}

    def test_the_entrys_own_env_wins(self, monkeypatch, default_cao_server):
        """An author who pinned a port means it."""
        monkeypatch.setenv("CAO_API_PORT", "9899")
        monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao-trial")
        out = apply_server_coordinates({"command": "x", "env": {"CAO_API_PORT": "7777"}})
        assert out["env"]["CAO_API_PORT"] == "7777"
        assert out["env"]["CAO_HOME_DIR"] == "/srv/cao-trial"

    def test_does_not_mutate_the_caller(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        original = {"command": "x", "env": {"CAO_TERMINAL_ID": "t1"}}
        apply_server_coordinates(original)
        assert original["env"] == {"CAO_TERMINAL_ID": "t1"}


class TestResolveCarriesServerCoordinates:
    """The runtime bundled entry gains them; the persisted entry gains nothing."""

    def _bundled(self):
        return {"type": "stdio", "command": "cao-mcp-server", "args": []}

    def test_runtime_entry_carries_them(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            out = resolve_mcp_server_config(self._bundled())
        assert out["env"]["CAO_API_PORT"] == "9899"

    def test_persisted_entry_gains_nothing(self, monkeypatch, default_cao_server):
        """A persisted entry outlives this process; another CAO reads it."""
        monkeypatch.setenv("CAO_API_HOST", "10.0.0.4")
        monkeypatch.setenv("CAO_API_PORT", "9899")
        monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao-trial")
        with (
            patch(f"{MOD}._sibling_script", return_value="/versioned/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value="/stable/bin/cao-mcp-server"),
        ):
            out = resolve_mcp_server_config(self._bundled(), persisted=True)
        assert "env" not in out

    def test_runtime_entrys_own_env_still_wins(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        entry = dict(self._bundled(), env={"CAO_API_PORT": "7777", "CAO_TERMINAL_ID": "t1"})
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            out = resolve_mcp_server_config(entry)
        assert out["env"] == {"CAO_API_PORT": "7777", "CAO_TERMINAL_ID": "t1"}

    def test_default_server_leaves_the_entry_byte_identical(self, default_cao_server):
        """No coordinates set: exactly the pre-change result, env key and all."""
        with (
            patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"),
            patch(f"{MOD}.shutil.which", return_value=None),
        ):
            out = resolve_mcp_server_config(self._bundled())
        assert out == {
            "type": "stdio",
            "command": "/venv/bin/cao-mcp-server",
            "args": [],
        }

    def test_third_party_server_is_untouched(self, monkeypatch, default_cao_server):
        """Only the bundled server learns where this CAO lives."""
        monkeypatch.setenv("CAO_API_PORT", "9899")
        out = resolve_mcp_server_config({"type": "stdio", "command": "my-server"})
        assert "env" not in out

    def test_commandless_url_server_is_untouched(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        original = {"type": "http", "url": "https://example.com/mcp"}
        assert resolve_mcp_server_config(original) == original


class TestIsBundledCaoServer:
    """Every shape that starts CAO's own server is recognised structurally."""

    def test_bare_console_script(self):
        assert is_bundled_cao_server({"command": "cao-mcp-server", "args": []})

    def test_resolved_absolute_path(self):
        assert is_bundled_cao_server({"command": "/venv/bin/cao-mcp-server", "args": []})

    def test_module_entrypoint(self):
        assert is_bundled_cao_server(
            {"command": "/venv/bin/python3", "args": ["-m", CAO_MCP_SERVER_MODULE]}
        )

    def test_uvx_form_used_by_the_shipped_aws_templates(self):
        """``templates/aws/*/template.md.j2`` declares the server this way.

        It is a passthrough for command resolution (the command is ``uvx``), so
        a command-equality check alone would have left every profile created
        from those templates reporting to the default CAO.
        """
        assert is_bundled_cao_server(
            {
                "type": "stdio",
                "command": "uvx",
                "args": [
                    "--from",
                    "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
                    "cao-mcp-server",
                ],
            }
        )

    def test_third_party_server_is_not_bundled(self):
        assert not is_bundled_cao_server({"command": "my-server", "args": ["--port", "9000"]})

    def test_uvx_third_party_server_is_not_bundled(self):
        assert not is_bundled_cao_server(
            {"command": "uvx", "args": ["--from", "pkg", "some-other-server"]}
        )

    def test_commandless_url_server_is_not_bundled(self):
        assert not is_bundled_cao_server({"type": "http", "url": "https://example.com/mcp"})

    def test_a_command_merely_containing_the_name_is_not_bundled(self):
        """Basename equality, not a substring match."""
        assert not is_bundled_cao_server({"command": "/opt/bin/cao-mcp-server-proxy"})


class TestUvxBundledServerCarriesCoordinates:
    """The uvx form reaches the same outcome as the bare console script."""

    def _uvx_entry(self):
        return {
            "type": "stdio",
            "command": "uvx",
            "args": [
                "--from",
                "git+https://github.com/awslabs/cli-agent-orchestrator.git@main",
                "cao-mcp-server",
            ],
        }

    def test_runtime_uvx_entry_carries_them(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        out = resolve_mcp_server_config(self._uvx_entry())
        assert out["env"] == {"CAO_API_PORT": "9899"}
        # Command resolution still passes uvx through untouched.
        assert out["command"] == "uvx"
        assert out["args"] == self._uvx_entry()["args"]

    def test_persisted_uvx_entry_gains_nothing(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        out = resolve_mcp_server_config(self._uvx_entry(), persisted=True)
        assert "env" not in out

    def test_default_server_leaves_the_uvx_entry_byte_identical(self, default_cao_server):
        original = self._uvx_entry()
        assert resolve_mcp_server_config(original) == original
