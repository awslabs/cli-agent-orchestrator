"""The last uncovered branches — name safety, projection modes, live-session scan.

Grouped by the guarantee each one backs:

* **Name safety** is the store's second, independent guard at the point a name
  becomes a path (§5.5 already constrains manifest names, but a store must not
  trust its callers).
* **Projection modes** — symlink and copy must both replace whatever is already at
  the target, including a leftover of the *other* mode.
* **The live-session scan** must never fail a removal: a terminal with no profile,
  or a profile deleted since launch, is normal operational reality.
"""

from __future__ import annotations

import shutil

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins import installer as installer_mod
from cli_agent_orchestrator.agent_plugins import projection as projection_mod
from cli_agent_orchestrator.agent_plugins.installer import affected_sessions
from cli_agent_orchestrator.agent_plugins.projection import (
    rebuild_projection,
    release_projection_claim,
)
from cli_agent_orchestrator.agent_plugins.store import _validate_plugin_dirname

from .conftest import build_plugin
from .test_store import make_record


class TestPluginNameSafetyAtThePathBoundary:
    @pytest.mark.parametrize(
        "name,reason",
        [
            ("a\x00b", "NUL byte"),
            ("a/b", "path separator"),
            ("a\\b", "path separator"),
            ("..", "'\\.\\.'"),
            ("a..b", "'\\.\\.'"),
        ],
    )
    def test_each_unsafe_shape_is_rejected_with_its_own_reason(self, name, reason):
        """The message must say which rule was broken, not just 'invalid'."""
        with pytest.raises(ValueError, match=reason):
            _validate_plugin_dirname(name)

    @pytest.mark.parametrize("name", ["cao", "cao-contributor", "a.b-c", "x0"])
    def test_conformant_names_pass(self, name):
        assert _validate_plugin_dirname(name) == name


class TestProjectionReplacesWhateverIsAtTheTarget:
    def _publish(self, store, tmp_path, skill="alpha"):
        source = build_plugin(tmp_path / "src", "demo", skills=[skill])
        store.publish(source, make_record("demo", skill_names=(skill,)))
        return source

    def test_symlink_mode_replaces_a_leftover_copy_directory(self, store, skills_dir, tmp_path):
        """Switching copy -> symlink must not leave the stale copy in place.

        Amended by review 3 on #584. The leftover is now produced by a real
        copy-mode rebuild rather than hand-built with different content, because
        that is what the migration path actually leaves behind — and after this
        review, what CAO may replace is exactly what it can prove it wrote. A
        hand-made directory with different bytes is somebody else's data and is
        covered by ``test_an_edited_copy_at_a_projected_name_is_refused`` below.
        """
        self._publish(store, tmp_path)
        first = rebuild_projection(store, skills_dir=skills_dir, mode="copy")
        assert first.projected.get("alpha") == "demo"
        assert not (skills_dir / "alpha").is_symlink()

        result = rebuild_projection(store, skills_dir=skills_dir, mode="symlink")

        assert result.projected.get("alpha") == "demo"
        assert (skills_dir / "alpha").is_symlink()

    def test_an_edited_copy_at_a_projected_name_is_refused(self, store, skills_dir, tmp_path):
        """Reported by review 3 on #584: a name claim cannot prove the bytes are CAO's.

        A copied projection the user edited in place is theirs now. Replacing it
        would silently destroy that edit, which is the same data loss the sweep
        side of this finding describes.
        """
        self._publish(store, tmp_path)
        rebuild_projection(store, skills_dir=skills_dir, mode="copy")
        (skills_dir / "alpha" / "SKILL.md").write_text("my own edit", encoding="utf-8")

        result = rebuild_projection(store, skills_dir=skills_dir, mode="symlink")

        assert "alpha" not in result.projected
        assert not (skills_dir / "alpha").is_symlink()
        assert (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8") == "my own edit"

    def test_symlink_mode_replaces_its_own_stale_symlink(self, store, skills_dir, tmp_path):
        """A symlink into the plugin store is structurally CAO's, so it is retargeted.

        This is the winner-transition path: the name moves between plugins and the
        old link must be repointed, not preserved.
        """
        self._publish(store, tmp_path)
        other_source = build_plugin(tmp_path / "src-other", "aaa-other", skills=["alpha"])
        store.publish(other_source, make_record("aaa-other", skill_names=("alpha",)))
        (skills_dir / "alpha").symlink_to(
            store.plugin_root("demo") / "skills" / "alpha", target_is_directory=True
        )

        rebuild_projection(store, skills_dir=skills_dir, mode="symlink")

        resolved = (skills_dir / "alpha").resolve()
        # `aaa-other` sorts first, so it wins the name.
        assert str(store.plugin_root("aaa-other")) in str(resolved)

    def test_a_symlink_pointing_outside_the_store_is_refused(self, store, skills_dir, tmp_path):
        """Amended by review 3 on #584: materialize and sweep now agree here.

        The sweep already refused a foreign symlink; ``_materialize`` replaced one,
        because its guard asked "is this a directory we did not claim" and a
        symlink is not a directory. A user who symlinked their own skill into the
        skill store had it silently replaced. One predicate, one answer.
        """
        self._publish(store, tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "SKILL.md").write_text("mine", encoding="utf-8")
        (skills_dir / "alpha").symlink_to(elsewhere, target_is_directory=True)

        result = rebuild_projection(store, skills_dir=skills_dir, mode="symlink")

        assert "alpha" not in result.projected
        assert (skills_dir / "alpha").resolve() == elsewhere.resolve()

    def test_copy_mode_replaces_a_leftover_store_symlink(self, store, skills_dir, tmp_path):
        """The reverse migration: symlink -> copy replaces CAO's own link."""
        self._publish(store, tmp_path)
        (skills_dir / "alpha").symlink_to(
            store.plugin_root("demo") / "skills" / "alpha", target_is_directory=True
        )

        result = rebuild_projection(store, skills_dir=skills_dir, mode="copy")

        assert result.projected.get("alpha") == "demo"
        assert not (skills_dir / "alpha").is_symlink()
        assert (skills_dir / "alpha" / "SKILL.md").is_file()

    def test_a_copy_failure_is_reported_not_raised(self, store, skills_dir, tmp_path, monkeypatch):
        self._publish(store, tmp_path)
        monkeypatch.setattr(
            projection_mod.shutil,
            "copytree",
            lambda *a, **k: (_ for _ in ()).throw(OSError("no space left on device")),
        )
        result = rebuild_projection(store, skills_dir=skills_dir, mode="copy")

        assert "alpha" not in result.projected
        assert any(
            "alpha" in (f.path or "") or "alpha" in (f.message or "") for f in result.findings
        )

    def test_an_extra_dir_that_does_not_exist_is_skipped(self, store, skills_dir, monkeypatch):
        """A stale extra_dirs entry must not break a rebuild."""
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
            lambda: ["/definitely/not/here"],
        )
        result = rebuild_projection(store, skills_dir=skills_dir)
        assert result.findings == () or isinstance(result.findings, tuple)


