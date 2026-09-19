"""Copy-mode projections prove ownership by marker and content digest.

Reported by review 3 on #584:

> "Do not treat every regular file as a managed projection. […] For copy-mode
> directories, persist a marker or other exact provenance before recursively
> deleting; a prior name claim alone cannot prove that the current bytes are
> CAO-owned."

The old predicate answered "yes, CAO's" for *any* regular file and for *any*
directory whenever the projection happened to be running in copy mode. Neither is
provenance. A copied projection now carries ``.cao-projection.json`` holding a
digest of the bytes CAO wrote, and only three things count as ownership: a symlink
resolving inside the plugin store, a directory whose marker digest still verifies,
or a directory byte-identical to the source the caller names (the adoption rule,
which is how copies written before this fix stay manageable).

The direction of every residual is over-preservation: an unproven directory is
left in place and reported, never deleted.

Mutation-verified: restoring ``if path.is_file(): return True`` fails the
regular-file tests; skipping the digest comparison fails the edited-copy and
forged-marker tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import projection as projection_module
from cli_agent_orchestrator.agent_plugins.installer import install, uninstall
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    MARKER_FILENAME,
    rebuild_projection,
)

from .conftest import build_plugin

SKILL = "donor-skill"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Copy-mode projection over a tmp store — the mode the marker exists for."""
    from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

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

    store = InstalledPluginStore(tmp_path / "agent-plugins", tmp_path / "agent-plugin-data")
    # `cao skills add` calls `release_projection_claim`, which builds its own store
    # from these module globals. Without redirecting them the test takes the real
    # store lock under `~/.aws/cli-agent-orchestrator/` and would rewrite a real
    # install record if a real plugin happened to claim this skill name -- the same
    # hazard class as the `TestOpencodeCollisionSnapshotShape` leak.
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", store.plugins_dir
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", store.data_dir
    )
    return {"store": store, "skills_dir": skills_dir, "tmp_path": tmp_path}


def _install(world, name: str = "donor", skills=(SKILL,)):
    source = build_plugin(
        world["tmp_path"] / f"src-{name}", name, skills=list(skills), with_mcp=False
    )
    outcome = install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        refresh_agents=False,
    )
    assert outcome.installed, [f.message for f in outcome.report.findings]
    return source


def _remove(world, name: str = "donor"):
    return uninstall(
        name, store=world["store"], skills_dir=world["skills_dir"], refresh_agents=False
    )


def _marker(world, skill: str = SKILL) -> Path:
    return world["skills_dir"] / skill / MARKER_FILENAME


def _codes(result) -> list:
    return [f.code for f in result.findings]


class TestTheMarkerIsWrittenAndVerifiable:
    def test_a_copy_mode_projection_carries_a_marker_whose_digest_matches(self, world):
        _install(world)
        projected = world["skills_dir"] / SKILL
        assert projected.is_dir() and not projected.is_symlink()

        payload = json.loads(_marker(world).read_text(encoding="utf-8"))
        assert payload["format"] == 1
        assert payload["plugin"] == "donor"
        assert payload["skill"] == SKILL
        assert payload["digest"].startswith("sha256-tree-v1:")
        # The recorded digest is the digest of what is on disk now.
        assert payload["digest"] == projection_module._tree_digest(projected)
        # And it is the source's digest too, since nothing has been edited.
        assert payload["source"] == str(world["store"].plugin_root("donor") / "skills" / SKILL)

    def test_the_marker_is_excluded_from_its_own_digest(self, world):
        """Otherwise the value could never verify against the tree containing it."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        source = world["store"].plugin_root("donor") / "skills" / SKILL
        # The source has no marker; the projection does. Equal digests prove the
        # marker was excluded.
        assert projection_module._tree_digest(projected) == projection_module._tree_digest(source)

    def test_a_symlink_mode_projection_carries_no_marker(self, world, monkeypatch):
        """Nothing to prove: a store symlink is structurally CAO's already."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "symlink",
        )
        _install(world)
        projected = world["skills_dir"] / SKILL
        assert projected.is_symlink()
        # The marker must not have been written into the plugin's own source tree.
        source = world["store"].plugin_root("donor") / "skills" / SKILL
        assert not (source / MARKER_FILENAME).exists()


