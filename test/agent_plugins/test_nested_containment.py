"""Nested package content and failed-copy debris.

Reported by review 5222539218 on #584 (item 2): copy mode called
``copytree(..., symlinks=False)`` before the new digest was computed, so a nested
symlink to an external file was copied in successfully, and a distinct
dangling-link case left a live ``SKILL.md`` with no ownership metadata -- which a
retry then misclassified as a user-owned collision.

Two halves, and both are needed: validate recursively accessed content *before*
copying, and stage the copy so a failure leaves nothing behind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins import projection as projection_mod
from cli_agent_orchestrator.agent_plugins.projection import rebuild_projection
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

from .conftest import build_plugin, write_skill
from .test_store import make_record


def codes(report) -> list:
    return [f.code for f in report.findings]


class TestNestedLinksAreValidatedBeforeAnythingIsCopied:
    """§4.1 containment applied to the whole tree, not just the skill's top level."""

    def test_a_nested_link_to_an_external_file_skips_only_that_skill(self, tmp_path):
        """The reported defect: ``symlinks=False`` would have copied the target in."""
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        (root / "skills" / "alpha" / "leak.txt").symlink_to(outside)

        report = validate_plugin(root)

        assert report.loadable, "one bad skill must not condemn the package"
        assert report.skill_names == ("beta",)
        assert "skill.link_escapes_root" in codes(report)

    def test_a_nested_dangling_link_skips_only_that_skill(self, tmp_path):
        """The reviewer's 'distinct dangling-link case'.

        ``copytree(symlinks=False)`` raises on a dangling link *partway through*,
        which is what used to leave debris.
        """
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        (root / "skills" / "alpha" / "gone.txt").symlink_to(root / "skills" / "alpha" / "nope")

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("beta",)
        assert "skill.link_dangling" in codes(report)

    def test_a_nested_directory_link_to_an_ancestor_is_a_cycle(self, tmp_path):
        """An unbounded walk for anything that recurses, so it is refused."""
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        (root / "skills" / "alpha" / "loop").symlink_to(
            root / "skills" / "alpha", target_is_directory=True
        )

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("beta",)
        assert "skill.link_cycle" in codes(report)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
    def test_a_special_file_skips_only_that_skill(self, tmp_path):
        """A FIFO is not packageable content, and reading one can block forever."""
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        os.mkfifo(root / "skills" / "alpha" / "pipe")

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("beta",)
        assert "skill.special_entry" in codes(report)

    def test_a_nested_contained_link_is_still_permitted(self, tmp_path):
        """§4.1 permits containment, so this must not become collateral damage."""
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        (root / "shared.txt").write_text("ok", encoding="utf-8")
        (root / "skills" / "alpha" / "ref.txt").symlink_to(root / "shared.txt")

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("alpha",)
        assert "skill.link_escapes_root" not in codes(report)

    def test_the_skill_directory_itself_may_still_be_a_contained_symlink(self, tmp_path):
        """Regression guard for the rule's own blind spot.

        ``os.walk`` always descends the directory it is *given*, even with
        ``followlinks=False``. So the containment rules must apply to NESTED links
        only -- ``test_skill_symlink_inside_the_root_is_permitted`` covers exactly
        this shape and §4.1 permits it.
        """
        root = build_plugin(tmp_path / "p", "demo")
        (root / "skills").mkdir()
        real = root / "_sources" / "gamma"
        write_skill(real, "gamma")
        (root / "skills" / "gamma").symlink_to(real, target_is_directory=True)

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == ("gamma",)