class TestReleaseSkipsRecordsThatDoNotClaimTheName:
    def test_only_the_claiming_record_is_rewritten(self, store):
        store.write_record(make_record("first", projected_skill_names=("alpha",)))
        store.write_record(make_record("second", projected_skill_names=("beta",)))

        assert release_projection_claim("beta", store=store) == "second"

        assert store.get("first").projected_skill_names == ("alpha",), "an unrelated record changed"
        assert store.get("second").projected_skill_names == ()


class TestAffectedSessionsToleratesRealWorldState:
    def test_a_terminal_without_a_profile_is_skipped(self, store, monkeypatch):
        monkeypatch.setattr(installer_mod, "list_sessions", lambda: [{"name": "s1"}], raising=False)
        monkeypatch.setattr(
            installer_mod,
            "list_terminals_by_session",
            lambda _s: [{"id": "t1"}, {"id": "t2", "agent_profile": ""}],
            raising=False,
        )
        store.write_record(make_record("demo", projected_skill_names=("alpha",)))

        assert affected_sessions("demo", store=store) == []

    def test_a_terminal_whose_profile_was_deleted_is_skipped(self, store, monkeypatch):
        """The profile is gone, so its skill filter is unresolvable — not fatal."""
        monkeypatch.setattr(installer_mod, "list_sessions", lambda: [{"name": "s1"}], raising=False)
        monkeypatch.setattr(
            installer_mod,
            "list_terminals_by_session",
            lambda _s: [{"id": "t1", "agent_profile": "vanished"}],
            raising=False,
        )
        monkeypatch.setattr(
            installer_mod,
            "load_agent_profile",
            lambda _n: (_ for _ in ()).throw(FileNotFoundError("vanished")),
            raising=False,
        )
        store.write_record(make_record("demo", projected_skill_names=("alpha",)))

        assert affected_sessions("demo", store=store) == []


class TestSkillsAddForceOverAProjection:
    def test_it_reports_the_ownership_transfer_and_replaces_a_symlink(
        self, store, tmp_path, monkeypatch
    ):
        """Covers both the transfer message and the symlink removal branch."""
        from cli_agent_orchestrator.cli.commands import skills as skills_mod

        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        monkeypatch.setattr(skills_mod, "SKILLS_DIR", skills_dir)
        monkeypatch.setattr(projection_mod, "SKILLS_DIR", skills_dir)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))
        rebuild_projection(store, skills_dir=skills_dir, mode="symlink")
        assert (skills_dir / "alpha").is_symlink()

        monkeypatch.setattr(
            skills_mod, "release_projection_claim", lambda name: "demo", raising=False
        )
        monkeypatch.setattr(skills_mod, "refresh_all_cao_managed_agents", lambda: [], raising=False)

        user_skill = tmp_path / "userskills" / "alpha"
        user_skill.mkdir(parents=True)
        (user_skill / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: The user's own alpha skill.\n---\n\nmine\n", "utf-8"
        )

        result = CliRunner().invoke(skills_mod.skills, ["add", str(user_skill), "--force"])

        assert result.exit_code == 0, result.output
        assert "agent plugin 'demo'" in result.output
        assert not (skills_dir / "alpha").is_symlink(), "the symlink was not replaced"
        assert "mine" in (skills_dir / "alpha" / "SKILL.md").read_text(encoding="utf-8")
