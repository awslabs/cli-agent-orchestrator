"""The packaged ``cao-ops`` MCP server — Increment 2.

**Validates: Requirements 19.1–19.7, 20.1–20.4**

Two separable things:

* the **shape of the packaged declaration** — which server, which command,
  which pin — where the failure mode is a manifest whose tools break on first
  call; and
* the **availability contract** the packaged server must honour when the
  operator's ``cao-server`` is not running, where the failure modes are a hang,
  a traceback, or a silently empty result.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import requests

from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

REPO_ROOT = Path(__file__).resolve().parents[2]
OPERATOR_DIR = REPO_ROOT / "agent-plugin" / "cao"
CONTRIBUTOR_DIR = REPO_ROOT / "agent-plugin" / "cao-contributor"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as builder  # noqa: E402

REQUEST = "cli_agent_orchestrator.ops_mcp_server.server.requests.request"


def mcp_document() -> dict:
    return json.loads((OPERATOR_DIR / "mcp.json").read_text(encoding="utf-8"))


def ops_entry() -> dict:
    return mcp_document()["mcpServers"]["cao-ops"]


class TestPackagedServerSelection:
    """Requirement 19.1 — the *ops* server, not the in-session one."""

    def test_the_server_key_is_cao_ops(self):
        assert set(mcp_document()["mcpServers"]) == {"cao-ops"}

    def test_it_invokes_the_ops_console_script(self):
        assert "cao-ops-mcp-server" in ops_entry()["args"]

    def test_it_never_invokes_the_in_session_server(self):
        """``cao-mcp-server`` derives its identity from ``CAO_TERMINAL_ID``.

        A foreign client installing this package has no terminal identity — it
        was not launched by CAO into a CAO-managed terminal — so packaging the
        in-session server would ship a manifest whose orchestration tools raise
        on first call rather than merely degrade.
        """
        assert "cao-mcp-server" not in ops_entry()["args"]
        assert ops_entry().get("command") != "cao-mcp-server"

    def test_the_console_script_the_manifest_invokes_really_exists(self):
        """The invoked entry point is the script name, not FastMCP's display name.

        ``uvx ... cao-ops-mcp-server`` resolves a *console script*, so the check
        reads the installed entry points rather than parsing pyproject.toml —
        that is the thing uvx will actually look up, and it keeps the test off
        ``tomllib``, which is stdlib only from 3.11 while this project supports
        3.10.
        """
        from importlib.metadata import entry_points

        scripts = {entry.name: entry.value for entry in entry_points(group="console_scripts")}

        invoked = ops_entry()["args"][-1]
        assert invoked == "cao-ops-mcp-server"
        assert invoked in scripts, f"{invoked} is not an installed console script"
        assert scripts[invoked].endswith("ops_mcp_server.server:main")

    def test_the_in_session_server_really_does_require_a_terminal_id(self):
        """The premise of the choice above, asserted rather than assumed."""
        source = (
            REPO_ROOT / "src" / "cli_agent_orchestrator" / "mcp_server" / "server.py"
        ).read_text(encoding="utf-8")
        assert "CAO_TERMINAL_ID not set" in source


class TestCommandAndPin:
    """Requirements 19.2–19.5."""

    def test_command_is_the_single_token_uvx(self):
        """§7.2.1 permits nothing richer, and the package cannot bundle a launcher."""
        assert ops_entry()["command"] == "uvx"
        assert " " not in ops_entry()["command"]

    def test_every_other_detail_lives_in_args(self):
        assert ops_entry()["args"][:2] == ["--from", f"cli-agent-orchestrator=={_version()}"]

    def test_the_version_is_pinned_exactly_not_floating(self):
        """An unpinned `--from` lets uvx resolve the latest release at first run."""
        args = ops_entry()["args"]
        assert "cli-agent-orchestrator" not in args  # the bare, unpinned name
        assert any(a.startswith("cli-agent-orchestrator==") for a in args)
        assert not any("git+" in a for a in args)  # @main has the same skew problem
        assert not any(">=" in a or "~=" in a for a in args)

    def test_the_pin_matches_the_manifest_version(self):
        """Requirement 19.4 — one source of truth, one write."""
        manifest = json.loads((OPERATOR_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert f"cli-agent-orchestrator=={manifest['version']}" in ops_entry()["args"]

    def test_the_drift_guard_catches_a_desynced_pin(self, tmp_path, monkeypatch, capsys):
        staging = tmp_path / "agent-plugin"
        staging.mkdir()
        version = builder.package_version()
        for config in builder.PACKAGES:
            builder.build_package(config, version, staging)

        mcp_path = staging / "cao" / "mcp.json"
        doc = json.loads(mcp_path.read_text(encoding="utf-8"))
        doc["mcpServers"]["cao-ops"]["args"] = [
            "--from",
            "cli-agent-orchestrator==0.0.1",
            "cao-ops-mcp-server",
        ]
        mcp_path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

        monkeypatch.setattr(builder, "PACKAGES_DIR", staging)
        assert builder.main(["--check"]) == 1
        assert "pin" in capsys.readouterr().err

    def test_an_unpublished_version_fails_the_build(self, monkeypatch):
        """Requirement 19.5 — never write a pin uvx cannot resolve."""
        monkeypatch.setattr(builder, "package_version", lambda: "0.0.0-never-published")

        with pytest.raises(builder.BuildError, match="not published"):
            builder.verify_published("0.0.0-never-published")

    def test_an_unreachable_pypi_also_fails_rather_than_writing_unverified(self, monkeypatch):
        """ "Could not check" is not "verified"."""
        import urllib.error

        def unreachable(*args, **kwargs):
            raise urllib.error.URLError("network down")

        monkeypatch.setattr(builder.urllib.request, "urlopen", unreachable)

        with pytest.raises(builder.BuildError, match="Could not reach PyPI"):
            builder.verify_published("2.4.1")


class TestNoCredentialsInPackageData:
    """Requirements 19.6, 22.5 — §7.2.1/§9.2 forbid them outright."""

    def test_the_entry_declares_no_env(self):
        assert "env" not in ops_entry()

    def test_the_entry_declares_no_headers(self):
        assert "headers" not in ops_entry()

    def test_nothing_credential_shaped_appears_anywhere_in_the_document(self):
        raw = json.dumps(mcp_document()).lower()
        for hint in ("token", "secret", "password", "api_key", "authorization"):
            assert hint not in raw


class TestSchemaVersionAgreement:
    """Requirement 19.7 / §7.2.2.2."""

    def test_mcp_and_plugin_target_the_same_specification_version(self):
        manifest = json.loads((OPERATOR_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert builder._schema_version(mcp_document()["$schema"]) == builder._schema_version(
            manifest["$schema"]
        )

    def test_the_mcp_schema_id_is_the_pinned_one(self):
        assert mcp_document()["$schema"].endswith("/1.0.0/mcp.schema.json")

    def test_the_package_validates_and_maps_the_server(self):
        report = validate_plugin(OPERATOR_DIR)
        assert report.loadable
        assert [server.name for server in report.mcp_servers] == ["cao-ops"]

    def test_the_contributor_package_ships_no_mcp_json_in_either_increment(self):
        """Requirement 2.5 — authoring skills need no CAO runtime at all."""
        assert not (CONTRIBUTOR_DIR / "mcp.json").exists()
        assert validate_plugin(CONTRIBUTOR_DIR).mcp_present is False


class TestApiServerAvailabilityContract:
    """Requirement 20 — what happens when ``cao-server`` is not running."""

    @pytest.mark.asyncio
    async def test_a_connection_failure_returns_a_structured_error(self):
        """Requirement 20.1 — names the operation and the underlying cause."""
        from cli_agent_orchestrator.ops_mcp_server.server import list_profiles

        with patch(REQUEST, side_effect=requests.ConnectionError("Connection refused")):
            result = await list_profiles()

        assert result.success is False
        assert "List profiles" in result.message  # the operation
        assert "Connection refused" in result.message  # the cause

    @pytest.mark.asyncio
    async def test_it_never_returns_a_silent_empty_result(self):
        """Requirement 20.3 — an empty list would read as "no profiles exist"."""
        from cli_agent_orchestrator.ops_mcp_server.server import list_profiles

        with patch(REQUEST, side_effect=requests.ConnectionError("Connection refused")):
            result = await list_profiles()

        assert result.success is False
        assert result.profiles in (None, [])
        assert result.message  # never empty

    @pytest.mark.asyncio
    async def test_the_error_is_returned_not_raised(self):
        """A traceback would reach the calling agent as a tool crash."""
        from cli_agent_orchestrator.ops_mcp_server.server import list_sessions

        with patch(REQUEST, side_effect=requests.ConnectionError("refused")):
            result = await list_sessions()  # must not raise

        assert result.success is False

    def test_every_request_is_bounded_by_a_timeout(self):
        """Requirement 20.2 — a wedged listener must not hang the tool forever."""
        from cli_agent_orchestrator.ops_mcp_server.server import _HTTP_TIMEOUT, _request_json

        captured = {}

        def capture(*args, **kwargs):
            captured.update(kwargs)
            raise requests.ConnectionError("refused")

        with patch(REQUEST, side_effect=capture):
            data, error = _request_json("get", "/health", operation="Probe")

        assert captured["timeout"] == _HTTP_TIMEOUT
        assert all(bound > 0 for bound in _HTTP_TIMEOUT)
        assert data is None and "Probe failed" in error

    @pytest.mark.asyncio
    async def test_a_timeout_produces_the_same_structured_error(self):
        """A timeout is a RequestException, so the contract is identical."""
        from cli_agent_orchestrator.ops_mcp_server.server import list_profiles

        with patch(REQUEST, side_effect=requests.Timeout("timed out")):
            result = await list_profiles()

        assert result.success is False
        assert "List profiles failed" in result.message

    def test_the_server_never_starts_a_cao_api_server(self):
        """Requirement 20.4 — self-start is rejected, not merely unimplemented.

        A plugin-launched subprocess spawning a long-lived HTTP server on a
        fixed local port would contradict the user-controlled-server posture the
        package documents: the operator decides when CAO is listening.
        """
        import ast

        module = REPO_ROOT / "src" / "cli_agent_orchestrator" / "ops_mcp_server" / "server.py"
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))

        # Parsed, not grepped: the module's prose legitimately discusses
        # `cao-server` and process launching, and a substring match would flag
        # the very comments that explain why it does not do it.
        forbidden_modules = {"subprocess", "multiprocessing", "uvicorn", "pty"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_modules, alias.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_modules, node.module
            elif isinstance(node, ast.Attribute):
                assert node.attr not in {
                    "system",
                    "popen",
                    "fork",
                    "execv",
                    "execvp",
                    "spawnv",
                }, node.attr


class TestDocumentedPrerequisites:
    """Requirement 1.5 / AC7 — surfaced where an installing operator sees them."""

    def test_the_manifest_description_states_the_uv_prerequisite(self):
        manifest = json.loads((OPERATOR_DIR / "plugin.json").read_text(encoding="utf-8"))
        assert "uv" in manifest["description"]
        assert "uvx" in manifest["description"]

    def test_the_docs_state_them_too(self):
        docs = (REPO_ROOT / "docs" / "agent-plugins.md").read_text(encoding="utf-8")
        assert "uv" in docs
        assert "http://127.0.0.1:9889" in docs
        assert "localhost-only" in docs

    def test_the_docs_state_that_cao_never_self_starts(self):
        docs = (REPO_ROOT / "docs" / "agent-plugins.md").read_text(encoding="utf-8")
        lowered = docs.lower()
        assert "not start a server" in lowered or "self-start" in lowered


def _version() -> str:
    return builder.package_version()
