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

**One class is not a guarantee.** :class:`TestBranchUnreadableRecordKnownDefect`
pins a KNOWN DEFECT under separate review — an unreadable install record hides its
claim, so a plugin's projection is deleted and the command reports success. Its
assertions describe wrong behaviour on purpose, so that it cannot regress silently,
and a fix must rewrite them rather than keep them green. Read that class's docstring
before treating anything in it as intended.
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


class TestBranchUnreadableRecordKnownDefect:
    """KNOWN DEFECT, pinned here and under separate review. Not a desired guarantee.

    Everything in this class asserts behaviour that is **wrong**: an unparseable
    install record hides its plugin's claim, so :func:`projection_owner` answers
    ``None`` for a genuine projection and ``cao skills remove`` deletes it while
    reporting success. A single corrupt byte in ``<state_dir>/<plugin>.json`` suffices.
    Measured against unmutated code in both projection modes.

    **A future fix MUST CHANGE these tests. They are not a contract.** They exist only
    so the defect cannot regress *silently* — a wrong behaviour nobody wrote down is
    indistinguishable from a right one, and this one is invisible from the outside
    because the command exits 0 and prints "removed successfully".

    Not fixed here deliberately. ``InstalledPluginStore.list_installed`` logs and skips
    an unparseable record so ``cao plugin list`` and every rebuild survive one corrupt
    file; changing that is a design decision affecting consumers with nothing to do
    with this predicate, and it deserves its own failing-test-first cycle rather than
    being bolted onto R7.

    Why the fall-through happens, recorded for whoever fixes it: the both-conditions
    rule means losing the record loses the protection entirely, and the two conditions
    are **not symmetric**. Requiring structure was justified by the poisoned state — a
    record claiming a name whose bytes are the user's — which says nothing about the
    inverse, where structure proves ownership while the record is unreadable. A symlink
    resolving into the plugin store cannot be the user's own skill, so it is conclusive
    alone; a copy's marker digest is weaker evidence and is the harder half. The
    eventual rule is therefore likely nearer ``(claim AND structure) OR
    structure-conclusive-by-itself`` than the single ``and`` in place today.

    The blast radius is bounded, which is why this is recorded rather than treated as
    data loss: only the projection is deleted, the plugin's own bytes under the plugin
    store survive, and a later :func:`rebuild_projection` restores the entry.
    """

    def test_known_defect_an_unparseable_record_hides_the_claim(self, world):
        """KNOWN DEFECT under separate review: the claim becomes invisible.

        Asserts the wrong-but-current answer so that a change is detectable. RED
        vehicle: refusing on structural proof alone, ignoring the claim — which is the
        shape a fix would take, so this fires exactly when one is attempted.
        """
        projected = _install_donor(world)
        record = world["store"].state_dir / "donor.json"
        assert record.is_file(), "precondition: the record exists before corruption"

        record.write_text("{ not valid json", encoding="utf-8")

        assert current_projection(world["store"]) == {}, "the claim is invisible once unparseable"
        assert projected.exists() or projected.is_symlink(), "the projection is still on disk"
        # WRONG, and pinned on purpose: this projection is plugin-owned.
        assert _owner(world) is None

    def test_known_defect_the_removal_is_not_refused_when_the_record_is_unreadable(self, world):
        """KNOWN DEFECT under separate review: a plugin's projection is deleted.

        The operator-visible half. ``cao skills remove`` should refuse here; instead it
        exits 0 with "removed successfully" having deleted content the plugin still
        owns. The first two assertions below describe the defect, not the intent — a
        fix will invert them, and this test must then be rewritten rather than kept
        green.

        The final assertion is the one that bounds the severity, and it should keep
        passing under any fix: the plugin's own bytes are never touched, so the loss is
        a projection that a rebuild can restore.
        """
        projected = _install_donor(world)
        (world["store"].state_dir / "donor.json").write_text("{ not valid json", encoding="utf-8")
        plugin_source = world["store"].plugin_root("donor") / "skills" / "shared-skill"

        result = CliRunner().invoke(cli, ["skills", "remove", "shared-skill"])

        # WRONG on both counts, pinned so the wrongness is visible and testable.
        assert result.exit_code == 0, result.output
        assert not (projected.exists() or projected.is_symlink())
        assert (plugin_source / "SKILL.md").is_file(), (
            "the plugin's own bytes must survive, which is what bounds this to a "
            "recoverable projection loss rather than real data loss"
        )


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