class TestAFailedCopyLeavesNothingBehind:
    """The debris half of item 2."""

    def test_a_failure_after_the_copy_leaves_no_unmarked_debris(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        """The reviewer's misclassification chain, cut at its first link.

        Debris at the projected path with no marker is indistinguishable from a
        skill the *user* created, so the retry refuses to touch it and the plugin
        silently never projects. Staging means the projected path is only ever
        written by an ``os.rename`` of a complete tree.
        """
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "copy",
        )
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))

        # Failure injected INSIDE copytree, after it has written part of the tree.
        # That is the reviewer's actual case -- a dangling nested link makes
        # copytree raise mid-tree -- and it is what leaves a live SKILL.md with no
        # marker. Failing at the marker write instead would not reproduce it,
        # because the tree is complete by then.
        real_copytree = projection_mod.shutil.copytree

        def partial_then_fail(src, dst, *args, **kwargs):
            dst_path = Path(dst)
            dst_path.mkdir(parents=True, exist_ok=True)
            (dst_path / "SKILL.md").write_text(
                "---\nname: alpha\ndescription: d\n---\n\nx\n", encoding="utf-8"
            )
            raise OSError("no space left on device")

        monkeypatch.setattr(projection_mod.shutil, "copytree", partial_then_fail)
        result = rebuild_projection(store, skills_dir=skills_dir, mode="copy")

        assert "projection.write_failed" in [f.code for f in result.findings]
        assert not (skills_dir / "alpha").exists(), (
            "a partially written projection must not survive: unmarked debris is "
            "indistinguishable from a user-owned skill and blocks every retry"
        )

    def test_a_retry_after_a_failure_projects_normally(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        """The consequence that actually matters to an operator."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
            lambda: "copy",
        )
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))

        calls = {"n": 0}
        real_copytree = projection_mod.shutil.copytree

        def fail_once(src, dst, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                dst_path = Path(dst)
                dst_path.mkdir(parents=True, exist_ok=True)
                (dst_path / "SKILL.md").write_text("partial", encoding="utf-8")
                raise OSError("transient")
            return real_copytree(src, dst, *args, **kwargs)

        monkeypatch.setattr(projection_mod.shutil, "copytree", fail_once)
        rebuild_projection(store, skills_dir=skills_dir, mode="copy")
        monkeypatch.setattr(projection_mod.shutil, "copytree", real_copytree)
        result = rebuild_projection(store, skills_dir=skills_dir, mode="copy")

        assert (skills_dir / "alpha" / "SKILL.md").is_file()
        assert "skill.collision" not in [f.code for f in result.findings]

    def test_staging_happens_outside_the_skills_directory(self, store, skills_dir, tmp_path):
        """Decided in the spec's §7.1: nothing transient under ``SKILLS_DIR``.

        Kiro CLI is handed ``skill://{SKILLS_DIR}/**/SKILL.md`` and resolves it
        itself with ``globset`` + ``walkdir`` -- neither of which excludes
        dot-prefixed components -- so a staged tree under ``SKILLS_DIR`` could be
        loaded mid-copy. Staging one level up removes the question rather than
        reasoning about it, and costs nothing: ``os.rename`` needs the same
        *filesystem*, not the same directory.
        """
        from cli_agent_orchestrator.constants import SKILLS_DIR

        assert projection_mod.STAGING_DIRNAME.startswith(".")

        # Checked for BOTH the real default and an arbitrary target, because the two
        # are what the rule has to hold for: the shipped default, and any projection
        # target an operator redirects to.
        for target in (SKILLS_DIR, skills_dir):
            staging = projection_mod._staging_root(target)
            assert not staging.is_relative_to(target), (
                f"staging {staging} is under the skills dir {target}; Kiro's "
                f"skill://<skills>/**/SKILL.md glob (globset + walkdir, neither of "
                f"which excludes dot components) could then load a half-copied skill"
            )
            assert staging.parent == target.parent, (
                "staging must be a SIBLING of the target: os.rename needs the same "
                "filesystem, and a sibling is on the destination's filesystem by "
                "construction. A fixed root would raise EXDEV for a target on "
                "another mount."
            )
            assert staging.name == projection_mod.STAGING_DIRNAME

    def test_staging_never_touches_the_real_home_when_the_target_is_redirected(self, skills_dir):
        """Derived from the target, so redirecting the target redirects staging.

        Without this, a suite that patched only the skills directory still created
        a staging directory in the operator's real home -- which is what happened.
        """
        from cli_agent_orchestrator.constants import CAO_HOME_DIR

        staging = projection_mod._staging_root(skills_dir)
        assert not staging.is_relative_to(CAO_HOME_DIR)
        assert staging.is_relative_to(skills_dir.parent)

    def test_a_leftover_staging_tree_is_cleared_by_the_next_rebuild(
        self, store, skills_dir, tmp_path
    ):
        """A killed process cannot leave staging debris to accumulate forever."""
        staging = projection_mod._staging_root(skills_dir)
        staging.mkdir(parents=True, exist_ok=True)
        leftover = staging / "alpha.99999.deadbeef"
        leftover.mkdir()
        (leftover / "SKILL.md").write_text("stale", encoding="utf-8")

        rebuild_projection(store, skills_dir=skills_dir)

        assert not leftover.exists(), "the rebuild clears staging leftovers under the lock"
