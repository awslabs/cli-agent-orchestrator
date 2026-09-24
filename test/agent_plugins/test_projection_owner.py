"""Direct unit tests for :func:`projection_owner` — the gate on a destructive path.

``cao skills remove`` asks this predicate whether an entry is plugin-owned, and
deletes when the answer is ``None``. Until this file existed, its entire coverage
was one incidental CLI-level assertion in ``test_skills_cli_guards.py``, which
exercised three of its four branches and never called it directly. A predicate that
authorises a delete needs its own tests.

**The dangerous direction is UNDER-claiming, not over-claiming.** If the structural
half wrongly answers False for content that genuinely is a plugin projection, this
returns ``None``, the refusal never fires, and ``cao skills remove`` deletes
plugin-owned content while reporting success. Over-claiming merely refuses a removal
that should have been allowed. So the tests that matter here are the ones asserting
a non-``None`` answer, plus :class:`TestARefusalPreservesTheContent`, which asserts
the bytes are still on disk rather than only that the exit code was non-zero.

Every test here was seen to FAIL against a mutation that breaks the branch it
covers, before being seen to pass — the mutation is named in each docstring.

**Record readability is not part of the contract.**
:class:`TestBranchUnreadableRecordRefusesRemoval` covers the case where the install
record will not parse, so no claim is visible, while the on-disk entry still proves
itself CAO's. These assert REFUSAL: a corrupt state file must not become a licence
to delete a plugin's projection. That class previously pinned the opposite as a known
defect (issue #797); the assertions were inverted when the defect was fixed, so a
reader who remembers the old prose should re-read them rather than the memory.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.projection import (
    MARKER_FILENAME,
    current_projection,
    projection_owner,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.cli.main import cli

from .conftest import build_plugin

USER_MARKER = "# the user's own version"


def _write_user_skill(folder: Path, name: str) -> Path:
    """A valid skill folder that no plugin has ever owned."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: A user-authored skill.\n---\n\n{USER_MARKER}\n",
        encoding="utf-8",
    )
    return folder


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp-backed plugin store and skill store, with the CLI pointed at both."""
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

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


@pytest.fixture
def copy_mode(monkeypatch):
    """Project copies rather than symlinks, so the marker path is exercised."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
        lambda: "copy",
    )


def _install_donor(world, skill_name: str = "shared-skill", *, name: str = "donor") -> Path:
    """Install a one-skill plugin, returning the projected path."""
    source = build_plugin(
        world["tmp_path"] / f"src-{name}", name, skills=[skill_name], version="1.0.0"
    )
    install(
        PluginSource(kind="path", location=str(source)),
        store=world["store"],
        skills_dir=world["skills_dir"],
        force=True,
        refresh_agents=False,
    )
    return world["skills_dir"] / skill_name


def _owner(world, skill_name: str = "shared-skill"):
    """Call the predicate under test with both roots pinned to the scratch tree."""
    return projection_owner(skill_name, world["store"], skills_dir=world["skills_dir"])


class TestBranchNoClaim:
    """Branch 1 — no installed record claims the name."""

    def test_an_unclaimed_name_has_no_owner(self, world):
        """RED vehicle: returning a non-``None`` owner when ``claimed`` is falsy.

        Without this, an implementation that answered "owned" for every name would
        make every skill permanently unremovable and nothing would notice.
        """
        _write_user_skill(world["skills_dir"] / "mine-alone", "mine-alone")

        assert current_projection(world["store"]) == {}
        assert _owner(world, "mine-alone") is None

    def test_a_name_that_does_not_exist_at_all_has_no_owner(self, world):
        """The predicate must not require the path to exist to answer safely."""
        assert _owner(world, "never-heard-of-it") is None


