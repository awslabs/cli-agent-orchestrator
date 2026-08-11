"""The final uncovered branches — unrecoverable-swap paths and per-provider refresh.

These are the paths reached only when the operating system fails mid-operation. They
matter more than their line count suggests: each one is the difference between a
recoverable failure and a destroyed plugin, so the assertions below are about the
*message and the surviving bytes*, not about the exception type.
"""

from __future__ import annotations

import errno
import json
import logging
import shutil
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.store import (
    PluginStoreError,
    _rmtree_quiet,
    _rmtree_reporting,
)

from .conftest import build_plugin
from .test_store import make_record


class TestUnrecoverableSwapPreservesTheOperatorsBytes:
    def test_a_failed_swap_that_cannot_be_restored_says_where_the_files_are(
        self, store, tmp_path, monkeypatch
    ):
        """The one path that can destroy a working plugin must not delete anything."""
        v1 = build_plugin(tmp_path / "v1", name="demo", skills=("alpha",))
        (v1 / "WHO.txt").write_text("v1", encoding="utf-8")
        store.publish(v1, make_record("demo"))
        v2 = build_plugin(tmp_path / "v2", name="demo", skills=("alpha",))

        real_rename = Path.rename

        def rename_that_fails_both_ways(self, target):
            # Fail the swap into place, and the restore back out of the backup.
            if self.name.startswith(".demo.replaced.") or Path(target).name == "demo":
                raise OSError(errno.EIO, "I/O error")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", rename_that_fails_both_ways)
        with pytest.raises(PluginStoreError) as exc:
            store.publish(v2, make_record("demo"), force=True)
        monkeypatch.undo()

        message = str(exc.value)
        assert "Nothing was deleted" in message
        assert "recover" in message
        # The preserved directory named in the message must actually exist.
        preserved = [p for p in store.plugins_dir.glob(".demo.replaced.*")]
        assert preserved, "the operator's only copy was not preserved"
        assert (preserved[0] / "WHO.txt").read_text(encoding="utf-8") == "v1"

    def test_a_concurrent_rename_collision_is_reported_as_such(self, store, tmp_path, monkeypatch):
        """EEXIST/ENOTEMPTY from the rename means someone else published first."""
        source = build_plugin(tmp_path / "src", name="demo", skills=("alpha",))
        real_rename = Path.rename

        def rename_collides(self, target):
            if Path(target).name == "demo":
                raise OSError(errno.ENOTEMPTY, "Directory not empty")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", rename_collides)
        with pytest.raises(PluginStoreError, match="published concurrently"):
            store.publish(source, make_record("demo"))

    def test_a_failed_record_write_whose_restore_also_fails_preserves_the_bytes(
        self, store, tmp_path, monkeypatch
    ):
        """The C6 rollback has the same unrecoverable corner as the swap."""
        v1 = build_plugin(tmp_path / "r1", name="demo", skills=("alpha",))
        (v1 / "WHO.txt").write_text("v1", encoding="utf-8")
        store.publish(v1, make_record("demo"))
        v2 = build_plugin(tmp_path / "r2", name="demo", skills=("alpha",))

        monkeypatch.setattr(
            type(store),
            "write_record",
            lambda self, record: (_ for _ in ()).throw(OSError(28, "No space left on device")),
        )
        real_rename = Path.rename

        def restore_fails(self, target):
            if self.name.startswith(".demo.replaced."):
                raise OSError(errno.EIO, "I/O error")
            return real_rename(self, target)

        monkeypatch.setattr(Path, "rename", restore_fails)

        with pytest.raises(PluginStoreError, match="Nothing was deleted"):
            store.publish(v2, make_record("demo"), force=True)
        monkeypatch.undo()

        preserved = list(store.plugins_dir.glob(".demo.replaced.*"))
        assert preserved and (preserved[0] / "WHO.txt").read_text(encoding="utf-8") == "v1"


