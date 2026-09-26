"""Descendants of a *permitted* directory symlink — review 5222539218 item 2, reopened.

``_validate_skill_tree`` walked with ``followlinks=False``, so it inspected a
contained directory symlink and then **never looked inside it**. The reviewer's
reproduction: ``skills/inspection/assets -> ../../shared`` passes containment, an
external symlink *inside* ``shared`` is never checked, and copy-mode projection's
``copytree(..., symlinks=False)`` then dereferences the whole chain and writes the
external content into the live skill store — with zero validation findings.

Everything here drives the **real installer** in copy mode rather than calling the
validator directly, because the defect is exactly that validation and projection
disagreed about what a copy touches. Asserting on the projected tree is what
catches that; asserting on a validator return value is what missed it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource

from .conftest import build_plugin, write_skill

#: A file that exists on every platform CI runs on and is not part of any package.
EXTERNAL_FILE = Path("/etc/passwd")


@pytest.fixture
def copy_mode(monkeypatch):
    """Project in ``copy`` mode — the mode that dereferences symlinks."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_skill_projection_mode",
        lambda: "copy",
    )


def codes(report) -> list:
    return [f.code for f in report.findings]


def do_install(source: Path, store, skills_dir):
    return install(
        PluginSource(kind="path", location=str(source)),
        store=store,
        skills_dir=skills_dir,
        refresh_agents=False,
    )


def live_files(skills_dir: Path) -> list:
    """Every regular file the projection actually wrote, as relative posix paths."""
    return sorted(
        p.relative_to(skills_dir).as_posix() for p in skills_dir.rglob("*") if p.is_file()
    )


def build_indirect_plugin(tmp_path: Path, *, link_target: Path) -> Path:
    """The reviewer's shape: a contained dir link whose target holds an escaping link.

    ``skills/inspection/assets`` is a **relative** symlink on purpose. The resolver
    stages a local source with ``copytree(symlinks=True)``, so a relative link keeps
    resolving inside the staged root — which is what makes it pass containment and
    reach the blind spot. An absolute link into the original source tree would be
    rejected for an unrelated reason and would not exercise this path at all.
    """
    root = build_plugin(tmp_path / "src", "demo")
    write_skill(root / "skills" / "inspection", "inspection")
    (root / "shared").mkdir()
    (root / "shared" / "keep.txt").write_text("contained", encoding="utf-8")
    (root / "shared" / "evil").symlink_to(link_target, target_is_directory=link_target.is_dir())
    (root / "skills" / "inspection" / "assets").symlink_to(
        Path("..") / ".." / "shared", target_is_directory=True
    )
    return root