class TestOwnershipDecidesWhatMayBeSwept:
    def test_a_marked_copy_is_swept_when_its_plugin_is_removed(self, world):
        _install(world)
        projected = world["skills_dir"] / SKILL
        assert projected.is_dir()

        _remove(world)

        assert not projected.exists()

    def test_a_user_edited_copy_is_not_swept_and_is_reported(self, world):
        """The core of the finding: the bytes changed, so they are not CAO's to delete."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        (projected / "SKILL.md").write_text("# my own version\n", encoding="utf-8")

        outcome = _remove(world)

        assert projected.is_dir()
        assert (projected / "SKILL.md").read_text(encoding="utf-8") == "# my own version\n"
        assert "projection.sweep_skipped_unmanaged" in [
            f.code for f in outcome.projection_findings
        ], [f.code for f in outcome.projection_findings]
        assert world["store"].get("donor") is None

    def test_an_added_file_also_breaks_ownership(self, world):
        """The digest covers the whole tree, not just SKILL.md."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        (projected / "extra-notes.md").write_text("mine", encoding="utf-8")

        _remove(world)

        assert projected.is_dir()
        assert (projected / "extra-notes.md").is_file()

    def test_a_forged_marker_with_a_wrong_digest_is_not_ownership(self, world):
        """A marker is not a token to be replayed — the digest has to hold."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        payload = json.loads(_marker(world).read_text(encoding="utf-8"))
        payload["digest"] = "sha256-tree-v1:" + "0" * 64
        _marker(world).write_text(json.dumps(payload), encoding="utf-8")

        _remove(world)

        assert projected.is_dir(), "a marker with a bogus digest was accepted as proof"

    def test_a_marker_sourced_outside_the_plugin_store_is_not_ownership(self, world):
        """A marker naming somewhere else was not written by a projection."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        payload = json.loads(_marker(world).read_text(encoding="utf-8"))
        payload["source"] = str(world["tmp_path"] / "not-the-store" / SKILL)
        _marker(world).write_text(json.dumps(payload) + "\n", encoding="utf-8")
        # Re-point the digest at the edited tree so only the source is wrong.
        payload["digest"] = projection_module._tree_digest(projected)
        _marker(world).write_text(json.dumps(payload) + "\n", encoding="utf-8")

        assert (
            projection_module._is_managed_projection(projected, world["store"], source=None)
            is False
        )

    def test_a_regular_file_at_a_projected_name_is_never_ours(self, world):
        stray = world["skills_dir"] / "unrelated"
        stray.write_text("not a skill", encoding="utf-8")
        assert projection_module._is_managed_projection(stray, world["store"], source=None) is False