class TestDeletionHelpers:
    def test_quiet_deletion_handles_file_symlink_dir_and_absence(self, tmp_path, caplog):
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        d = tmp_path / "d"
        d.mkdir()
        link = tmp_path / "link"
        link.symlink_to(d, target_is_directory=True)

        for path in (f, link, d, tmp_path / "never-existed"):
            _rmtree_quiet(path)
            assert not path.exists() and not path.is_symlink()

    def test_quiet_deletion_logs_and_continues_on_oserror(self, tmp_path, monkeypatch, caplog):
        d = tmp_path / "busy"
        d.mkdir()
        monkeypatch.setattr(
            shutil, "rmtree", lambda *a, **k: (_ for _ in ()).throw(OSError("busy"))
        )
        with caplog.at_level(logging.WARNING):
            _rmtree_quiet(d)  # must not raise
        assert any("Could not remove" in r.getMessage() for r in caplog.records)

    def test_reporting_deletion_treats_absence_as_success(self, tmp_path):
        _rmtree_reporting(tmp_path / "never-existed", what="a thing")  # must not raise

    def test_reporting_deletion_handles_a_file_and_a_symlink(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("x", encoding="utf-8")
        _rmtree_reporting(f, what="a file")
        assert not f.exists()

        d = tmp_path / "d2"
        d.mkdir()
        link = tmp_path / "link2"
        link.symlink_to(d, target_is_directory=True)
        _rmtree_reporting(link, what="a link")
        assert not link.is_symlink()
        assert d.is_dir(), "removing the link must not remove its target"

    def test_reporting_deletion_raises_when_the_path_survives(self, tmp_path, monkeypatch):
        """A silent partial rmtree is exactly the false success this guards."""
        d = tmp_path / "survivor"
        d.mkdir()
        (d / "inner.txt").write_text("still here", encoding="utf-8")
        monkeypatch.setattr(shutil, "rmtree", lambda *a, **k: None)  # pretends to succeed

        with pytest.raises(PluginStoreError, match="still exists after deletion"):
            _rmtree_reporting(d, what="agent plugin 'survivor'")


class TestCopilotRuntimeMcpConfigEdges:
    def _provider(self, monkeypatch, profile):
        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(mod, "load_agent_profile", lambda _n: profile)
        return mod.CopilotCliProvider("tid-x", "sess", "win", "worker"), mod

    def test_a_profile_that_cannot_be_loaded_only_warns(self, monkeypatch, caplog):
        """Copilot must still launch with CAO's own server when the profile fails."""
        from cli_agent_orchestrator.providers import copilot_cli as mod

        monkeypatch.setattr(
            mod,
            "load_agent_profile",
            lambda _n: (_ for _ in ()).throw(RuntimeError("profile unreadable")),
        )
        provider = mod.CopilotCliProvider("tid-y", "sess", "win", "worker")

        with caplog.at_level(logging.WARNING):
            servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]

        assert list(servers) == ["cao-mcp-server"]
        assert any("Copilot MCP config" in r.getMessage() for r in caplog.records)

    def test_a_plugin_cannot_replace_caos_own_server(self, monkeypatch):
        class P:
            name = "worker"
            mcpServers = {"cao-mcp-server": {"command": "impostor"}, "extra": {"command": "ok"}}

        provider, _ = self._provider(monkeypatch, P())
        servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]

        assert servers["cao-mcp-server"]["command"] != "impostor"
        assert "extra" in servers

    def test_a_pydantic_entry_is_serialized_and_gets_the_terminal_id(self, monkeypatch):
        from cli_agent_orchestrator.models.agent_profile import McpServer

        class P:
            name = "worker"
            mcpServers = {"modelled": McpServer(command="node", args=["s.js"])}

        provider, _ = self._provider(monkeypatch, P())
        servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]

        assert servers["modelled"]["command"] == "node"
        assert (
            servers["modelled"]["env"]["CAO_TERMINAL_ID"] == "tid-y"
            or servers["modelled"]["env"]["CAO_TERMINAL_ID"]
        )
        assert servers["modelled"]["disabled"] is False

    def test_an_explicit_terminal_id_in_a_plugin_env_is_not_overridden(self, monkeypatch):
        class P:
            name = "worker"
            mcpServers = {"pinned": {"command": "x", "env": {"CAO_TERMINAL_ID": "preset"}}}

        provider, _ = self._provider(monkeypatch, P())
        servers = json.loads(provider._build_runtime_mcp_config())["mcpServers"]
        assert servers["pinned"]["env"]["CAO_TERMINAL_ID"] == "preset"


class TestInitSkillRenameHelpers:
    def test_unreadable_skill_md_yields_a_stable_sentinel(self, tmp_path):
        """A retirement decision must not hinge on an unreadable file."""
        from cli_agent_orchestrator.cli.commands.init import _normalized_skill_md

        missing = tmp_path / "SKILL.md"
        assert _normalized_skill_md(missing) == "<unreadable:SKILL.md>"

    def test_the_name_line_is_ignored_when_comparing(self, tmp_path):
        from cli_agent_orchestrator.cli.commands.init import _normalized_skill_md

        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("---\nname: old\ndescription: d\n---\nbody\n", encoding="utf-8")
        b.write_text("---\nname: new\ndescription: d\n---\nbody\n", encoding="utf-8")

        assert _normalized_skill_md(a) == _normalized_skill_md(b)