class TestDescendantsOfAContainedDirectoryLinkAreValidated:
    """§4.1 has to hold for what a copy *reaches*, not for what one walk enumerates."""

    def test_an_external_file_link_inside_a_contained_dir_link_refuses_the_skill(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """Reviewer reproduction, external FILE symlink."""
        root = build_indirect_plugin(tmp_path, link_target=EXTERNAL_FILE)

        outcome = do_install(root, store, skills_dir)

        assert "skill.link_escapes_root" in codes(outcome.report), (
            "the escaping link lives under a PERMITTED directory symlink; a walk that "
            "does not follow it reports nothing at all"
        )
        assert outcome.report.skill_names == ()
        assert outcome.record is None or outcome.record.projected_skill_names == ()
        assert not (skills_dir / "inspection").exists(), "nothing may be published"

    def test_an_external_dir_link_inside_a_contained_dir_link_refuses_the_skill(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """Same shape, external DIRECTORY symlink — the form ``copytree`` recurses into."""
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "loot.txt").write_text("exfiltrated", encoding="utf-8")
        root = build_indirect_plugin(tmp_path, link_target=outside)

        outcome = do_install(root, store, skills_dir)

        assert "skill.link_escapes_root" in codes(outcome.report)
        assert outcome.report.skill_names == ()
        assert not (skills_dir / "inspection").exists()

    def test_the_live_skill_store_holds_none_of_the_external_content(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """The consequence that matters: what ended up on disk under ``SKILLS_DIR``.

        Asserted by *content*, not only by path. ``copytree(symlinks=False)``
        materializes the target's bytes under a name the package chose, so a path
        assertion alone would pass against a differently-named leak.
        """
        outside = tmp_path / "outside-dir"
        outside.mkdir()
        (outside / "loot.txt").write_text("exfiltrated", encoding="utf-8")
        root = build_indirect_plugin(tmp_path, link_target=outside)

        do_install(root, store, skills_dir)

        assert live_files(skills_dir) == [], f"projection wrote {live_files(skills_dir)}"
        leaked = [
            p for p in skills_dir.rglob("*") if p.is_file() and p.read_bytes() == b"exfiltrated"
        ]
        assert leaked == [], f"external content was copied into the skill store: {leaked}"

    def test_a_sibling_skill_still_installs(self, store, skills_dir, tmp_path, copy_mode):
        """§7.2.2.2: one bad skill is skipped, it does not condemn the package."""
        root = build_indirect_plugin(tmp_path, link_target=EXTERNAL_FILE)
        write_skill(root / "skills" / "beta", "beta")

        outcome = do_install(root, store, skills_dir)

        assert outcome.installed
        assert outcome.report.skill_names == ("beta",)
        assert (skills_dir / "beta" / "SKILL.md").is_file()
        assert not (skills_dir / "inspection").exists()


class TestCyclesTerminateWithAFinding:
    """Mandatory: a loop must produce a finding, never ``RecursionError`` or a hang."""

    def test_a_link_back_to_an_ancestor_through_a_contained_dir_link_is_a_cycle(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """``skills/alpha/assets -> ../../shared`` and ``shared/back -> ../skills/alpha``.

        ``copytree(symlinks=False)`` follows both and never terminates, so validation
        has to recognize the loop rather than discover it by exhausting the stack.
        """
        root = build_plugin(tmp_path / "src", "demo")
        write_skill(root / "skills" / "alpha", "alpha")
        (root / "shared").mkdir()
        (root / "shared" / "back").symlink_to(
            Path("..") / "skills" / "alpha", target_is_directory=True
        )
        (root / "skills" / "alpha" / "assets").symlink_to(
            Path("..") / ".." / "shared", target_is_directory=True
        )

        outcome = do_install(root, store, skills_dir)

        assert "skill.link_cycle" in codes(outcome.report)
        assert outcome.report.skill_names == ()
        assert not (skills_dir / "alpha").exists()

    def test_two_contained_dir_links_pointing_at_each_other_is_a_cycle(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """The ``a -> b``, ``b -> a`` form, reached from inside a skill."""
        root = build_plugin(tmp_path / "src", "demo")
        write_skill(root / "skills" / "alpha", "alpha")
        (root / "one").mkdir()
        (root / "two").mkdir()
        (root / "one" / "to_two").symlink_to(Path("..") / "two", target_is_directory=True)
        (root / "two" / "to_one").symlink_to(Path("..") / "one", target_is_directory=True)
        (root / "skills" / "alpha" / "assets").symlink_to(
            Path("..") / ".." / "one", target_is_directory=True
        )

        outcome = do_install(root, store, skills_dir)

        assert "skill.link_cycle" in codes(outcome.report)
        assert not (skills_dir / "alpha").exists()

    def test_a_chain_far_deeper_than_the_recursion_limit_still_answers(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """Depth must not be the thing that decides the answer.

        A chain of contained directory symlinks longer than ``sys.getrecursionlimit()``
        is trivial for a hostile package to ship. A recursive validator answers it with
        ``RecursionError`` -- an exception, from a function whose whole contract (P1) is
        that it never raises -- so the walk is iterative and this is the regression guard.
        The chain is clean, so the *correct* answer is a clean install; the point is that
        an answer arrives at all.
        """
        depth = sys.getrecursionlimit() + 50
        root = build_plugin(tmp_path / "src", "demo")
        write_skill(root / "skills" / "alpha", "alpha")
        for index in range(depth):
            (root / f"d{index}").mkdir()
        for index in range(depth - 1):
            (root / f"d{index}" / "next").symlink_to(
                Path("..") / f"d{index + 1}", target_is_directory=True
            )
        (root / f"d{depth - 1}" / "end.txt").write_text("bottom", encoding="utf-8")
        (root / "skills" / "alpha" / "assets").symlink_to(
            Path("..") / ".." / "d0", target_is_directory=True
        )

        outcome = do_install(root, store, skills_dir)

        assert outcome.installed
        assert outcome.report.skill_names == ("alpha",)


class TestContainedOnlyNestingStillInstalls:
    """The control. This is what makes the defect quiet, and it must keep working."""

    def test_a_contained_nested_dir_link_installs_cleanly(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """Every link in the chain resolves inside the root, so §4.1 permits it."""
        root = build_plugin(tmp_path / "src", "demo")
        write_skill(root / "skills" / "alpha", "alpha")
        (root / "shared" / "deep").mkdir(parents=True)
        (root / "shared" / "deep" / "note.txt").write_text("fine", encoding="utf-8")
        (root / "shared" / "alias").symlink_to(Path("deep"), target_is_directory=True)
        (root / "skills" / "alpha" / "assets").symlink_to(
            Path("..") / ".." / "shared", target_is_directory=True
        )

        outcome = do_install(root, store, skills_dir)

        assert outcome.installed
        assert "skill.link_escapes_root" not in codes(outcome.report)
        assert "skill.link_cycle" not in codes(outcome.report)
        assert outcome.report.skill_names == ("alpha",)
        # Projected by dereference, so the contained content is genuinely there.
        assert (skills_dir / "alpha" / "assets" / "deep" / "note.txt").read_text() == "fine"

    def test_two_links_to_the_same_contained_dir_are_not_a_cycle(
        self, store, skills_dir, tmp_path, copy_mode
    ):
        """A diamond is not a loop; the visited set must not conflate them."""
        root = build_plugin(tmp_path / "src", "demo")
        write_skill(root / "skills" / "alpha", "alpha")
        (root / "shared").mkdir()
        (root / "shared" / "note.txt").write_text("fine", encoding="utf-8")
        for name in ("first", "second"):
            (root / "skills" / "alpha" / name).symlink_to(
                Path("..") / ".." / "shared", target_is_directory=True
            )

        outcome = do_install(root, store, skills_dir)

        assert outcome.installed
        assert "skill.link_cycle" not in codes(outcome.report)
        assert outcome.report.skill_names == ("alpha",)