class TestBranchClaimWithProof:
    """Branch 2 — a record claims it AND the bytes are provably CAO's.

    THE UNDER-CLAIMING DIRECTION. These are the two tests that stand between a
    broken structural check and ``cao skills remove`` deleting plugin content.
    """

    def test_a_symlink_projection_is_owned_by_the_claiming_plugin(self, world):
        """RED vehicle: forcing the ``_is_managed_projection`` result to False.

        With that mutation this returns ``None`` and the CLI deletes the projection
        while printing "removed successfully" — measured, not assumed.
        """
        projected = _install_donor(world)
        if not projected.is_symlink():
            pytest.skip("symlink projection unavailable in this environment")

        assert current_projection(world["store"]) == {"shared-skill": "donor"}
        assert _owner(world) == "donor"

    def test_a_copy_mode_projection_with_a_verified_marker_is_owned(self, world, copy_mode):
        """Same mutation, the other materialization mode.

        Copy mode is the more dangerous of the two: deleting a symlink costs only the
        link, whereas ``rmtree`` on a copy removes real bytes from the skill store.
        """
        projected = _install_donor(world)
        assert projected.is_dir() and not projected.is_symlink()
        assert (projected / MARKER_FILENAME).is_file(), "precondition: the marker was written"

        assert _owner(world) == "donor"

    def test_ownership_survives_an_edit_to_an_unrelated_skill(self, world):
        """Ownership is per-path, so unrelated churn must not disturb the answer."""
        projected = _install_donor(world)
        if not projected.is_symlink():
            pytest.skip("symlink projection unavailable in this environment")
        _write_user_skill(world["skills_dir"] / "unrelated", "unrelated")

        assert _owner(world) == "donor"
        assert _owner(world, "unrelated") is None


class TestBranchClaimWithoutProof:
    """Branch 3 — the poisoned state: a live claim over the user's own directory.

    Reachable after a ``release_projection_claim`` that failed: the record still
    names the skill while what is on disk is the user's. Refusing on the claim alone
    would leave the user unable to remove their own directory and unable to find out
    why. This is the both-conditions rule, and it is the branch that most needs a
    direct test — the CLI-level version of it is the single assertion out of 1581
    that catches a claim-only implementation.
    """

    def test_a_live_claim_over_a_user_directory_has_no_owner(self, world):
        """RED vehicle: returning ``claimed`` as soon as the record claims the name.

        The assertion on ``current_projection`` is what makes this non-vacuous: it
        proves the claim is genuinely still live, so a ``None`` answer is the
        structural half doing its job rather than the claim having quietly vanished.
        """
        projected = _install_donor(world)
        if projected.is_symlink():
            projected.unlink()
        else:
            shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")

        assert current_projection(world["store"]) == {
            "shared-skill": "donor"
        }, "precondition: the record must still claim the name, or this test proves nothing"
        assert _owner(world) is None

    def test_a_regular_file_at_a_claimed_name_has_no_owner(self, world):
        """CAO never projects a file, so a file at a claimed name is somebody else's."""
        projected = _install_donor(world)
        if projected.is_symlink():
            projected.unlink()
        else:
            shutil.rmtree(projected)
        projected.write_text("the user's note", encoding="utf-8")

        assert current_projection(world["store"]) == {"shared-skill": "donor"}
        assert _owner(world) is None


