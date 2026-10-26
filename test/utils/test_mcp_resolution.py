"""Unit tests for cao-mcp-server command resolution."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

import cli_agent_orchestrator
from cli_agent_orchestrator.constants import (
    CAO_HOME_DIR,
    SERVER_COORDINATE_ENV_VARS,
    server_coordinate_env,
)
from cli_agent_orchestrator.services.agent_scaffold import _TEMPLATES_ROOT
from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_DISTRIBUTION_NAME,
    CAO_MCP_SERVER_COMMAND,
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

    def test_a_relative_home_is_canonicalised(self, monkeypatch, default_cao_server):
        """A relative ``CAO_HOME_DIR`` means cao-server's cwd, not the child's.

        A provider may start the MCP subprocess from another working
        directory; forwarding the raw string would send it to a different
        home and a second, empty state tree.
        """
        monkeypatch.setenv("CAO_HOME_DIR", "relative-state")
        forwarded = server_coordinate_env()["CAO_HOME_DIR"]
        assert Path(forwarded).is_absolute()
        assert forwarded == str((Path.cwd() / "relative-state").resolve())

    def test_a_tilde_home_is_expanded(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_HOME_DIR", "~/cao-trial")
        assert server_coordinate_env()["CAO_HOME_DIR"] == str(Path.home() / "cao-trial")

    def test_an_absolute_home_is_unchanged(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao-trial")
        assert server_coordinate_env()["CAO_HOME_DIR"] == "/srv/cao-trial"

    def test_an_unset_home_is_absent(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_PORT", "9899")
        assert "CAO_HOME_DIR" not in server_coordinate_env()

    def test_the_forwarded_home_is_the_one_constants_resolved(self, monkeypatch):
        """With the environment as it was at import, the child gets OUR home.

        The reviewer's ask, pinned literally: the value handed to a subprocess
        is ``str(CAO_HOME_DIR)``, the home this process actually uses.
        """
        monkeypatch.setenv("CAO_HOME_DIR", str(CAO_HOME_DIR))
        # Compared as resolved paths, not text: on a machine whose default home
        # is a symlink (a shared state leaf, say), ``CAO_HOME_DIR`` is the
        # unresolved default while the helper deliberately canonicalises, so a
        # textual comparison fails for a reason that has nothing to do with the
        # contract under test (independent review of #1066, round 3).
        forwarded = server_coordinate_env()["CAO_HOME_DIR"]
        assert Path(forwarded).resolve() == Path(CAO_HOME_DIR).resolve()

    def test_nothing_blank_is_ever_forwarded(self, monkeypatch, default_cao_server):
        """A blank value in the CHILD is what breaks it, so none is sent.

        An empty ``CAO_API_PORT`` raises at the child's import of
        ``constants`` (``int("")``) and an empty ``CAO_API_HOST`` yields a
        ``http://:9889`` base URL. The filter here is what keeps either from
        ever being handed over; constants' own parsing of a blank value set by
        someone else is a separate matter and unchanged by this PR.
        """
        monkeypatch.setenv("CAO_API_HOST", "")
        monkeypatch.setenv("CAO_API_PORT", "   ")
        monkeypatch.setenv("CAO_HOME_DIR", "\t")
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
    """Every shape that starts CAO's own server is recognised by its SHAPE.

    The command and the arguments that give that command its meaning are
    checked together. Naming ``cao-mcp-server`` (or the module) somewhere in an
    argument list is not enough -- a third party is free to do that, and must
    not be handed this server's host, port and home.
    """

    def test_bare_console_script(self):
        assert is_bundled_cao_server({"command": "cao-mcp-server", "args": []})

    def test_resolved_absolute_path(self):
        assert is_bundled_cao_server({"command": "/venv/bin/cao-mcp-server", "args": []})

    def test_module_entrypoint(self):
        assert is_bundled_cao_server(
            {"command": "/venv/bin/python3", "args": ["-m", CAO_MCP_SERVER_MODULE]}
        )

    def test_module_entrypoint_via_sys_executable(self):
        """The exact interpreter ``resolve_cao_mcp_command`` falls back to."""
        assert is_bundled_cao_server(
            {"command": sys.executable, "args": ["-m", CAO_MCP_SERVER_MODULE]}
        )

    def test_module_entrypoint_with_trailing_server_args(self):
        assert is_bundled_cao_server(
            {"command": "python3.12", "args": ["-m", CAO_MCP_SERVER_MODULE, "--verbose"]}
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

    def test_uvx_from_the_published_distribution_name(self):
        assert is_bundled_cao_server(
            {"command": "uvx", "args": ["--from", CAO_DISTRIBUTION_NAME, "cao-mcp-server"]}
        )

    def test_uvx_from_the_distribution_with_extras_and_a_version(self):
        """PEP 503 spelling and a version pin are still this package."""
        assert is_bundled_cao_server(
            {
                "command": "uvx",
                "args": ["--from=cli_agent_orchestrator[all]>=0.2", "cao-mcp-server"],
            }
        )

    def test_uv_tool_run_long_form(self):
        assert is_bundled_cao_server(
            {
                "command": "/home/op/.local/bin/uv",
                "args": ["tool", "run", "--from", CAO_DISTRIBUTION_NAME, "cao-mcp-server"],
            }
        )

    def test_uvx_from_a_git_url_without_the_dot_git_suffix(self):
        assert is_bundled_cao_server(
            {
                "command": "uvx",
                "args": [
                    "--from",
                    "git+ssh://git@github.com/awslabs/cli-agent-orchestrator",
                    "cao-mcp-server",
                ],
            }
        )

    # ── the three near-misses the independent review found (#1066 review) ──

    def test_uvx_from_another_package_is_not_bundled(self):
        """``--from`` decides whose ``cao-mcp-server`` script runs.

        Any distribution may ship a console script by that name; handing it
        CAO's internal endpoint and ``CAO_HOME_DIR`` would leak both to a
        third party.
        """
        assert not is_bundled_cao_server(
            {"command": "uvx", "args": ["--from", "some-other-package", "cao-mcp-server"]}
        )

    def test_a_third_party_taking_the_name_as_an_argument_is_not_bundled(self):
        assert not is_bundled_cao_server(
            {"command": "my-server", "args": ["--helper", "cao-mcp-server"]}
        )

    def test_a_third_party_taking_the_module_as_an_argument_is_not_bundled(self):
        assert not is_bundled_cao_server(
            {"command": "my-server", "args": ["--module", CAO_MCP_SERVER_MODULE]}
        )

    # ── further negatives ──

    def test_third_party_server_is_not_bundled(self):
        assert not is_bundled_cao_server({"command": "my-server", "args": ["--port", "9000"]})

    def test_uvx_third_party_server_is_not_bundled(self):
        assert not is_bundled_cao_server(
            {"command": "uvx", "args": ["--from", "pkg", "some-other-server"]}
        )

    def test_uvx_without_a_from_is_not_bundled(self):
        """Without ``--from``, uvx resolves the name on the index -- not ours."""
        assert not is_bundled_cao_server({"command": "uvx", "args": ["cao-mcp-server"]})

    def test_uvx_from_a_lookalike_repository_is_not_bundled(self):
        assert not is_bundled_cao_server(
            {
                "command": "uvx",
                "args": [
                    "--from",
                    "git+https://example.invalid/x/cli-agent-orchestrator-fork.git",
                    "cao-mcp-server",
                ],
            }
        )

    def test_uv_without_tool_run_is_not_bundled(self):
        assert not is_bundled_cao_server(
            {"command": "uv", "args": ["run", "--from", CAO_DISTRIBUTION_NAME, "cao-mcp-server"]}
        )

    def test_a_non_interpreter_running_the_module_flag_is_not_bundled(self):
        """``-m`` only means our module when a Python interpreter reads it."""
        assert not is_bundled_cao_server({"command": "node", "args": ["-m", CAO_MCP_SERVER_MODULE]})

    def test_an_interpreter_running_another_module_is_not_bundled(self):
        assert not is_bundled_cao_server({"command": "python3", "args": ["-m", "http.server"]})

    def test_commandless_url_server_is_not_bundled(self):
        assert not is_bundled_cao_server({"type": "http", "url": "https://example.com/mcp"})

    def test_a_command_merely_containing_the_name_is_not_bundled(self):
        """Basename equality, not a substring match."""
        assert not is_bundled_cao_server({"command": "/opt/bin/cao-mcp-server-proxy"})


def _entries_in(files):
    """Every ``mcpServers`` entry declared in these profile/template files."""
    entries = []
    for path in files:
        frontmatter = path.read_text().split("---")[1]
        servers = yaml.safe_load(frontmatter).get("mcpServers") or {}
        for name, config in servers.items():
            entries.append((f"{path.parent.name}/{path.name}", name, config))
    return entries


def _shipped_templates():
    files = sorted((_TEMPLATES_ROOT / "aws").glob("*/template.md.j2"))
    assert files, f"no shipped AWS templates under {_TEMPLATES_ROOT / 'aws'}"
    return _entries_in(files)


def _shipped_profiles():
    root = Path(cli_agent_orchestrator.__file__).parent / "agent_store"
    files = sorted(root.glob("*.md"))
    assert files, f"no shipped agent profiles under {root}"
    return _entries_in(files)


class TestShippedTemplatesAreRecognised:
    """The entries in the REAL shipped files, not a copy of them.

    ``templates/aws/*/template.md.j2`` is where the uvx shape comes from and
    ``agent_store/*.md`` is where the console-script shape comes from. A
    hand-written fixture would keep passing if someone edited those files; this
    parses them. Between them they cover two of the three recognised shapes;
    the third (the module entrypoint) is what resolution falls back to.
    """

    def test_every_shipped_uvx_entry_is_recognised(self):
        entries = _shipped_templates()
        assert entries, "the shipped templates declare no MCP server"
        for template, name, config in entries:
            assert config["command"] == "uvx", f"{template}:{name} changed shape"
            assert is_bundled_cao_server(config), f"{template}:{name} not recognised"

    def test_every_bundled_profile_entry_is_recognised(self):
        entries = _shipped_profiles()
        assert entries, "the bundled profiles declare no MCP server"
        for profile, name, config in entries:
            assert config["command"] == CAO_MCP_SERVER_COMMAND, f"{profile}:{name} changed shape"
            assert is_bundled_cao_server(config), f"{profile}:{name} not recognised"

    @pytest.mark.parametrize("source", [_shipped_templates, _shipped_profiles])
    def test_every_shipped_entry_gains_the_coordinates_at_runtime(
        self, source, monkeypatch, default_cao_server
    ):
        monkeypatch.setenv("CAO_API_PORT", "9777")
        for origin, name, config in source():
            out = resolve_mcp_server_config(config)
            assert out["env"]["CAO_API_PORT"] == "9777", f"{origin}:{name}"


class TestNearMissesGainNoCoordinates:
    """End to end: a near-miss goes through the resolver and gains NOTHING.

    The predicate is the gate, but the leak the review found was in the
    resolved entry, so that is what these assert.
    """

    NEAR_MISSES = [
        {"command": "uvx", "args": ["--from", "some-other-package", "cao-mcp-server"]},
        {"command": "my-server", "args": ["--helper", "cao-mcp-server"]},
        {"command": "my-server", "args": ["--module", CAO_MCP_SERVER_MODULE]},
    ]

    def test_all_three_coordinates_are_withheld(self, monkeypatch, default_cao_server):
        monkeypatch.setenv("CAO_API_HOST", "10.0.0.4")
        monkeypatch.setenv("CAO_API_PORT", "9777")
        monkeypatch.setenv("CAO_HOME_DIR", "/srv/cao-trial")
        for entry in self.NEAR_MISSES:
            out = resolve_mcp_server_config(dict(entry))
            assert "env" not in out, entry
            assert out == entry, entry


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