class TestOwnershipSurvivesASymlinkedCaoHome:
    """Reported by independent review of this fix (B1).

    ``_write_marker`` recorded ``str(source)`` unresolved while ``_within``
    compares against ``os.path.realpath(root)``, so on any host whose CAO home
    contains a symlink component the marker never verified. The symlink branch of
    ``_is_managed_projection`` realpaths both sides; the marker branch did not.

    While the plugin is installed the adoption rule masks the bug (the copy is
    still byte-identical to its source). It becomes visible exactly when the
    source is gone — on ``uninstall`` — and the copy is stranded as "a directory
    CAO did not place". macOS ``/tmp`` (→ ``/private/tmp``) and any symlinked
    ``$HOME`` hit this, so it is the common case there, not an exotic one.
    """

    @pytest.fixture
    def linked_world(self, tmp_path, monkeypatch):
        """A world whose store lives behind a symlink.

        ``tmp_path`` is already resolved, so the symlink has to be created here —
        inheriting the outer fixture would not reproduce anything.
        """
        from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        skills_dir = link / "skills"
        skills_dir.mkdir()

        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skills_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "copy",
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
        )
        store = InstalledPluginStore(link / "agent-plugins", link / "agent-plugin-data")
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", store.plugins_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", store.data_dir
        )
        return {"store": store, "skills_dir": skills_dir, "tmp_path": tmp_path}

    def test_the_marker_verifies_behind_a_symlinked_home(self, linked_world):
        _install(linked_world)
        projected = linked_world["skills_dir"] / SKILL
        assert projected.is_dir() and (projected / MARKER_FILENAME).is_file()

        assert (
            projection_module._verified_marker(projected, linked_world["store"]) is not None
        ), "the marker did not verify, so ownership rests on the adoption rule alone"

    def test_the_recorded_source_is_stored_resolved(self, linked_world):
        """The write half of the fix, asserted on the persisted artifact.

        Verification realpaths the recorded value too, so it would still pass if
        the unresolved path were written -- the read half subsumes the write half
        behaviourally. This assertion is what makes the write half independently
        meaningful: the marker is a persisted format, and a ``source`` that only
        resolves relative to the symlink layout in force when it was written is a
        latent trap for any future reader that trusts it as-is.
        """
        import os

        _install(linked_world)
        payload = json.loads(
            (linked_world["skills_dir"] / SKILL / MARKER_FILENAME).read_text(encoding="utf-8")
        )
        assert payload["source"] == os.path.realpath(payload["source"])
        assert "/link/" not in payload["source"], payload["source"]

    def test_the_copy_is_swept_on_uninstall_behind_a_symlinked_home(self, linked_world):
        """The observable consequence: no source left, so only the marker can prove it."""
        _install(linked_world)
        projected = linked_world["skills_dir"] / SKILL

        outcome = _remove(linked_world)

        codes = [f.code for f in outcome.projection_findings]
        assert not projected.exists(), codes
        assert "projection.sweep_skipped_unmanaged" not in codes, codes


class TestAMarkerIsBoundToItsDirectory:
    """Reported by independent review of this fix (B2) — a marker-introduced regression.

    ``_verified_marker`` read ``plugin``, ``skill`` and ``source`` but checked none
    of them against ``path.name``, so a marker was a bearer token: copying a marked
    projection to another name carried ownership with it, and a later plugin
    claiming that name replaced the user's directory *with no finding at all*. On
    the base commit that directory was refused with ``target_not_ours``, so the
    marker made this strictly worse — and "exact provenance" is precisely what the
    review asked for.

    ``plugin`` stays unchecked: a winner transition legitimately hands a name from
    one plugin to another. The *skill name* never changes in that transition, which
    is why binding on it costs nothing.
    """

    def test_a_relocated_marker_does_not_confer_ownership(self, world):
        import shutil

        _install(world)
        projected = world["skills_dir"] / SKILL
        relocated = world["skills_dir"] / "beta"
        shutil.copytree(projected, relocated)  # dot-file included
        assert (relocated / MARKER_FILENAME).is_file()

        assert (
            projection_module._is_managed_projection(relocated, world["store"], source=None)
            is False
        ), "a marker naming another skill was accepted as proof of ownership"

    def test_a_plugin_claiming_that_name_cannot_replace_the_users_directory(self, world):
        """The copy is left byte-identical on purpose, so the marker really does verify.

        Editing it would break the digest and refuse the directory for an unrelated
        reason — the bug would be masked and the test would pass vacuously. The
        window this closes is "the user copied a projected skill as a starting
        point and has not edited it yet", which is exactly when a `cp -r` is most
        likely to be sitting there.
        """
        import shutil

        _install(world)
        projected = world["skills_dir"] / SKILL
        relocated = world["skills_dir"] / "beta"
        shutil.copytree(projected, relocated)
        # Precondition: the copy is byte-identical, so the recorded digest still
        # matches and only the name binding can refuse it.
        assert projection_module._tree_digest(relocated) == projection_module._tree_digest(
            projected
        )

        _install(world, name="betaplug", skills=("beta",))
        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert "beta" not in result.projected, _codes(result)
        assert _codes(result), "the directory was replaced with no finding at all"
        # Still the user's copy: a replacement would have re-marked it for betaplug.
        payload = json.loads((relocated / MARKER_FILENAME).read_text(encoding="utf-8"))
        assert payload["skill"] == SKILL and payload["plugin"] == "donor"