class TestBranchUnreadableRecordRefusesRemoval:
    """Branch 4 — the record will not parse, and the disk proves ownership anyway.

    ``InstalledPluginStore.list_installed`` logs and SKIPS a record whose JSON will
    not parse, deliberately, so that ``cao plugin list`` and every rebuild survive
    one corrupt file. That policy is unchanged and other consumers still depend on
    it. What changed is that :func:`projection_owner` no longer takes a missing claim
    as the end of the enquiry: it asks the filesystem independently, and structurally
    conclusive evidence refuses the removal on its own.

    This class used to pin the opposite as a known defect (issue #797) — a single
    corrupt byte in ``<state_dir>/<plugin>.json`` made ``cao skills remove`` delete a
    genuine projection and exit 0 reporting success. The reviewer rejected that
    decision, so the assertions here are inverted from the ones that shipped with R7.

    The evidence is asymmetric, which is why a missing claim can be overridden but a
    missing *structure* cannot. A symlink resolving into the plugin store, or a copy
    whose marker digest still verifies against a source inside the plugin store,
    cannot be content the user authored — only the projection engine puts those
    there, and either one also NAMES the owning plugin, from the store-relative path,
    without consulting a record. A bare claim over a directory the user owns remains
    insufficient in the other direction: see
    :class:`TestBranchClaimWithoutProof`, and the control at the end of this class.
    """

    def test_an_unparseable_record_does_not_hide_structural_ownership(self, world):
        """RED vehicle: deriving ownership from the records alone.

        Against the pre-fix code this returned ``None``. The assertion on
        ``current_projection`` is what makes the test non-vacuous — it proves the
        claim really is invisible, so a ``"donor"`` answer can only have come from
        the structural path.
        """
        projected = _install_donor(world)
        if not projected.is_symlink():
            pytest.skip("symlink projection unavailable in this environment")
        record = world["store"].state_dir / "donor.json"
        assert record.is_file(), "precondition: the record exists before corruption"

        record.write_text("{ not valid json", encoding="utf-8")

        assert current_projection(world["store"]) == {}, "the claim is invisible once unparseable"
        assert _owner(world) == "donor"

    def test_copy_mode_ownership_survives_an_unparseable_record(self, world, copy_mode):
        """Same mutation, the mode where a wrong answer costs real bytes.

        Deleting a symlink costs the link; ``rmtree`` on a copy-mode projection
        removes content from the skill store. The verified marker is the evidence
        here, and it names the plugin from its own recorded source path.
        """
        projected = _install_donor(world)
        assert projected.is_dir() and not projected.is_symlink()
        assert (projected / MARKER_FILENAME).is_file(), "precondition: the marker was written"

        (world["store"].state_dir / "donor.json").write_text("{ not valid json", encoding="utf-8")

        assert current_projection(world["store"]) == {}
        assert _owner(world) == "donor"

    def test_the_removal_is_refused_when_the_record_is_unreadable(self, world):
        """The operator-visible half: ``cao skills remove`` must refuse, not report success.

        Content first, exit code second, for the reason
        :class:`TestARefusalPreservesTheContent` explains: the property is the bytes.
        """
        projected = _install_donor(world)
        (world["store"].state_dir / "donor.json").write_text("{ not valid json", encoding="utf-8")
        plugin_source = world["store"].plugin_root("donor") / "skills" / "shared-skill"

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        assert (
            projected.exists() or projected.is_symlink()
        ), "the projection was deleted because one corrupt state file hid the claim"
        assert result.exit_code != 0, result.output
        assert "cao plugin remove donor" in result.output
        assert (plugin_source / "SKILL.md").is_file()

    def test_a_user_owned_skill_is_still_removable_while_a_record_is_corrupt(self, world):
        """Control: the fix must not make a corrupt record freeze unrelated names.

        Over-claiming is the safe direction but it is not free — an operator who can
        no longer remove their own skills has a different outage.
        """
        _install_donor(world)
        (world["store"].state_dir / "donor.json").write_text("{ not valid json", encoding="utf-8")
        mine = _write_user_skill(world["skills_dir"] / "mine-alone", "mine-alone")

        result = CliRunner().invoke(cli, ["skills", "remove", "mine-alone"])

        assert result.exit_code == 0, result.output
        assert not mine.exists()

    def test_a_user_directory_at_a_claimed_name_is_still_removable(self, world):
        """Control: structure decides, so the poisoned state stays removable.

        The record claims ``shared-skill`` AND is unreadable AND the directory is the
        user's. Neither half of the evidence holds, so the removal must go through —
        an implementation that refused on a corrupt record alone would fail here.
        """
        projected = _install_donor(world)
        if projected.is_symlink():
            projected.unlink()
        else:
            shutil.rmtree(projected)
        _write_user_skill(projected, "shared-skill")
        (world["store"].state_dir / "donor.json").write_text("{ not valid json", encoding="utf-8")

        assert _owner(world) is None
        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        assert result.exit_code == 0, result.output
        assert not projected.exists()


class TestARefusalPreservesTheContent:
    """The assertion the CLI tests structurally cannot make first.

    ``test_skills_cli_guards.py`` asserts survival, but *after* ``exit_code != 0``,
    so a mutant that under-claims trips the exit-code assertion and the survival
    check never runs. Here survival is asserted BEFORE anything about the exit code,
    so these tests fail on the fact that matters — the bytes — rather than on the
    symptom.
    """

    def test_the_symlink_projection_is_still_on_disk_after_a_refusal(self, world):
        projected = _install_donor(world)
        if not projected.is_symlink():
            pytest.skip("symlink projection unavailable in this environment")
        target_before = projected.resolve()

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        # Content first, deliberately: this is the property, the exit code is evidence.
        assert projected.is_symlink(), "the projection was deleted despite being plugin-owned"
        assert projected.resolve() == target_before
        assert (projected / "SKILL.md").is_file()
        assert result.exit_code != 0
        assert "cao plugin remove donor" in result.output

    def test_the_copied_projection_bytes_are_still_on_disk_after_a_refusal(self, world, copy_mode):
        projected = _install_donor(world)
        assert projected.is_dir() and not projected.is_symlink()
        digest_before = (projected / "SKILL.md").read_bytes()

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        assert projected.is_dir(), "rmtree destroyed a copy-mode projection"
        assert (projected / "SKILL.md").read_bytes() == digest_before
        assert (projected / MARKER_FILENAME).is_file(), "the marker was removed"
        assert result.exit_code != 0
        assert "cao plugin remove donor" in result.output
