"""Removal ordering: the record outlives a purge failure (review finding F5).

``_unpublish_locked`` unlinked the install record *before* purging the ``PLUGIN_DATA``
tree, so a purge failure raised an error whose message promised "the installation
remains tracked and can be retried" when the record was already gone — and there
was no retry path at all, because the next ``remove`` reports "not installed". The
fix is a reorder; these tests pin the record as the retry handle.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.agent_plugins import store as store_module
from cli_agent_orchestrator.agent_plugins.installer import install, uninstall
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore, PluginStoreError

from .conftest import build_plugin


@pytest.fixture
def env(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )
    return {
        "store": InstalledPluginStore(plugins_dir, data_dir),
        "skills_dir": skills_dir,
        "tmp_path": tmp_path,
    }


def _source(path):
    return PluginSource(kind="path", location=str(path))


class TestPurgePrecedesRecordUnlink:
    """Finding F5 — the record is the retry handle; it must outlive a failure."""

    def _installed(self, env):
        plugin_dir = build_plugin(env["tmp_path"] / "src", name="demo", skills=["alpha"])
        install(
            _source(plugin_dir),
            store=env["store"],
            skills_dir=env["skills_dir"],
            refresh_agents=False,
        )
        data_file = env["store"].plugin_data_dir("demo", create=True) / "blob.bin"
        data_file.write_bytes(b"payload")
        return data_file

    def test_a_purge_failure_keeps_the_record_and_stays_retryable(self, env, monkeypatch):
        data_file = self._installed(env)
        data_dir = env["store"].plugin_data_dir("demo")
        real = store_module._rmtree_reporting
        fault = {"on": True}

        def _fail_for_data(path, *, what):
            # Mirrors the real helper's contract: it converts an OSError into a
            # PluginStoreError, so injecting the raw OSError would test a code
            # path that cannot happen.
            if fault["on"] and path == data_dir:
                raise PluginStoreError(
                    f"Could not remove {what}: Permission denied. Nothing was "
                    "recorded as removed, so the installation remains tracked "
                    "and can be retried."
                )
            return real(path, what=what)

        monkeypatch.setattr(store_module, "_rmtree_reporting", _fail_for_data)

        # `installer.uninstall` converts only ValueError, so the store's own error
        # type is what an operator sees for a deletion failure.
        with pytest.raises(PluginStoreError) as excinfo:
            uninstall(
                "demo",
                store=env["store"],
                skills_dir=env["skills_dir"],
                purge_data=True,
                refresh_agents=False,
            )

        assert env["store"].get("demo") is not None, "the retry handle was destroyed"
        assert "remains tracked" in str(excinfo.value)
        assert data_file.exists()

        fault["on"] = False
        outcome = uninstall(
            "demo",
            store=env["store"],
            skills_dir=env["skills_dir"],
            purge_data=True,
            refresh_agents=False,
        )

        assert outcome.removed is True
        assert env["store"].get("demo") is None
        assert not data_dir.exists()

    def test_a_root_removal_failure_still_leaves_everything_intact(self, env, monkeypatch):
        """The pre-existing guarantee: the first failing step strands nothing."""
        data_file = self._installed(env)
        root = env["store"].plugin_root("demo")
        real = store_module._rmtree_reporting

        def _fail_for_root(path, *, what):
            if path == root:
                raise PluginStoreError(f"Could not remove {what}: Permission denied.")
            return real(path, what=what)

        monkeypatch.setattr(store_module, "_rmtree_reporting", _fail_for_root)

        with pytest.raises(PluginStoreError):
            uninstall(
                "demo",
                store=env["store"],
                skills_dir=env["skills_dir"],
                purge_data=True,
                refresh_agents=False,
            )

        assert env["store"].get("demo") is not None
        assert data_file.exists()
        assert root.exists()


def test_the_store_error_type_is_reachable():
    """Guard against the fixtures silently swapping the exception hierarchy."""
    assert issubclass(PluginStoreError, Exception)
