"""Unit tests for cao-mcp-server command resolution."""

from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_MODULE,
    cao_server_env,
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

    def test_resolves_command_and_preserves_other_keys(self, monkeypatch):
        # The default-server case must be independent of the CAO running pytest.
        for name in ("CAO_API_HOST", "CAO_API_PORT", "CAO_HOME_DIR"):
            monkeypatch.delenv(name, raising=False)
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

    def test_non_string_command_passes_through_without_crashing(self):
        """P2-1: ``AgentProfile.mcpServers`` is ``Dict[str, Any]``, so ``command``
        can reach here as a list (OpenCode's own format) or any other non-str
        value. The basename check must not call ``Path()`` on it."""
        out = resolve_mcp_server_config({"command": ["uvx", "x"]})
        assert out["command"] == ["uvx", "x"]

        out = resolve_mcp_server_config({"command": 123})
        assert out["command"] == 123


class TestServerEnv:
    """A runtime bundled server learns the address and home of the CAO that launched it."""

    @staticmethod
    def _set(monkeypatch, **values):
        for key in ("CAO_API_HOST", "CAO_API_PORT", "CAO_HOME_DIR"):
            monkeypatch.delenv(key, raising=False)
        for key, value in values.items():
            monkeypatch.setenv(key, value)

    def test_runtime_bundled_entry_gains_the_set_variables(self, monkeypatch):
        self._set(monkeypatch, CAO_API_PORT="9899", CAO_HOME_DIR="/srv/cao-b")
        monkeypatch.setattr(f"{MOD}.CAO_HOME_DIR", Path("/srv/cao-b"))
        with patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"):
            out = resolve_mcp_server_config(
                {"command": "cao-mcp-server", "env": {"CAO_TERMINAL_ID": "abc"}}
            )
        assert out["env"] == {
            "CAO_API_PORT": "9899",
            "CAO_HOME_DIR": "/srv/cao-b",
            "CAO_TERMINAL_ID": "abc",
        }

    def test_relative_cao_home_dir_is_forwarded_as_the_resolved_absolute_path(self, monkeypatch):
        """P2-1: a relative ``CAO_HOME_DIR`` must not re-resolve against a
        child's (different) cwd. ``cao_server_env`` must forward the already-
        resolved absolute path (``constants.CAO_HOME_DIR``), not the raw env
        text, even though the raw text is what's set in ``os.environ``."""
        self._set(monkeypatch, CAO_HOME_DIR="cao-home")
        monkeypatch.setattr(f"{MOD}.CAO_HOME_DIR", Path("/srv/resolved/cao-home"))
        assert cao_server_env() == {"CAO_HOME_DIR": "/srv/resolved/cao-home"}

    def test_cao_home_dir_absent_when_env_unset(self, monkeypatch):
        self._set(monkeypatch)
        monkeypatch.setattr(f"{MOD}.CAO_HOME_DIR", Path("/default/home"))
        assert "CAO_HOME_DIR" not in cao_server_env()

    def test_wildcard_host_is_mapped_to_loopback(self, monkeypatch):
        """#1075 note 4: an env-only CAO_API_HOST=0.0.0.0 must get the same
        127.0.0.1 mapping main() applies to the --host flag, so the two paths
        agree on what address a client can actually connect to."""
        self._set(monkeypatch, CAO_API_HOST="0.0.0.0")
        assert cao_server_env() == {"CAO_API_HOST": "127.0.0.1"}

    def test_non_wildcard_host_passes_through(self, monkeypatch):
        self._set(monkeypatch, CAO_API_HOST="10.0.0.5")
        assert cao_server_env() == {"CAO_API_HOST": "10.0.0.5"}

    def test_the_entry_own_env_wins(self, monkeypatch):
        self._set(monkeypatch, CAO_API_PORT="9899")
        with patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"):
            out = resolve_mcp_server_config(
                {"command": "cao-mcp-server", "env": {"CAO_API_PORT": "7000"}}
            )
        assert out["env"] == {"CAO_API_PORT": "7000"}

    def test_persisted_custom_and_default_entries_gain_nothing(self, monkeypatch):
        self._set(monkeypatch, CAO_API_PORT="9899")
        with (
            patch(f"{MOD}._sibling_script", return_value=""),
            patch(f"{MOD}.shutil.which", return_value="/usr/local/bin/cao-mcp-server"),
        ):
            persisted = resolve_mcp_server_config({"command": "cao-mcp-server"}, persisted=True)
        custom = resolve_mcp_server_config({"command": "my-server"})
        assert "env" not in persisted and "env" not in custom
        self._set(monkeypatch)
        with patch(f"{MOD}._sibling_script", return_value="/venv/bin/cao-mcp-server"):
            default = resolve_mcp_server_config({"command": "cao-mcp-server"})
        assert "env" not in default

    def test_uvx_launched_entry_named_cao_mcp_server_gains_the_address(self, monkeypatch):
        """#1075 P3-1: the entry's own name in the profile's ``mcpServers`` map
        identifies it as the CAO server too, the same way Codex's own
        ``is_cao_server`` check already does, so a ``uvx`` launch is covered."""
        self._set(monkeypatch, CAO_API_PORT="9899")
        out = resolve_mcp_server_config(
            {"command": "uvx", "args": ["--from", "pkg", "cao-mcp-server"]},
            name="cao-mcp-server",
        )
        assert out["env"] == {"CAO_API_PORT": "9899"}
        # Name-matching only adds the address; it does not touch the uvx command.
        assert out["command"] == "uvx"

    def test_absolute_path_entry_gains_the_address_by_final_segment(self, monkeypatch):
        """An entry started by an absolute path is the CAO server when the
        path's final segment is the bare console-script name."""
        self._set(monkeypatch, CAO_API_PORT="9899")
        out = resolve_mcp_server_config({"command": "/opt/cao/bin/cao-mcp-server"})
        assert out["env"] == {"CAO_API_PORT": "9899"}

    def test_non_cao_server_never_gains_the_address(self, monkeypatch):
        """Neither the command, its final path segment, nor the entry name is
        ``cao-mcp-server``, so no address is added — even via uvx."""
        self._set(monkeypatch, CAO_API_PORT="9899")
        out = resolve_mcp_server_config(
            {"command": "uvx", "args": ["--from", "pkg", "my-server"]},
            name="my-server",
        )
        assert "env" not in out

    @pytest.mark.parametrize(
        "config,name",
        [
            ({"command": "/usr/local/bin/my-cao-mcp-server"}, None),
            ({"command": "cao-mcp-server-dev"}, None),
            ({"command": "node"}, "cao-mcp-server-2"),
        ],
    )
    def test_near_miss_entries_never_gain_the_address(self, monkeypatch, config, name):
        """P3-1: these entries each contain ``cao-mcp-server`` as a substring
        of the basename or the name, but neither equals it exactly. A
        substring regression on either clause (``CAO_MCP_SERVER_COMMAND in
        str(command)`` or `` in (name or "")``) would wrongly match them."""
        self._set(monkeypatch, CAO_API_PORT="9899")
        kwargs = {"name": name} if name is not None else {}
        out = resolve_mcp_server_config(config, **kwargs)
        assert "env" not in out

    def test_persisted_entry_never_gains_the_address_even_by_name(self, monkeypatch):
        """``persisted=True`` is the off switch regardless of how the entry is
        identified as the CAO server — a persisted config outlives this
        process and may be read by another CAO."""
        self._set(monkeypatch, CAO_API_PORT="9899")
        out = resolve_mcp_server_config(
            {"command": "uvx", "args": ["--from", "pkg", "cao-mcp-server"]},
            name="cao-mcp-server",
            persisted=True,
        )
        assert "env" not in out
