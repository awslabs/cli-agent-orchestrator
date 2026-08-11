"""Ownership transfer to a user-installed skill (finding F2).

``release_projection_claim`` returned ``None`` both for "no plugin held this name"
and for "a plugin held it and the record write failed". ``cao skills add --force``
read the failure as the former, unlinked the projection, copied the user's directory
into place, and left the plugin record still claiming the name; the next rebuild's
sweep then removed that directory *by name only* — no symlink check, no containment
check — and ``shutil.rmtree`` took the user's real skill with it. The failure premise
is real: records live under the store's ``state_dir`` while projections live in
``SKILLS_DIR``, so a full or read-only state volume fails the record write while the
copy into the skill store succeeds.

Two independent layers are tested: the tri-state release the caller aborts on, and a
sweep that refuses to delete anything that is not structurally a projection.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins import projection as projection_module
from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    ProjectionClaimError,
    rebuild_projection,
    release_projection_claim,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.cli.main import cli

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


class TestFailedReleaseAbortsTheInstall:
    """F2 layer 1 — the caller must be able to see the failure."""

    def test_a_failed_release_changes_nothing_and_reports_loudly(self, world, monkeypatch):
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        assert projected.exists()
        was_symlink = projected.is_symlink()
        record_before = world["store"].get("donor")

        user_src = _write_user_skill(world["tmp_path"] / "mine" / "shared-skill", "shared-skill")

        def _unwritable(self, record):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(InstalledPluginStore, "write_record", _unwritable)

        result = CliRunner().invoke(cli, ["skills", "add", str(user_src), "--force"])

        assert result.exit_code != 0
        assert "could not be updated" in result.output
        assert "Nothing was changed" in result.output

        monkeypatch.undo()
        assert projected.exists(), "the projection was destroyed by a failed transfer"
        assert projected.is_symlink() == was_symlink
        assert world["store"].get("donor").projected_skill_names == (
            record_before.projected_skill_names
        )
        assert "shared-skill" in world["store"].get("donor").projected_skill_names
        assert (user_src / "SKILL.md").is_file(), "the user's source folder was touched"

    def test_a_working_release_still_transfers_ownership(self, world):
        """Review-1 fix R1-2 stays green: the happy path is unchanged."""
        _install_donor(world, "shared-skill")
        user_src = _write_user_skill(world["tmp_path"] / "mine" / "shared-skill", "shared-skill")

        result = CliRunner().invoke(cli, ["skills", "add", str(user_src), "--force"])

        assert result.exit_code == 0, result.output
        assert "released its projection claim" in result.output or "now user-owned" in result.output
        assert world["store"].get("donor").projected_skill_names == ()
        installed = world["skills_dir"] / "shared-skill"
        assert not installed.is_symlink()
        assert USER_MARKER in (installed / "SKILL.md").read_text(encoding="utf-8")

    def test_no_plugin_holding_the_name_is_not_an_error(self, world):
        assert release_projection_claim("nobody-claims-this", store=world["store"]) is None

    def test_the_failure_state_is_its_own_exception(self, world, monkeypatch):
        _install_donor(world, "shared-skill")
        monkeypatch.setattr(
            InstalledPluginStore,
            "write_record",
            lambda self, record: (_ for _ in ()).throw(OSError(30, "Read-only file system")),
        )

        with pytest.raises(ProjectionClaimError) as excinfo:
            release_projection_claim("shared-skill", store=world["store"])

        assert "donor" in str(excinfo.value)


class TestSweepRefusesUnmanagedPaths:
    """F2 layer 2 — a stale name match must never delete a real directory."""

    def test_a_real_directory_survives_a_poisoned_claim(self, world):
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"

        # Poisoned state: the record still claims the name (as it would after a
        # release that failed), but what is on disk is the user's own directory.
        if projected.is_symlink():
            projected.unlink()
        else:
            shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")

        swept, findings = projection_module._sweep(
            world["store"],
            world["skills_dir"],
            previous={"shared-skill": "donor"},
            current={},
        )

        assert swept == []
        assert projected.is_dir()
        assert USER_MARKER in (projected / "SKILL.md").read_text(encoding="utf-8")
        assert [f for f in findings if f.code == "projection.sweep_skipped_unmanaged"]

    def test_a_real_store_symlink_is_still_swept(self, world):
        """The guard must not break the legitimate case it wraps."""
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        if not projected.is_symlink():
            pytest.skip("symlink projection unavailable in this environment")

        swept, findings = projection_module._sweep(
            world["store"],
            world["skills_dir"],
            previous={"shared-skill": "donor"},
            current={},
        )

        assert swept == ["shared-skill"]
        assert not projected.exists()
        assert findings == []

    def test_a_stray_file_at_a_projected_name_is_still_removed(self, world):
        """A regular file is never something the engine placed.

        The guard's job is to protect a real *directory* the user owns. A plain
        file at a projected skill name is not a skill (skill discovery gates on
        ``is_dir()`` plus a ``SKILL.md``) and cannot be a projection, so removing
        it by name is safe and keeps the store tidy — the guard must not turn into
        a reason to leave junk behind.
        """
        stray = world["skills_dir"] / "shared-skill"
        stray.write_text("not a skill", encoding="utf-8")

        swept, findings = projection_module._sweep(
            world["store"],
            world["skills_dir"],
            previous={"shared-skill": "donor"},
            current={},
        )

        assert swept == ["shared-skill"]
        assert not stray.exists()
        assert findings == []

    def test_a_copy_mode_projection_is_still_swept(self, world, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "copy",
        )
        _install_donor(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        assert projected.is_dir() and not projected.is_symlink()

        swept, _findings = projection_module._sweep(
            world["store"],
            world["skills_dir"],
            previous={"shared-skill": "donor"},
            current={},
            mode="copy",
        )

        assert swept == ["shared-skill"]
        assert not projected.exists()


class TestMaterializeRefusesUnclaimedTargets:
    """The other half of the delete path, found by an independent audit.

    The F2 fix guarded the sweep's delete-by-name path, but ``_place`` removes
    whatever occupies the target before writing — ``_remove_quiet`` on a real
    directory is the same ``shutil.rmtree``. So materialization could destroy a
    directory the engine never placed, with the sweep guard fully in place.

    The rule is deliberately non-circular: a real directory at a name the
    *previous* projection did not own is somebody else's data and is refused,
    while a name we did own stays replaceable — which is what keeps a plugin
    upgrade and a copy-to-symlink migration working.
    """

    def test_an_unclaimed_directory_is_not_destroyed_by_materialization(self, world):
        # A directory with no SKILL.md is not classified pre-existing (that test
        # requires a SKILL.md), so the plugin wins the name and materialization
        # is reached — exactly the window the audit identified.
        squatter = world["skills_dir"] / "shared-skill"
        squatter.mkdir()
        (squatter / "notes.txt").write_text("half-copied, or simply the user's", encoding="utf-8")

        _install_donor(world, "shared-skill")

        assert squatter.is_dir() and not squatter.is_symlink()
        assert (squatter / "notes.txt").read_text(encoding="utf-8").startswith("half-copied")

        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"])

        assert (squatter / "notes.txt").is_file(), "materialization destroyed an unclaimed dir"
        assert [f for f in result.findings if f.code == "projection.target_not_ours"]
        assert "shared-skill" not in result.projected

    def test_a_name_the_projection_owned_is_still_replaceable(self, world):
        """The upgrade/migration path must keep working."""
        _install_donor(world, "shared-skill", version="1.0.0")
        projected = world["skills_dir"] / "shared-skill"
        assert projected.exists()

        # Second install of the same name: previous owns it, so replacement is allowed.
        _install_donor(world, "shared-skill", version="2.0.0", src="plugin-src-v2")

        assert (world["skills_dir"] / "shared-skill").exists()
        assert world["store"].get("donor").projected_skill_names == ("shared-skill",)
