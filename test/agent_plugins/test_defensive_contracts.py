"""Defensive contracts on the plugin path — each asserts a stated guarantee.

These are the branches that exist because plugin content is untrusted and CAO's
own state can be edited by hand: an uncanonicalizable path, a manifest entry of
the wrong JSON type, a corrupt timestamp in an install record. Every one of them
has a documented "degrade, do not raise" contract, and a contract nothing
exercises is a contract nothing holds anyone to.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import containment
from cli_agent_orchestrator.agent_plugins.mcp_delivery import (
    McpDeliveryResult,
    log_delivery_findings,
    with_plugin_mcp,
)
from cli_agent_orchestrator.agent_plugins.mcp_mapping import map_mcp_config
from cli_agent_orchestrator.agent_plugins.models import (
    Finding,
    PluginRecord,
    Severity,
)
from cli_agent_orchestrator.agent_plugins.resolver import ResolverError, _apply_subdir
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin
from cli_agent_orchestrator.utils.opencode_config import entry_within_roots


class TestContainmentDegradesRatherThanRaising:
    def test_an_uncanonicalizable_root_yields_none(self, tmp_path, monkeypatch):
        """No root means no containment decision — never a false 'contained'."""
        monkeypatch.setattr(containment, "canonical_root", lambda _root: None)
        assert containment.resolve_within_root(tmp_path, "anything") is None

    def test_a_candidate_that_cannot_be_canonicalized_yields_none(self, tmp_path, monkeypatch):
        """An OSError while resolving must not propagate into install."""
        import os

        real = os.path.realpath

        def explode(path, *a, **k):
            if "boom" in str(path):
                raise OSError("cannot resolve")
            return real(path, *a, **k)

        monkeypatch.setattr(os.path, "realpath", explode)
        assert containment.resolve_within_root(tmp_path, "./boom") is None

    def test_a_path_inside_the_root_still_resolves(self, tmp_path):
        """The negative cases above must not have made everything None."""
        (tmp_path / "inside").mkdir()
        assert containment.resolve_within_root(tmp_path, "./inside") is not None


class TestRecordAndModelSerialization:
    def test_a_corrupt_installed_at_falls_back_to_the_epoch(self):
        """A hand-edited record must load, not explode `cao plugin list`."""
        base = {
            "name": "demo",
            "version": "1.0.0",
            "source": {"kind": "path", "location": "/somewhere"},
            "resolved_ref": None,
            "schema_id": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
            "skill_names": ["alpha"],
        }
        for bad in ("not-a-date", None, "", "2026-13-45T99:99:99"):
            record = PluginRecord.from_dict({**base, "installed_at": bad})
            assert record.installed_at == datetime.fromtimestamp(0, tz=timezone.utc)

    def test_a_valid_installed_at_is_preserved(self):
        stamp = "2026-08-08T12:00:00+00:00"
        record = PluginRecord.from_dict(
            {
                "name": "demo",
                "version": None,
                "source": {"kind": "path", "location": "/x"},
                "resolved_ref": None,
                "installed_at": stamp,
                "schema_id": "s",
                "skill_names": [],
            }
        )
        assert record.installed_at == datetime.fromisoformat(stamp)

    def test_author_and_server_config_round_trip_through_to_dict(self):
        """`to_dict` is what the API and CLI render; assert the shape."""
        from cli_agent_orchestrator.agent_plugins.models import Author, MappedServer

        assert Author(name="A", email="a@example.test", url=None).to_dict() == {
            "name": "A",
            "email": "a@example.test",
            "url": None,
        }
        cfg = MappedServer(name="srv", config={"command": "x"})
        assert cfg.to_dict() == {"name": "srv", "config": {"command": "x"}}


class TestDeliveryResultAndLogging:
    def test_server_names_are_sorted(self):
        result = McpDeliveryResult(servers={"zulu": {}, "alpha": {}, "mike": {}})
        assert result.server_names == ("alpha", "mike", "zulu")

    def test_a_quiet_finding_logs_at_debug_and_a_loud_one_at_warning(self, caplog):
        """The severity split is the operator's signal; assert both sides."""
        loud = Finding(
            severity=Severity.WARNING,
            code="mcp_delivery.opencode_config_collision",
            spec_ref="§9",
            message="loud-message",
            path="mcp.json",
        )
        quiet = Finding(
            severity=Severity.SKIPPED,
            code="mcp.transport_unsupported",
            spec_ref="§7.2.1",
            message="quiet-message",
            path="mcp.json",
        )
        with caplog.at_level(logging.DEBUG):
            log_delivery_findings(McpDeliveryResult(findings=(loud, quiet)), agent_name="a")

        seen = [(r.getMessage(), r.levelno) for r in caplog.records]
        assert any("loud-message" in m and lv == logging.WARNING for m, lv in seen), seen
        assert any("quiet-message" in m and lv == logging.DEBUG for m, lv in seen), seen


