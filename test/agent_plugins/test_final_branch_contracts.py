"""Final branch contracts, patched at the real import sites.

A note on method, because the first attempt at several of these was wrong in a way
worth recording: ``affected_sessions`` imports ``list_sessions``,
``list_terminals_by_session`` and ``load_agent_profile`` **inside the function
body**, so ``monkeypatch.setattr(installer, "list_sessions", ...)`` creates a new
module attribute that the real code never reads. With ``raising=False`` that is
silent — the test passes and asserts nothing. Everything here patches the module
the function actually imports from.

Two branches are also exercised through the private mapper on purpose:
``_map_entry``'s type and command guards sit *behind* whole-document schema
validation, so a public call can never reach them. They are defence in depth, and
testing them directly is the only way to hold them to their contract.
"""

from __future__ import annotations

import errno
import logging
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins.installer import PluginInstallError, affected_sessions
from cli_agent_orchestrator.agent_plugins.mcp_mapping import _map_entry
from cli_agent_orchestrator.agent_plugins.store import _rmtree_quiet, _rmtree_reporting

from .conftest import build_plugin
from .test_store import make_record

SESSION_SRC = "cli_agent_orchestrator.services.session_service.list_sessions"
TERMINALS_SRC = "cli_agent_orchestrator.clients.database.list_terminals_by_session"
PROFILE_SRC = "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile"


class TestAffectedSessionsWalksRealLiveState:
    @pytest.fixture
    def claimed(self, store):
        store.write_record(make_record("demo", projected_skill_names=("alpha",)))
        return store

    def test_a_terminal_with_no_profile_is_skipped(self, claimed, monkeypatch):
        monkeypatch.setattr(SESSION_SRC, lambda: [{"name": "s1"}])
        monkeypatch.setattr(
            TERMINALS_SRC, lambda _s: [{"id": "t1"}, {"id": "t2", "agent_profile": ""}]
        )

        assert affected_sessions("demo", store=claimed) == []

    def test_a_terminal_whose_profile_vanished_is_skipped(self, claimed, monkeypatch):
        """Unresolvable filter: the profile is gone, so nothing can be asserted."""
        monkeypatch.setattr(SESSION_SRC, lambda: [{"name": "s1"}])
        monkeypatch.setattr(TERMINALS_SRC, lambda _s: [{"id": "t1", "agent_profile": "vanished"}])
        monkeypatch.setattr(
            PROFILE_SRC, lambda _n: (_ for _ in ()).throw(FileNotFoundError("vanished"))
        )

        assert affected_sessions("demo", store=claimed) == []

    def test_a_terminal_using_the_skill_is_reported(self, claimed, monkeypatch):
        """The positive case — otherwise the two negatives above prove nothing."""

        class Profile:
            name = "worker"
            skills = None

        monkeypatch.setattr(SESSION_SRC, lambda: [{"name": "s1"}])
        monkeypatch.setattr(TERMINALS_SRC, lambda _s: [{"id": "t1", "agent_profile": "worker"}])
        monkeypatch.setattr(PROFILE_SRC, lambda _n: Profile())

        affected = affected_sessions("demo", store=claimed)
        assert [a.terminal_id for a in affected] == ["t1"]

    def test_the_same_profile_is_resolved_once_across_terminals(self, claimed, monkeypatch):
        """The filter cache is why this route is affordable for a polled panel."""
        calls = []

        class Profile:
            name = "worker"
            skills = None

        monkeypatch.setattr(SESSION_SRC, lambda: [{"name": "s1"}])
        monkeypatch.setattr(
            TERMINALS_SRC,
            lambda _s: [{"id": f"t{i}", "agent_profile": "worker"} for i in range(3)],
        )

        def counting(name):
            calls.append(name)
            return Profile()

        monkeypatch.setattr(PROFILE_SRC, counting)

        affected_sessions("demo", store=claimed)
        assert calls == ["worker"], f"profile resolved {len(calls)} times"


class TestUninstallWrapsStoreValueErrors:
    def test_a_value_error_from_unpublish_becomes_an_install_error(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        """Reached only when unpublish itself rejects — not the earlier guards."""
        from cli_agent_orchestrator.agent_plugins.installer import uninstall

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))
        monkeypatch.setattr(
            type(store),
            "unpublish",
            lambda self, name, purge_data=False: (_ for _ in ()).throw(ValueError("unsafe name")),
        )

        with pytest.raises(PluginInstallError, match="unsafe name"):
            uninstall("demo", store=store, skills_dir=skills_dir, refresh_agents=False)


