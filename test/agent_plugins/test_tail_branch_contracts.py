"""The remaining reachable branches — skill-level isolation and refresh bookkeeping.

The validation cases here are the §7.1 per-skill failure boundary at its finest
grain: a sibling whose ``SKILL.md`` escapes the plugin root, and one where it is
missing entirely, must each be skipped **with a finding** while their well-formed
siblings still load.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins.installer import PluginInstallError
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

from .conftest import build_plugin
from .test_store import make_record


class TestPerSkillFailureIsolationAtTheFileLevel:
    def test_a_skill_md_symlinked_outside_the_root_is_skipped_with_a_finding(self, tmp_path):
        """§4.1 + §7.1: the escape is reported and the sibling survives."""
        plugin = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        outside = tmp_path / "outside.md"
        outside.write_text("---\nname: beta\ndescription: d\n---\n", encoding="utf-8")

        beta_md = plugin / "skills" / "beta" / "SKILL.md"
        beta_md.unlink()
        beta_md.symlink_to(outside)

        report = validate_plugin(plugin)

        assert "alpha" in report.skill_names, "a well-formed sibling must still load"
        assert "beta" not in report.skill_names
        assert any(f.code == "skill.escapes_root" for f in report.findings)

    def test_a_skill_directory_without_a_skill_md_is_skipped_with_a_finding(self, tmp_path):
        plugin = build_plugin(tmp_path / "p2", "demo", skills=["alpha"])
        (plugin / "skills" / "empty").mkdir()

        report = validate_plugin(plugin)

        assert "alpha" in report.skill_names
        assert "empty" not in report.skill_names
        assert report.findings, "a directory with no SKILL.md must be reported"

    def test_a_stray_file_under_skills_is_not_a_skill_candidate(self, tmp_path):
        plugin = build_plugin(tmp_path / "p3", "demo", skills=["alpha"])
        (plugin / "skills" / "README.md").write_text("not a skill", encoding="utf-8")

        report = validate_plugin(plugin)
        assert report.skill_names == ("alpha",)


class TestCommitPinnedCloneClearsStaging:
    def test_a_pinned_fetch_starts_from_an_empty_staging_tree(self, tmp_path, monkeypatch):
        """`init` + `fetch` cannot run over a partial tree from a failed attempt.

        Exercised through `_clone_at_commit` directly: the public `resolve()` only
        reaches it for a full 40-hex pin, and the leftover-tree condition is a
        recovery path rather than something a normal call produces.
        """
        from cli_agent_orchestrator.agent_plugins import resolver as rmod

        staged = tmp_path / "stage"
        staged.mkdir()
        (staged / "leftover.txt").write_text("from a failed attempt", encoding="utf-8")

        calls = []
        monkeypatch.setattr(rmod, "_run_git", lambda args, **k: calls.append(args[0]) or "")

        rmod._clone_at_commit("https://example.test/x.git", "a" * 40, staged)

        assert not (staged / "leftover.txt").exists(), "a stale staging tree survived"
        assert staged.is_dir(), "staging must be recreated, not merely deleted"
        assert calls[:1] == ["init"]

    @pytest.mark.parametrize("kind", ["file", "symlink"])
    def test_git_metadata_is_removed_without_following_it(self, tmp_path, kind):
        """A `.git` file (worktree pointer) or symlink must be unlinked, not traversed."""
        from cli_agent_orchestrator.agent_plugins import resolver as rmod

        staged = tmp_path / "staged"
        staged.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        canary = outside / "canary.txt"
        canary.write_text("must survive", encoding="utf-8")

        git_path = staged / ".git"
        if kind == "file":
            git_path.write_text("gitdir: /elsewhere/.git\n", encoding="utf-8")
        else:
            git_path.symlink_to(outside, target_is_directory=True)

        rmod._strip_vcs_metadata(staged)

        assert not git_path.exists() and not git_path.is_symlink()
        assert canary.is_file(), "the symlink target was followed and deleted"

    def test_a_real_git_directory_is_removed(self, tmp_path):
        from cli_agent_orchestrator.agent_plugins import resolver as rmod

        staged = tmp_path / "staged2"
        (staged / ".git").mkdir(parents=True)
        (staged / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        rmod._strip_vcs_metadata(staged)

        assert not (staged / ".git").exists()

    def test_absent_git_metadata_is_a_no_op(self, tmp_path):
        from cli_agent_orchestrator.agent_plugins import resolver as rmod

        staged = tmp_path / "staged3"
        staged.mkdir()
        rmod._strip_vcs_metadata(staged)  # must not raise
        assert staged.is_dir()

    def test_a_git_file_is_removed_without_following_it(self, tmp_path, monkeypatch):
        """A `.git` *file* (worktree pointer) must be unlinked, not traversed."""
        from cli_agent_orchestrator.agent_plugins import resolver as rmod

        def fake_run(args, *, what, cwd=None, **k):
            if cwd is not None and args[:1] in (["checkout"], ["fetch"]):
                build_plugin(Path(cwd), "demo", skills=["alpha"])
                (Path(cwd) / ".git").write_text("gitdir: /elsewhere/.git\n", encoding="utf-8")
            return ""

        monkeypatch.setattr(rmod, "_run_git", fake_run)
        resolved = rmod.resolve(
            rmod.PluginSource(kind="git", location="https://example.test/y.git", ref="b" * 40),
            dest=tmp_path / "dest2",
        )

        assert not (resolved.root / ".git").exists()


class TestOpencodeCollisionSnapshotShape:
    def test_a_non_object_mcp_section_is_treated_as_empty(self, monkeypatch, tmp_path):
        """A hand-edited opencode.json must not break collision detection."""
        from cli_agent_orchestrator.services import install_service as isvc

        monkeypatch.setattr(isvc, "read_config", lambda: {"mcp": "not-an-object"})
        written = {}
        monkeypatch.setattr(
            isvc, "upsert_mcp_server", lambda name, cfg: written.setdefault(name, cfg)
        )

        from cli_agent_orchestrator.agent_plugins.mcp_delivery import McpDeliveryResult

        isvc._materialize_opencode_mcp(
            "worker",
            {"plugin-srv": {"type": "stdio", "command": "demo"}},
            McpDeliveryResult(),
            agent_name="worker",
        )

        # The bad shape was treated as "no pre-existing entries", so the server was
        # written rather than refused as a user collision.
        assert "plugin-srv" in written


class TestRefreshBookkeeping:
    def test_no_agent_context_dir_means_nothing_to_refresh(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.services import install_service as isvc

        monkeypatch.setattr(isvc, "AGENT_CONTEXT_DIR", tmp_path / "absent")
        assert isvc.refresh_installed_agents_for_plugin_mcp() == []

    def test_a_failed_provider_refresh_is_logged_and_omitted(self, tmp_path, monkeypatch, caplog):
        from cli_agent_orchestrator.services import install_service as isvc

        ctx = tmp_path / "agent-context"
        ctx.mkdir()
        (ctx / "worker.md").write_text("# worker", encoding="utf-8")
        monkeypatch.setattr(isvc, "AGENT_CONTEXT_DIR", ctx)

        # Only installed providers are refreshed, so one artifact must exist.
        kiro = tmp_path / "kiro-agents"
        kiro.mkdir()
        (kiro / "worker.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(isvc, "KIRO_AGENTS_DIR", kiro)
        monkeypatch.setattr(isvc, "COPILOT_AGENTS_DIR", tmp_path / "absent-copilot")
        monkeypatch.setattr(isvc, "OPENCODE_AGENTS_DIR", tmp_path / "absent-opencode")

        class Failed:
            success = False
            message = "provider config locked"

        monkeypatch.setattr(isvc, "install_agent", lambda *a, **k: Failed())

        with caplog.at_level(logging.WARNING):
            refreshed = isvc.refresh_installed_agents_for_plugin_mcp()

        assert refreshed == []
        assert any("Could not refresh agent" in r.getMessage() for r in caplog.records)


class TestPluginRemoveGenericErrorMapping:
    def test_an_unexpected_error_is_still_a_click_exception(self, monkeypatch, store, tmp_path):
        """No traceback may escape `cao plugin remove`, whatever the cause."""
        from cli_agent_orchestrator.cli.commands import agent_plugin as mod

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        store.publish(source, make_record("demo", skill_names=("alpha",)))

        monkeypatch.setattr(mod, "InstalledPluginStore", lambda *a, **k: store)
        monkeypatch.setattr(mod, "affected_sessions", lambda *a, **k: [])
        monkeypatch.setattr(
            mod, "uninstall", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk exploded"))
        )

        result = CliRunner().invoke(mod.agent_plugin, ["remove", "demo", "--yes"])

        assert result.exit_code != 0
        assert "disk exploded" in result.output
        assert not isinstance(result.exception, RuntimeError)


class TestMarkdownLinkScanSkipsGeneratedPackageSkills:
    def test_a_generated_package_skill_copy_is_not_scanned_twice(self, tmp_path, monkeypatch):
        """The packages mirror `skills/`, so scanning both double-reports links."""
        from cli_agent_orchestrator.utils import markdown_links as ml

        repo = tmp_path / "repo"
        generated = repo / ml._GENERATED_PACKAGE_ROOT / "cao" / ml._GENERATED_PACKAGE_SKILLS / "s"
        generated.mkdir(parents=True)
        (generated / "SKILL.md").write_text("[x](./nope.md)\n", encoding="utf-8")
        plain = repo / "docs"
        plain.mkdir(parents=True)
        (plain / "guide.md").write_text("# guide\n", encoding="utf-8")

        import subprocess

        subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)

        files = ml.discover_markdown_files(repo)

        names = {str(p.relative_to(repo)) for p in files}
        assert "docs/guide.md" in names
        assert not any(
            str(ml._GENERATED_PACKAGE_ROOT) in n for n in names
        ), "a generated package copy was scanned"