class TestTheAdoptionRuleUpgradesPreMarkerCopies:
    def test_a_markerless_copy_identical_to_its_source_is_adopted_on_rebuild(self, world):
        """Byte-identity with the plugin's own bytes is exact proof.

        Without this, every copy-mode projection written before markers existed
        would become permanently unmanaged — reported as a pre-existing collision
        forever and never sweepable.
        """
        _install(world)
        _marker(world).unlink()  # a pre-fix projection

        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert result.projected.get(SKILL) == "donor"
        assert "projection.preexisting_collision" not in _codes(result)
        assert _marker(world).is_file(), "the adopted copy was not re-marked"

    def test_a_markerless_copy_of_a_removed_plugin_is_preserved(self, world):
        """No marker and no source to compare against ⇒ nothing proves it is CAO's.

        The conservative direction. A pre-fix copy whose plugin is being removed in
        the same breath cannot be adopted, so the user keeps the directory.
        """
        _install(world)
        projected = world["skills_dir"] / SKILL
        _marker(world).unlink()
        # Remove the plugin's bytes first so the adoption comparison has no source.
        outcome = _remove(world)

        assert projected.is_dir()
        assert "projection.sweep_skipped_unmanaged" in [f.code for f in outcome.projection_findings]

    def test_an_edited_markerless_copy_is_not_adopted(self, world):
        """Adoption is identity, not resemblance."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        _marker(world).unlink()
        (projected / "SKILL.md").write_text("# edited\n", encoding="utf-8")

        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert SKILL not in result.projected
        assert (projected / "SKILL.md").read_text(encoding="utf-8") == "# edited\n"


class TestMaterializeUsesTheSamePredicate:
    def test_election_refuses_an_edited_copy_that_is_still_a_valid_skill(self, world):
        """An edited copy with a ``SKILL.md`` never reaches ``_materialize``.

        Renamed after independent review pointed out the old name claimed a code
        path this test does not exercise. ``_preexisting_skill_names`` recognises
        the directory as the user's skill, so *election* refuses the name and the
        finding is ``preexisting_collision`` -- plus a ``sweep_skipped_unmanaged``
        recording that CAO abandoned its claim without deleting. The real
        ``_materialize`` refusal is exercised by the two variants below.
        """
        _install(world)
        projected = world["skills_dir"] / SKILL
        (projected / "SKILL.md").write_text("# mine now\n", encoding="utf-8")

        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert SKILL not in result.projected
        assert "projection.preexisting_collision" in _codes(result)
        assert "projection.sweep_skipped_unmanaged" in _codes(result)
        assert (projected / "SKILL.md").read_text(encoding="utf-8") == "# mine now\n"

    def test_materialize_refuses_a_claimed_directory_without_a_skill_md(self, world):
        """The genuine ``_materialize`` path: claimed name, changed bytes, not a skill.

        With no ``SKILL.md`` the directory is invisible to election, so the plugin
        wins the name and ``_materialize`` is the only thing between the user's
        files and ``shutil.rmtree``.
        """
        _install(world)
        projected = world["skills_dir"] / SKILL
        (projected / "SKILL.md").unlink()
        (projected / "notes.txt").write_text("half-finished work of my own", encoding="utf-8")

        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert SKILL not in result.projected
        assert "projection.target_not_ours" in _codes(result)
        assert (projected / "notes.txt").read_text(
            encoding="utf-8"
        ) == "half-finished work of my own"

    def test_materialize_refuses_a_foreign_symlink_that_is_not_a_valid_skill(self, world):
        """Same for a symlink out of the store whose target has no ``SKILL.md``.

        The variant in ``test_last_mile_contracts`` gives its target a ``SKILL.md``,
        so election refuses it there and ``_materialize`` is never reached --
        contrary to what that test's name suggests. This one reaches it.
        """
        elsewhere = world["tmp_path"] / "mine"
        elsewhere.mkdir()
        (elsewhere / "notes.txt").write_text("not a skill, still mine", encoding="utf-8")
        (world["skills_dir"] / SKILL).symlink_to(elsewhere, target_is_directory=True)

        _install(world)
        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")

        assert SKILL not in result.projected
        assert "projection.target_not_ours" in _codes(result)
        assert (world["skills_dir"] / SKILL).is_symlink()
        assert (elsewhere / "notes.txt").is_file()

    def test_an_unedited_copy_is_still_replaceable(self, world):
        """The guard must not break the idempotent rebuild it wraps."""
        _install(world)
        result = rebuild_projection(world["store"], skills_dir=world["skills_dir"], mode="copy")
        assert result.projected.get(SKILL) == "donor"
        assert "projection.target_not_ours" not in _codes(result)

    def test_a_symlink_mode_rebuild_sweeps_a_stale_marked_copy(self, world, monkeypatch):
        """The residual the marker retires: copy-mode leftovers used to survive a
        symlink-mode rebuild and be misreported as pre-existing forever."""
        _install(world)
        projected = world["skills_dir"] / SKILL
        assert projected.is_dir() and not projected.is_symlink()

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "symlink",
        )
        outcome = _remove(world)

        assert not projected.exists(), [f.code for f in outcome.projection_findings]


class TestTheMarkerIsInvisibleToEveryReader:
    def test_skill_discovery_does_not_see_the_marker(self, world):
        """A dot-file, so ``list_skills`` and ``SKILL.md`` gating both ignore it."""
        from cli_agent_orchestrator.utils.skills import list_skills

        _install(world)
        names = {skill.name for skill in list_skills()}
        assert SKILL in names
        assert MARKER_FILENAME not in names
        assert not any(name.startswith(".") for name in names)

    def test_skill_content_is_unchanged_by_the_marker(self, world):
        from cli_agent_orchestrator.utils.skills import load_skill_content

        _install(world)
        content = load_skill_content(SKILL)
        assert content is not None
        assert MARKER_FILENAME not in content

    def test_plugin_validation_ignores_a_stray_marker_in_a_plugin_root(self, world):
        """``_discover_skills`` skips dot-entries, so a packaged marker is inert."""
        from cli_agent_orchestrator.agent_plugins import validation

        source = build_plugin(
            world["tmp_path"] / "src-stray", "stray", skills=[SKILL], with_mcp=False
        )
        (source / "skills" / MARKER_FILENAME).write_text("{}", encoding="utf-8")

        discovered, findings = validation._discover_skills(source)
        names = {skill.name for skill in discovered}
        assert SKILL in names
        assert MARKER_FILENAME not in names
        assert not any(name.startswith(".") for name in names)


class TestSkillsAddDoesNotInheritTheMarker:
    def test_cao_skills_add_strips_the_marker(self, world):
        """A user copying a projected skill must end up owning it outright.

        Carrying the marker over would leave their own directory verifying as a CAO
        projection — and therefore sweepable the next time a plugin went away.
        """
        import shutil

        from cli_agent_orchestrator.cli.commands.skills import _install_skill_folder

        _install(world)
        projected = world["skills_dir"] / SKILL
        assert (projected / MARKER_FILENAME).is_file()

        # What a user actually does: take the projected copy as a starting point
        # for a skill of their own. The marker comes along in *their* copy, so
        # `cao skills add` is the last place it can be stripped.
        # The folder name must keep matching the skill's own `name:`, so it is
        # staged under a different parent rather than renamed.
        staged = world["tmp_path"] / "staging" / SKILL
        shutil.copytree(projected, staged)
        assert (staged / MARKER_FILENAME).is_file()

        destination = _install_skill_folder(staged, force=True)

        assert (destination / "SKILL.md").is_file()
        assert not (destination / MARKER_FILENAME).exists()