class TestPerEntryDefensiveGuards:
    """Behind schema validation, so exercised through the private mapper."""

    def _args(self, tmp_path):
        root = tmp_path / "root"
        root.mkdir()
        data = tmp_path / "data"
        data.mkdir()
        return dict(
            root_str=str(root),
            data_str=str(data),
            root=root,
            data_dir=data,
            allowed_transports=frozenset({"stdio", "streamable-http", "sse"}),
        )

    @pytest.mark.parametrize("entry", ["a string", 42, ["a", "list"], None, True])
    def test_a_non_object_entry_is_skipped_with_a_finding(self, tmp_path, entry):
        mapped, findings = _map_entry(name="bad", entry=entry, **self._args(tmp_path))

        assert mapped is None
        assert [f.code for f in findings] == ["mcp.server_invalid"]

    @pytest.mark.parametrize("command", [None, "", 42, ["not", "a", "token"], {}])
    def test_a_stdio_entry_without_a_single_command_token_is_skipped(self, tmp_path, command):
        """§7.2.1: `command` is a single non-empty string."""
        mapped, findings = _map_entry(
            name="srv", entry={"type": "stdio", "command": command}, **self._args(tmp_path)
        )

        assert mapped is None
        assert findings and all(f.spec_ref for f in findings)

    def test_a_well_formed_entry_still_maps(self, tmp_path):
        mapped, findings = _map_entry(
            name="srv", entry={"type": "stdio", "command": "demo"}, **self._args(tmp_path)
        )
        assert mapped is not None and mapped.name == "srv"


class TestDeletionRaceBranches:
    def test_quiet_deletion_tolerates_a_file_vanishing_mid_call(self, tmp_path, monkeypatch):
        """Another process removing it first is success, not an error."""
        target = tmp_path / "f.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(FileNotFoundError())
        )
        _rmtree_quiet(target)  # must not raise

    def test_reporting_deletion_tolerates_a_file_vanishing_mid_call(self, tmp_path, monkeypatch):
        target = tmp_path / "g.txt"
        target.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(FileNotFoundError())
        )
        _rmtree_reporting(target, what="a file")  # absence is success


class TestResolverRemainingPaths:
    def test_a_clone_with_no_resolvable_head_still_resolves(self, tmp_path, monkeypatch):
        """Odd but not fatal: the tree is usable, the commit is simply unknown."""
        from cli_agent_orchestrator.agent_plugins import resolver as rmod

        staged_marker = {}

        def fake_run(args, *, what, cwd=None, **k):
            if args[:1] == ["clone"]:
                dest = Path(args[-1])
                build_plugin(dest, "demo", skills=["alpha"])
                staged_marker["cloned"] = True
                return ""
            if args[:1] == ["rev-parse"]:
                raise rmod.ResolverError("no HEAD")
            return ""

        monkeypatch.setattr(rmod, "_run_git", fake_run)
        resolved = rmod.resolve(
            rmod.PluginSource(kind="git", location="https://example.test/x.git"),
            dest=tmp_path / "dest",
        )

        assert staged_marker.get("cloned")
        assert resolved.resolved_ref is None


class TestPluginCliRemainingOutput:
    def _invoke(self, args, monkeypatch, **patches):
        from cli_agent_orchestrator.cli.commands import agent_plugin as mod

        for name, value in patches.items():
            monkeypatch.setattr(mod, name, value, raising=False)
        return CliRunner().invoke(mod.agent_plugin, args)

    def test_validate_reports_mcp_presence(self, monkeypatch, tmp_path):
        from cli_agent_orchestrator.agent_plugins.models import PluginValidationReport
        from cli_agent_orchestrator.cli.commands import agent_plugin as mod

        monkeypatch.setattr(
            mod,
            "validate_plugin",
            lambda _p: PluginValidationReport(root=tmp_path, mcp_present=True),
        )
        result = CliRunner().invoke(mod.agent_plugin, ["validate", str(tmp_path)])

        assert result.exit_code == 0, result.output
        assert "mcp.json: present" in result.output

    def test_remove_maps_an_install_error(self, monkeypatch, store, tmp_path):
        """The CLI checks installed-ness first, so the plugin must really exist."""
        from cli_agent_orchestrator.cli.commands import agent_plugin as mod

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))

        monkeypatch.setattr(mod, "InstalledPluginStore", lambda *a, **k: store)
        monkeypatch.setattr(mod, "affected_sessions", lambda *a, **k: [])
        monkeypatch.setattr(
            mod, "uninstall", lambda *a, **k: (_ for _ in ()).throw(PluginInstallError("nope"))
        )

        result = CliRunner().invoke(mod.agent_plugin, ["remove", "demo", "--yes"])

        assert result.exit_code != 0
        assert "nope" in result.output
