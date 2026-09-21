"""A user skill that replaced a projected one must survive plugin removal.

Reproduced by review on #584: the previous projection was reconstructed from the
install records, so after ``cao skills add <folder> --force`` replaced a copied
plugin projection, the record still claimed the name. A later ``cao plugin
remove`` then recursively deleted the *user's* directory, and any intervening
projection rebuild overwrote it with the plugin's copy.

The fix transfers the claim at install time, so the documented rule — a
user-added skill always wins — holds regardless of the order the two installs
happened in. Mutation-verified: removing the ``release_projection_claim`` call
fails these tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install, uninstall
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    rebuild_projection,
    release_projection_claim,
)

from .conftest import build_plugin

USER_MARKER = "# the user's own version"


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
    """Copy-mode projection over a tmp-path store, as the defect requires."""
    from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
        lambda: "copy",
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )

    store = InstalledPluginStore(plugins_dir, data_dir)
    return {"store": store, "skills_dir": skills_dir, "tmp_path": tmp_path}


def _install_plugin_providing(world, skill_name: str):
    source = build_plugin(
        world["tmp_path"] / "plugin-src", "donor", skills=[skill_name], with_mcp=False
    )
    return install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )


class TestOwnershipTransfersToTheUser:
    def test_plugin_remove_does_not_delete_the_user_skill_that_replaced_it(self, world):
        """The reviewer's sequence, end to end."""
        _install_plugin_providing(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        assert projected.exists()

        # The user replaces it with their own, which transfers the claim.
        released = release_projection_claim("shared-skill", store=world["store"])
        assert released == "donor"
        import shutil

        shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")

        uninstall("donor", store=world["store"], skills_dir=world["skills_dir"])

        assert projected.is_dir(), "plugin removal deleted the user's skill"
        assert USER_MARKER in (projected / "SKILL.md").read_text(encoding="utf-8")

    def test_a_rebuild_does_not_overwrite_the_user_skill(self, world):
        """The other half: an intervening rebuild must not restore the copy."""
        _install_plugin_providing(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"

        release_projection_claim("shared-skill", store=world["store"])
        import shutil

        shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")

        rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert USER_MARKER in (projected / "SKILL.md").read_text(
            encoding="utf-8"
        ), "a projection rebuild overwrote the user's skill"

    def test_the_plugin_loses_the_collision_with_a_report(self, world):
        """Losing must be reported, not silent."""
        _install_plugin_providing(world, "shared-skill")
        projected = world["skills_dir"] / "shared-skill"
        release_projection_claim("shared-skill", store=world["store"])
        import shutil

        shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")

        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert "shared-skill" not in result.projected
        assert any("shared-skill" in (f.message or "") for f in result.findings)

    def test_releasing_a_name_no_plugin_claims_is_a_no_op(self, world):
        assert release_projection_claim("nobody-claims-this", store=world["store"]) is None

    def test_the_record_no_longer_claims_the_released_name(self, world):
        _install_plugin_providing(world, "shared-skill")
        release_projection_claim("shared-skill", store=world["store"])

        record = world["store"].get("donor")
        assert "shared-skill" not in record.projected_skill_names
