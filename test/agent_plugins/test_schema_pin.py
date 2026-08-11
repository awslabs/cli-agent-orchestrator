"""Schema pin tests — correctness property P11.

**Validates: Requirements 4.2, 4.3, 4.4, 4.5**

§5.2 forbids retrieving a schema while loading a plugin. Asserting that by
inspection is not enough, so the offline test here **blocks socket creation
process-wide** and then validates a real plugin: if any code path tried to fetch
a schema, the test fails with a connection error rather than passing quietly.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.validation import (
    MCP_SCHEMA_FILENAME,
    PLUGIN_SCHEMA_FILENAME,
    load_pinned_schema,
    supported_schema_id,
    validate_plugin,
)

from .conftest import PLUGIN_SCHEMA_ID, build_plugin

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "schemas" / "agent_plugins" / "1.0.0"
PIN_PATH = SCHEMA_DIR / "PIN.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


class TestPinIntegrity:
    def test_pin_file_exists_and_records_both_schemas(self):
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
        assert pin["version"] == "1.0.0"
        assert set(pin["files"]) == {PLUGIN_SCHEMA_FILENAME, MCP_SCHEMA_FILENAME}

    @pytest.mark.parametrize("filename", [PLUGIN_SCHEMA_FILENAME, MCP_SCHEMA_FILENAME])
    def test_vendored_bytes_hash_to_the_recorded_value(self, filename):
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
        actual = hashlib.sha256((SCHEMA_DIR / filename).read_bytes()).hexdigest()
        assert actual == pin["files"][filename]["sha256"]

    def test_pin_records_a_source_url_for_each_file(self):
        """Requirement 4.2: a recorded source reference accompanies the hash."""
        pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
        for entry in pin["files"].values():
            assert entry["url"].startswith("https://agent-plugins.org/schemas/1.0.0/")

    def test_mcp_schema_is_committed_for_increment_two(self):
        """Committed in Increment 1, unused until the MCP mapper lands."""
        assert (SCHEMA_DIR / MCP_SCHEMA_FILENAME).is_file()
        assert supported_schema_id(MCP_SCHEMA_FILENAME).endswith("mcp.schema.json")

    def test_vendored_plugin_schema_is_closed(self):
        """The manifest is closed (§5.2); the tolerances are applied in code."""
        schema = load_pinned_schema(PLUGIN_SCHEMA_FILENAME)
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["$schema", "name"]


class TestDriftGuard:
    """Requirement 4.3 — the ``--check`` mode CI runs on every PR."""

    def test_check_passes_against_the_committed_tree(self):
        import vendor_agent_plugins_schemas as vendor

        assert vendor.main(["--check"]) == 0

    def test_check_fails_on_a_hash_mismatch(self, monkeypatch, tmp_path):
        import vendor_agent_plugins_schemas as vendor

        # Copy the pin and schemas into a scratch tree, then tamper with one byte.
        scratch = tmp_path / "1.0.0"
        scratch.mkdir(parents=True)
        for name in (PLUGIN_SCHEMA_FILENAME, MCP_SCHEMA_FILENAME):
            (scratch / name).write_bytes((SCHEMA_DIR / name).read_bytes())
        (scratch / "PIN.json").write_bytes(PIN_PATH.read_bytes())
        (scratch / PLUGIN_SCHEMA_FILENAME).write_text("{}", encoding="utf-8")

        monkeypatch.setattr(vendor, "SCHEMA_DIR", scratch)
        monkeypatch.setattr(vendor, "PIN_PATH", scratch / "PIN.json")

        assert vendor.main(["--check"]) == 1

    def test_check_fails_when_a_schema_is_missing(self, monkeypatch, tmp_path):
        import vendor_agent_plugins_schemas as vendor

        scratch = tmp_path / "1.0.0"
        scratch.mkdir(parents=True)
        (scratch / "PIN.json").write_bytes(PIN_PATH.read_bytes())

        monkeypatch.setattr(vendor, "SCHEMA_DIR", scratch)
        monkeypatch.setattr(vendor, "PIN_PATH", scratch / "PIN.json")

        assert vendor.main(["--check"]) == 1

    def test_check_fails_when_the_pin_itself_is_missing(self, monkeypatch, tmp_path):
        import vendor_agent_plugins_schemas as vendor

        monkeypatch.setattr(vendor, "SCHEMA_DIR", tmp_path)
        monkeypatch.setattr(vendor, "PIN_PATH", tmp_path / "PIN.json")

        assert vendor.main(["--check"]) == 1


class TestOfflineValidation:
    """Requirement 4.4 — validation completes with all sockets blocked."""

    @pytest.fixture
    def no_sockets(self, monkeypatch):
        def _blocked(*args, **kwargs):
            raise AssertionError(
                "Validation attempted a network operation; §5.2 forbids retrieving "
                "a schema while loading a plugin"
            )

        monkeypatch.setattr(socket, "socket", _blocked)
        monkeypatch.setattr(socket, "create_connection", _blocked)
        monkeypatch.setattr(socket, "getaddrinfo", _blocked)
        return None

    def test_valid_plugin_validates_with_sockets_blocked(self, no_sockets, tmp_path):
        # Prime nothing: the caches are populated lazily, so if the schema were
        # fetched rather than read from disk, this is where it would happen.
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("alpha",)

    def test_unknown_schema_is_rejected_offline_rather_than_fetched(self, no_sockets, tmp_path):
        """Requirement 4.5: an unrecognized ``$schema`` is a rejection, not a fetch."""
        root = build_plugin(
            tmp_path / "p", "demo", schema_id="https://example.invalid/plugin.schema.json"
        )
        report = validate_plugin(root)

        assert not report.loadable
        assert any(f.code == "manifest.schema_unsupported" for f in report.findings)


class TestRegistryRefusesRetrieval:
    def test_remote_ref_resolution_is_refused(self):
        """An unexpected ``$ref`` must raise, never open a connection."""
        from referencing.exceptions import Unresolvable

        from cli_agent_orchestrator.agent_plugins.validation import _offline_validator

        validator = _offline_validator(PLUGIN_SCHEMA_FILENAME)
        resolver = validator._resolver  # noqa: SLF001 - asserting the safety property

        with pytest.raises(Unresolvable):
            resolver.lookup("https://example.invalid/some-other.schema.json")

    def test_the_two_pinned_schemas_resolve_locally(self):
        from cli_agent_orchestrator.agent_plugins.validation import _offline_validator

        validator = _offline_validator(PLUGIN_SCHEMA_FILENAME)
        resolver = validator._resolver  # noqa: SLF001

        assert resolver.lookup(PLUGIN_SCHEMA_ID) is not None