class TestWithPluginMcpNeverBreaksALaunch:
    def test_a_none_profile_is_returned_unchanged(self):
        assert with_plugin_mcp(None, "claude_code") is None

    def test_a_failing_merge_returns_the_profile_instead_of_raising(self, monkeypatch, caplog):
        """A degraded launch beats no launch: terminal creation must not fail here."""
        from cli_agent_orchestrator.agent_plugins import mcp_delivery

        monkeypatch.setattr(
            mcp_delivery,
            "apply_plugin_mcp_servers",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("store unreadable")),
        )

        class P:
            name = "worker"
            mcpServers = {"own": {"command": "x"}}

        profile = P()
        with caplog.at_level(logging.WARNING):
            returned = with_plugin_mcp(profile, "codex")

        assert returned is profile
        assert profile.mcpServers == {"own": {"command": "x"}}, "the profile was mutated on failure"
        assert any("agent-plugin MCP" in r.getMessage() for r in caplog.records)


class TestMalformedMcpEntriesAreSkippedWithAFinding:
    @pytest.fixture
    def roots(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        data = tmp_path / "data"
        data.mkdir()
        return root, data

    def _doc(self, servers):
        return {
            "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
            "mcpServers": servers,
        }

    @pytest.mark.parametrize("entry", ["a string", 42, ["a", "list"], None, True])
    def test_an_entry_of_the_wrong_type_is_skipped(self, roots, entry):
        root, data = roots
        result = map_mcp_config(root, data, self._doc({"bad": entry}), provider="claude_code")
        assert result.servers == ()
        assert result.findings, "a skipped entry must be reported, not dropped silently"

    @pytest.mark.parametrize("command", [None, "", 42, ["not", "a", "token"], {}])
    def test_a_stdio_entry_without_a_usable_command_is_skipped(self, roots, command):
        """§7.2.1: `command` is a single non-empty token."""
        root, data = roots
        result = map_mcp_config(
            root, data, self._doc({"srv": {"type": "stdio", "command": command}}), provider="codex"
        )
        assert result.servers == ()
        assert result.findings

    def test_a_well_formed_stdio_entry_still_maps(self, roots):
        root, data = roots
        result = map_mcp_config(
            root, data, self._doc({"srv": {"type": "stdio", "command": "demo"}}), provider="codex"
        )
        assert [s.name for s in result.servers] == ["srv"]

    def test_nul_byte_paths_skip_only_affected_entries(self, roots):
        """A hostile NUL byte in `./command` or `cwd` must isolate to that entry (§7.2.2.2)."""
        root, data = roots
        doc = self._doc(
            {
                "bad-cmd": {"type": "stdio", "command": "./bin/run\x00evil"},
                "bad-cwd": {"type": "stdio", "command": "demo", "cwd": "./sub\x00dir"},
                "bad-data-cwd": {
                    "type": "stdio",
                    "command": "demo",
                    "cwd": "${PLUGIN_DATA}/sub\x00dir",
                },
                "good": {"type": "stdio", "command": "demo"},
            }
        )
        result = map_mcp_config(root, data, doc, provider="codex")
        assert [s.name for s in result.servers] == ["good"]
        codes = [f.code for f in result.findings]
        assert codes.count("mcp.command_escapes_root") == 1
        assert codes.count("mcp.cwd_escapes_root") == 2

    def test_validate_plugin_preserves_skills_when_mcp_has_nul_byte_paths(self, tmp_path):
        """A NUL byte in mcp.json must not trip the FATAL internal.error backstop."""
        plugin_root = tmp_path / "plugin"
        (plugin_root / "skills" / "demo").mkdir(parents=True)
        (plugin_root / "plugin.json").write_text(
            json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                    "name": "demo",
                    "version": "1.0.0",
                    "description": "demo plugin",
                }
            ),
            encoding="utf-8",
        )
        (plugin_root / "skills" / "demo" / "SKILL.md").write_text(
            "---\nname: demo\ndescription: demo skill\n---\nbody\n",
            encoding="utf-8",
        )
        (plugin_root / "mcp.json").write_text(
            json.dumps(
                self._doc(
                    {
                        "bad-cmd": {"type": "stdio", "command": "./bin/run\x00evil"},
                        "good": {"type": "stdio", "command": "demo"},
                    }
                )
            ),
            encoding="utf-8",
        )

        report = validate_plugin(plugin_root)
        assert report.loadable is True
        assert [s.name for s in report.skills] == ["demo"]
        assert [s.name for s in report.mcp_servers] == ["good"]
        assert any(f.code == "mcp.command_escapes_root" for f in report.findings)
        assert all(f.code != "internal.error" for f in report.findings)

    def test_validate_plugin_reports_root_invalid_for_nul_byte_root(self, tmp_path):
        report = validate_plugin(Path(f"{tmp_path}/bad\x00root"))
        assert report.loadable is False
        assert [f.code for f in report.findings] == ["plugin.root_invalid"]

    def test_resolver_apply_subdir_rejects_nul_byte_with_resolver_error(self, tmp_path):
        with pytest.raises(ResolverError, match="escapes the source tree"):
            _apply_subdir(tmp_path, "sub\x00dir")


class TestEntryWithinRootsToleratesUnusablePaths:
    @pytest.mark.parametrize(
        "config",
        [
            {"command": ["\x00not-a-path"]},
            {"command": [None]},
            {"command": [42]},
        ],
    )
    def test_an_unconstructable_path_is_skipped_not_raised(self, tmp_path, config):
        """A hand-edited opencode.json must not crash the ownership check."""
        assert entry_within_roots(config, [tmp_path]) is False

    def test_a_real_in_root_command_is_still_detected(self, tmp_path):
        target = tmp_path / "plugins" / "demo" / "server"
        assert entry_within_roots({"command": [str(target)]}, [tmp_path / "plugins"]) is True
