"""The shim gets its token from a FILE when the value cannot be written (#802).

Three mechanisms were tried, and the first two each failed one way:

1. the token value in the MCP config — readable wherever that config lands, which
   for codex/kimi/copilot is argv, readable by every local process;
2. omitting it and relying on the child inheriting the parent environment — not
   portable: Kimi does not pass its environment to MCP subprocesses, so the shim
   exited at startup and every forwarded tool call failed;
3. a path to an owner-only file — nothing readable in argv or in a persisted
   config, and no dependency on inheritance.

This covers (3), including that (1) does not regress and that the failure posture
for a missing credential is still fatal-and-legible rather than silent.
"""

import json
import os
import pathlib

import pytest

from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    RUNTIME_TOKEN_ENV,
    RUNTIME_TOKEN_FILE_ENV,
    SHARED_ENDPOINT_URL_ENV,
    resolve_mcp_server_config,
)

TOKEN = "secret-channel-token"
ENDPOINT = "http://cao-server:9891/mcp"


@pytest.fixture(autouse=True)
def _endpoint(monkeypatch, tmp_path):
    # Patch the RESOLVED constant, not the env var: constants.CAO_HOME_DIR is
    # computed at import, so setenv here left the test writing its token file into
    # the developer's real ~/.cao/tmp. The assertions still passed, which is why the
    # broken isolation was invisible.
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.CAO_HOME_DIR", str(tmp_path), raising=False
    )
    monkeypatch.setenv(SHARED_ENDPOINT_URL_ENV, ENDPOINT)
    monkeypatch.setenv(RUNTIME_TOKEN_ENV, TOKEN)
    # The path is cached per process; clear it so each test materializes its own.
    import cli_agent_orchestrator.utils.mcp_resolution as mr

    monkeypatch.setattr(mr, "_TOKEN_FILE_PATH", "")


def _omitting_config():
    return resolve_mcp_server_config(
        {"command": CAO_MCP_SERVER_COMMAND, "args": []}, omit_token=True
    )


class TestTheTokenTravelsAsAPathNotAValue:
    def test_the_config_carries_a_path_and_not_the_secret(self):
        cfg = _omitting_config()
        assert RUNTIME_TOKEN_FILE_ENV in cfg["env"]
        assert RUNTIME_TOKEN_ENV not in cfg["env"]
        assert TOKEN not in json.dumps(cfg), "the token value reached the config"

    def test_the_file_is_owner_only(self):
        path = pathlib.Path(_omitting_config()["env"][RUNTIME_TOKEN_FILE_ENV])
        assert path.read_text() == TOKEN
        assert oct(path.stat().st_mode & 0o777) == "0o600"

    def test_the_endpoint_is_still_supplied(self):
        """Omitting the credential must not omit the destination."""
        assert _omitting_config()["env"][SHARED_ENDPOINT_URL_ENV] == ENDPOINT

    def test_the_path_is_stable_across_resolutions(self):
        """A persisted config must stay valid when the provider relaunches."""
        first = _omitting_config()["env"][RUNTIME_TOKEN_FILE_ENV]
        assert _omitting_config()["env"][RUNTIME_TOKEN_FILE_ENV] == first


class TestTheShimReadsItWithoutInheritance:
    def test_the_shim_authenticates_from_the_file_alone(self, monkeypatch):
        """The Kimi case: no inherited token, only the file variable."""
        path = _omitting_config()["env"][RUNTIME_TOKEN_FILE_ENV]
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        monkeypatch.setenv(RUNTIME_TOKEN_FILE_ENV, path)
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

        from cli_agent_orchestrator.mcp_server.stdio_bridge import build_forward_headers

        assert any(TOKEN in str(v) for v in build_forward_headers().values())

    def test_an_inherited_value_still_wins_when_present(self, monkeypatch):
        """Providers that DO pass their environment keep working unchanged."""
        monkeypatch.setenv(RUNTIME_TOKEN_ENV, "inherited-tok")
        monkeypatch.delenv(RUNTIME_TOKEN_FILE_ENV, raising=False)
        monkeypatch.setenv("CAO_TERMINAL_ID", "abcd1234")

        from cli_agent_orchestrator.mcp_server.stdio_bridge import build_forward_headers

        assert any("inherited-tok" in str(v) for v in build_forward_headers().values())

    def test_neither_source_is_fatal_and_says_so(self, monkeypatch):
        """Fail closed and legibly, not a silent unauthenticated start."""
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        monkeypatch.delenv(RUNTIME_TOKEN_FILE_ENV, raising=False)

        from cli_agent_orchestrator.mcp_server.stdio_bridge import build_forward_headers

        with pytest.raises(SystemExit) as exc:
            build_forward_headers()
        assert RUNTIME_TOKEN_FILE_ENV in str(exc.value)

    def test_an_unreadable_file_is_fatal_not_silent(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        monkeypatch.setenv(RUNTIME_TOKEN_FILE_ENV, "/nonexistent/runtime-token")

        from cli_agent_orchestrator.mcp_server.stdio_bridge import build_forward_headers

        with pytest.raises(SystemExit) as exc:
            build_forward_headers()
        assert "could not read" in str(exc.value)


class TestAProfileCannotChooseTheTokenFile:
    """The path is operator state, so a profile must not be able to name it.

    `extra` normally clobbers the key, but when no token is configured
    `_materialize_token_file` returns "" and there is nothing to clobber with — so a
    profile-planted `CAO_RUNTIME_TOKEN_FILE` survived into the persisted config and
    the shim would read its credential from an attacker-chosen path. Found by review
    of the token-file change itself.
    """

    HOSTILE = "/tmp/attacker-supplied-token"

    def test_a_planted_path_is_stripped_when_no_token_is_configured(self, monkeypatch):
        monkeypatch.delenv(RUNTIME_TOKEN_ENV, raising=False)
        import cli_agent_orchestrator.utils.mcp_resolution as mr

        monkeypatch.setattr(mr, "_TOKEN_FILE_PATH", "")

        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {RUNTIME_TOKEN_FILE_ENV: self.HOSTILE, "keep": "yes"},
            },
            omit_token=True,
        )
        assert RUNTIME_TOKEN_FILE_ENV not in resolved["env"]
        assert resolved["env"]["keep"] == "yes"

    def test_a_planted_path_loses_to_the_real_one(self):
        resolved = resolve_mcp_server_config(
            {
                "command": CAO_MCP_SERVER_COMMAND,
                "args": [],
                "env": {RUNTIME_TOKEN_FILE_ENV: self.HOSTILE},
            },
            omit_token=True,
        )
        assert resolved["env"][RUNTIME_TOKEN_FILE_ENV] != self.HOSTILE
        assert pathlib.Path(resolved["env"][RUNTIME_TOKEN_FILE_ENV]).read_text() == TOKEN

    def test_a_rotated_token_refreshes_the_file(self, monkeypatch):
        """Caching on existence alone served a stale credential after rotation."""
        first = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": []}, omit_token=True
        )["env"][RUNTIME_TOKEN_FILE_ENV]
        assert pathlib.Path(first).read_text() == TOKEN

        monkeypatch.setenv(RUNTIME_TOKEN_ENV, "rotated-token")
        second = resolve_mcp_server_config(
            {"command": CAO_MCP_SERVER_COMMAND, "args": []}, omit_token=True
        )["env"][RUNTIME_TOKEN_FILE_ENV]
        assert pathlib.Path(second).read_text() == "rotated-token"
