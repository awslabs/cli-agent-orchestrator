"""Install ordering: ``PLUGIN_DATA`` must exist before the commit (review finding F4).

The §9.1 data directory used to be created *after* ``store.publish`` committed, so
a failure on the data volume left root + record present, no data directory, and no
projection — and the retry was then refused as already-installed, forcing the
operator to pass ``--force`` to recover from a failure they did not cause. The fix
is a reorder, so the test injects a fault at the fragile step and asserts the store
committed nothing and the operation is still retryable once the fault clears.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

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


class TestDataDirectoryPrecedesPublish:
    """Requirement 9.1 / finding F4 — nothing may fail after the commit."""

    def test_a_data_dir_failure_leaves_nothing_installed(self, env, monkeypatch):
        plugin_dir = build_plugin(env["tmp_path"] / "src", name="demo", skills=["alpha"])

        calls = {"n": 0}
        real_mkdir = type(plugin_dir).mkdir

        def _failing_mkdir(self, *args, **kwargs):
            if "agent-plugin-data" in str(self):
                calls["n"] += 1
                raise OSError(28, "No space left on device")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr("pathlib.Path.mkdir", _failing_mkdir)

        with pytest.raises(OSError):
            install(
                _source(plugin_dir),
                store=env["store"],
                skills_dir=env["skills_dir"],
                refresh_agents=False,
            )

        assert calls["n"] >= 1, "the data directory creation was never attempted"
        assert env["store"].get("demo") is None
        assert env["store"].is_installed("demo") is False
        assert not (env["store"].plugin_root("demo")).exists()

    def test_a_retry_after_the_fault_clears_needs_no_force(self, env, monkeypatch):
        """The recovery property: the operator is not punished for an ENOSPC."""
        plugin_dir = build_plugin(env["tmp_path"] / "src", name="demo", skills=["alpha"])
        real_mkdir = type(plugin_dir).mkdir
        fault = {"on": True}

        def _maybe_failing_mkdir(self, *args, **kwargs):
            if fault["on"] and "agent-plugin-data" in str(self):
                raise OSError(28, "No space left on device")
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr("pathlib.Path.mkdir", _maybe_failing_mkdir)
        with pytest.raises(OSError):
            install(
                _source(plugin_dir),
                store=env["store"],
                skills_dir=env["skills_dir"],
                refresh_agents=False,
            )

        fault["on"] = False
        outcome = install(  # no force=True — this is the whole point
            _source(plugin_dir),
            store=env["store"],
            skills_dir=env["skills_dir"],
            refresh_agents=False,
        )

        assert outcome.installed is True
        assert env["store"].get("demo") is not None

    def test_reinstalling_preserves_existing_plugin_data(self, env):
        """§9.1 persistence — the reorder must not clobber a populated data dir."""
        plugin_dir = build_plugin(env["tmp_path"] / "src", name="demo", skills=["alpha"])
        install(
            _source(plugin_dir),
            store=env["store"],
            skills_dir=env["skills_dir"],
            refresh_agents=False,
        )
        keeper = env["store"].plugin_data_dir("demo", create=True) / "state.json"
        keeper.write_text('{"kept": true}', encoding="utf-8")

        install(
            _source(plugin_dir),
            store=env["store"],
            skills_dir=env["skills_dir"],
            force=True,
            refresh_agents=False,
        )

        assert keeper.read_text(encoding="utf-8") == '{"kept": true}'
