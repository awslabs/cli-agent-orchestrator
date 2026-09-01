"""Projection write-back is a compare-and-set, not a stale overwrite (finding F3).

``rebuild_projection`` snapshotted the installed set, did slow filesystem work, then
wrote back a *complete* ``PluginRecord`` rebuilt from that stale snapshot — no lock,
no freshness check. Two interleavings followed, both live in-process because
``POST /plugins`` and ``DELETE /plugins/{name}`` each offload to a thread:

* *metadata revert* — a concurrent ``add --force`` published v2 under the store lock
  and the write-back put v1's metadata back, so the store described v1 while v2's
  bytes were installed;
* *record resurrection* — a concurrent removal unpublished the record and the
  write-back re-created it, so ``get()`` reported a plugin whose bytes were gone and
  a later non-force ``add`` was refused.

Both are driven deterministically here by mutating the store from inside the
materialization step — the exact window between the snapshot and the write-back. No
sleeps, no threads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import projection as projection_module
from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    rebuild_projection,
    release_projection_claim,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

from .conftest import build_plugin

USER_MARKER = "# the user's own version, which must survive"


def _write_user_skill(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A user-authored skill that must not be deleted.\n"
        f"---\n\n{USER_MARKER}\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def world(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    # Patched on the store module too, so the default-constructed store the CLI
    # path builds for itself lands in the same tmp tree.
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", data_dir
    )
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.skills._refresh_installed_agents", lambda: None
    )

    return {
        "store": InstalledPluginStore(plugins_dir, data_dir),
        "skills_dir": skills_dir,
        "tmp_path": tmp_path,
    }


def _install_donor(world, skill_name: str, *, version: str = "1.0.0", src: str = "plugin-src"):
    source = build_plugin(
        world["tmp_path"] / src, "donor", skills=[skill_name], version=version, with_mcp=False
    )
    return install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        force=True,
        refresh_agents=False,
    )


class TestWriteBackIsACompareAndSet:
    """F3 — the write-back must not revert or resurrect a concurrent commit."""

    def _rebuild_with_interference(self, world, interfere):
        """Run a rebuild, mutating the store between snapshot and write-back.

        The hook rides on ``_materialize``, which is called after the record
        snapshot and before ``_write_back`` — the exact window the race lives in,
        driven deterministically rather than with sleeps.
        """
        real_materialize = projection_module._materialize
        fired = {"once": False}

        def _hooked(*args, **kwargs):
            result = real_materialize(*args, **kwargs)
            # One-shot: the interference itself installs/removes a plugin, which
            # rebuilds the projection again, and an unguarded hook would recurse.
            if not fired["once"]:
                fired["once"] = True
                interfere()
            return result

        original = projection_module._materialize
        projection_module._materialize = _hooked
        try:
            return rebuild_projection(world["store"], skills_dir=world["skills_dir"])
        finally:
            projection_module._materialize = original

    def test_a_concurrent_publish_is_not_reverted(self, world):
        _install_donor(world, "shared-skill", version="1.0.0")

        def _publish_v2():
            _install_donor(world, "shared-skill", version="2.0.0", src="plugin-src-v2")

        self._rebuild_with_interference(world, _publish_v2)

        record = world["store"].get("donor")
        assert record is not None
        assert record.version == "2.0.0", "the stale snapshot reverted the concurrent publish"
        assert record.projected_skill_names == ("shared-skill",)

    def test_a_concurrent_removal_is_not_resurrected(self, world):
        _install_donor(world, "shared-skill")
        record_path = world["store"].state_dir / "donor.json"
        assert record_path.is_file()

        def _remove_the_record():
            world["store"].unpublish("donor")

        self._rebuild_with_interference(world, _remove_the_record)

        assert world["store"].get("donor") is None, "an unpublished record was resurrected"
        assert not record_path.exists()

    def test_the_primitive_patches_only_its_own_field(self, world):
        _install_donor(world, "shared-skill", version="1.0.0")
        before = world["store"].get("donor")

        assert world["store"].update_projected_names("donor", ("other-name",)) is True

        after = world["store"].get("donor")
        assert after.projected_skill_names == ("other-name",)
        assert (after.version, after.source, after.installed_at, after.skill_names) == (
            before.version,
            before.source,
            before.installed_at,
            before.skill_names,
        )

    def test_the_primitive_reports_a_vanished_record_instead_of_writing_one(self, world):
        _install_donor(world, "shared-skill")
        world["store"].unpublish("donor")

        assert world["store"].update_projected_names("donor", ("shared-skill",)) is False
        assert world["store"].get("donor") is None

    def test_release_reads_fresh_state_under_the_lock(self, world):
        """The claim release is the same read-modify-write, so it gets the same lock."""
        _install_donor(world, "shared-skill", version="1.0.0")
        stale = world["store"].get("donor")

        _install_donor(world, "shared-skill", version="2.0.0", src="plugin-src-v2")
        assert stale.version == "1.0.0"

        released = release_projection_claim("shared-skill", store=world["store"])

        assert released == "donor"
        record = world["store"].get("donor")
        assert record.version == "2.0.0", "the release wrote back stale metadata"
        assert record.projected_skill_names == ()
