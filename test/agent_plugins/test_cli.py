"""``cao plugin`` CLI tests — parity with ``cao skills``, ``--json``, removal confirmation.

The CLI commands construct their own store from the module-level constants, so
each test patches those two symbols to tmp-path-backed directories. That is the
seam the production code genuinely uses; patching it here keeps the tests
exercising the real command bodies rather than a re-implementation of them.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.cli.commands.agent_plugin import (
    UNTRUSTED_CONTENT_WARNING,
    _looks_like_git,
    agent_plugin,
)
from cli_agent_orchestrator.cli.main import cli

from .conftest import CANONICAL_EXAMPLE_DIR, build_plugin, write_skill


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """Point the CLI's store and projection target at an isolated tree."""
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    for module in (
        "cli_agent_orchestrator.agent_plugins.store",
        "cli_agent_orchestrator.agent_plugins.projection",
    ):
        monkeypatch.setattr(f"{module}.AGENT_PLUGINS_DIR", plugins_dir, raising=False)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", data_dir
    )
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )

    return {
        "runner": CliRunner(),
        "plugins_dir": plugins_dir,
        "skills_dir": skills_dir,
        "store": InstalledPluginStore(plugins_dir, data_dir),
        "tmp_path": tmp_path,
    }


def run(cli_env, *args):
    return cli_env["runner"].invoke(agent_plugin, list(args))


class TestCommandShape:
    def test_the_group_is_registered_on_the_root_cli(self):
        """Registered and invocable — the M1 gate hides it, it does not unwire it."""
        assert "plugin" in cli.commands
        assert cli.commands["plugin"] is agent_plugin

        result = CliRunner().invoke(cli, ["plugin", "--help"])
        assert result.exit_code == 0
        assert "add" in result.output

    def test_it_is_absent_from_the_root_help_pending_m1(self):
        """Requirement 16.5 — the surface must not ship to end users before M1.

        Click's only "reachable but not advertised" mechanism is omission from the
        parent's help listing, so that omission *is* the gate. Asserting it here
        means flipping ``hidden`` back on cannot pass unnoticed. The assertion is
        deliberately narrow: it checks the command *listing*, because the word
        "plugin" legitimately appears elsewhere in ``cao --help`` output.
        """
        result = CliRunner().invoke(cli, ["--help"])
        listed = [
            line.split()[0]
            for line in result.output.splitlines()
            if line.startswith("  ") and line.strip() and not line.strip().startswith("-")
        ]
        assert "plugin" not in listed, f"`plugin` must stay hidden until M1: {listed}"

    def test_it_offers_the_four_capabilities(self):
        result = CliRunner().invoke(agent_plugin, ["--help"])
        for command in ("add", "list", "remove", "validate"):
            assert command in result.output

    def test_the_help_distinguishes_it_from_event_plugins(self):
        """M3's vocabulary rule, enforced where an operator first meets the verb."""
        result = CliRunner().invoke(agent_plugin, ["--help"])
        assert "event-plugin" in result.output or "event plugin" in result.output


class TestAdd:
    def test_add_installs_and_reports(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        result = run(cli_env, "add", str(source))

        assert result.exit_code == 0, result.output
        assert "installed" in result.output
        assert (cli_env["skills_dir"] / "alpha" / "SKILL.md").is_file()

    def test_add_warns_that_the_plugin_is_untrusted(self, cli_env, tmp_path):
        """Requirement 22.1 — stated at the point of install, not only in docs."""
        source = build_plugin(tmp_path / "src", "demo")
        result = run(cli_env, "add", str(source))

        assert "untrusted" in result.output.lower()
        assert UNTRUSTED_CONTENT_WARNING.split(".")[0] in result.output

    def test_add_json_is_machine_readable(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        result = run(cli_env, "add", str(source), "--json")

        payload = json.loads(result.output)
        assert payload["installed"] is True
        assert payload["record"]["projected_skill_names"] == ["alpha"]

    def test_add_of_an_unloadable_plugin_exits_non_zero(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", manifest_text="{ broken")
        result = run(cli_env, "add", str(source))

        assert result.exit_code != 0
        assert "manifest.invalid_json" in result.output

    def test_duplicate_add_is_refused_with_the_force_hint(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        run(cli_env, "add", str(source))

        result = run(cli_env, "add", str(source))
        assert result.exit_code != 0
        assert "--force" in result.output

    def test_force_replaces(self, cli_env, tmp_path):
        first = build_plugin(tmp_path / "v1" / "demo", "demo", skills=["alpha"])
        run(cli_env, "add", str(first))
        second = build_plugin(tmp_path / "v2" / "demo", "demo", skills=["beta"])

        result = run(cli_env, "add", str(second), "--force")
        assert result.exit_code == 0
        assert (cli_env["skills_dir"] / "beta").exists()

    def test_dry_run_installs_nothing(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        result = run(cli_env, "add", str(source), "--dry-run")

        assert result.exit_code == 0
        assert "loadable" in result.output
        assert not (cli_env["skills_dir"] / "alpha").exists()

    def test_subdir_addresses_a_monorepo_package(self, cli_env, tmp_path):
        build_plugin(tmp_path / "mono" / "agent-plugin" / "demo", "demo", skills=["alpha"])
        result = run(cli_env, "add", str(tmp_path / "mono"), "--subdir", "agent-plugin/demo")

        assert result.exit_code == 0, result.output
        assert (cli_env["skills_dir"] / "alpha").exists()

    def test_an_unreachable_source_reports_the_cause(self, cli_env, tmp_path):
        result = run(cli_env, "add", str(tmp_path / "nope"))
        assert result.exit_code != 0
        assert "does not exist" in result.output


class TestSourceKindDetection:
    @pytest.mark.parametrize(
        "location",
        [
            "https://github.com/agentplugins/agent-plugins-example",
            "https://github.com/o/r.git",
            "git@github.com:owner/repo.git",
            "ssh://git@example.com/x",
            "git+https://example.com/x",
        ],
    )
    def test_git_shaped_sources(self, location):
        assert _looks_like_git(location)

    @pytest.mark.parametrize("location", ["./local", "/abs/path", "relative/dir", "."])
    def test_path_shaped_sources(self, location):
        assert not _looks_like_git(location)


class TestList:
    def test_empty_store_says_so(self, cli_env):
        result = run(cli_env, "list")
        assert result.exit_code == 0
        assert "No agent plugins installed" in result.output

    def test_list_shows_name_version_and_skills(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"], version="2.1.0")
        run(cli_env, "add", str(source))

        result = run(cli_env, "list")
        assert "demo" in result.output
        assert "2.1.0" in result.output
        assert "alpha" in result.output

    def test_list_json_is_machine_readable(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        run(cli_env, "add", str(source))

        payload = json.loads(run(cli_env, "list", "--json").output)
        assert payload[0]["name"] == "demo"
        assert payload[0]["projected_skill_names"] == ["alpha"]

    def test_list_reports_a_skill_that_lost_a_collision(self, cli_env, tmp_path):
        write_skill(cli_env["skills_dir"] / "alpha", "alpha")
        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))

        result = run(cli_env, "list")
        assert "not projected" in result.output
        assert "alpha" in result.output

    def test_list_sweeps_dangling_projections(self, cli_env, tmp_path):
        import shutil

        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))
        shutil.rmtree(cli_env["store"].plugin_root("demo") / "skills" / "alpha")

        assert (cli_env["skills_dir"] / "alpha").is_symlink()
        assert run(cli_env, "list").exit_code == 0
        assert not (cli_env["skills_dir"] / "alpha").is_symlink()


class TestValidate:
    def test_validate_reports_without_installing(self, cli_env):
        result = run(cli_env, "validate", str(CANONICAL_EXAMPLE_DIR))

        assert result.exit_code == 0
        assert "loadable" in result.output
        assert list(cli_env["skills_dir"].iterdir()) == []

    def test_validate_json_is_machine_readable(self, cli_env):
        payload = json.loads(run(cli_env, "validate", str(CANONICAL_EXAMPLE_DIR), "--json").output)

        assert payload["loadable"] is True
        assert payload["skills"][0]["name"] == "migrate-agent-plugin"

    def test_validate_exits_non_zero_for_an_unloadable_plugin(self, cli_env, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", schema_id=None)
        result = run(cli_env, "validate", str(source))

        assert result.exit_code != 0
        assert "manifest.schema_missing" in result.output

    def test_validate_of_a_nonexistent_path_reports_rather_than_crashes(self, cli_env, tmp_path):
        result = run(cli_env, "validate", str(tmp_path / "absent"))
        assert result.exit_code != 0
        assert "plugin.root_invalid" in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)


class TestRemove:
    def test_remove_deletes_and_unprojects(self, cli_env, tmp_path):
        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))

        result = run(cli_env, "remove", "demo")

        assert result.exit_code == 0
        assert "removed" in result.output
        assert not (cli_env["skills_dir"] / "alpha").exists()

    def test_removing_an_absent_plugin_is_an_error(self, cli_env):
        result = run(cli_env, "remove", "ghost")
        assert result.exit_code != 0
        assert "not installed" in result.output

    def test_purge_data_is_reported(self, cli_env, tmp_path):
        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))
        result = run(cli_env, "remove", "demo", "--purge-data")
        assert "persistent data directory was deleted" in result.output


class TestRemovalConfirmation:
    """Requirement 15 — warn, require confirmation, never refuse."""

    @pytest.fixture
    def with_live_session(self, cli_env, tmp_path, monkeypatch):
        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_service.list_sessions",
            lambda: [{"id": "cao-live"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda name: [{"id": "abcd1234", "agent_profile": "worker"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            lambda name: type("P", (), {"skills": ["alpha"]})(),
        )
        return cli_env

    def test_it_reports_the_affected_sessions_and_skills(self, with_live_session):
        result = with_live_session["runner"].invoke(agent_plugin, ["remove", "demo"], input="n\n")

        assert "cao-live" in result.output
        assert "abcd1234" in result.output
        assert "alpha" in result.output

    def test_declining_aborts_and_leaves_the_plugin_installed(self, with_live_session):
        result = with_live_session["runner"].invoke(agent_plugin, ["remove", "demo"], input="n\n")

        assert result.exit_code != 0
        assert with_live_session["store"].get("demo") is not None
        assert (with_live_session["skills_dir"] / "alpha").exists()

    def test_confirming_proceeds(self, with_live_session):
        """It warns; it does not refuse."""
        result = with_live_session["runner"].invoke(agent_plugin, ["remove", "demo"], input="y\n")

        assert result.exit_code == 0
        assert with_live_session["store"].get("demo") is None

    def test_yes_bypasses_the_prompt(self, with_live_session):
        result = with_live_session["runner"].invoke(agent_plugin, ["remove", "demo", "--yes"])

        assert result.exit_code == 0
        assert "Remove it anyway?" not in result.output
        assert with_live_session["store"].get("demo") is None

    def test_no_prompt_when_nothing_is_affected(self, cli_env, tmp_path):
        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))

        result = run(cli_env, "remove", "demo")
        assert result.exit_code == 0
        assert "Remove it anyway?" not in result.output


class TestParityWithSkillsCommand:
    def test_both_groups_use_click_exception_for_errors(self, cli_env, tmp_path):
        """``cao skills`` wraps every failure as a ClickException; so does this."""
        result = run(cli_env, "add", str(tmp_path / "missing"))
        assert result.exit_code == 1
        assert result.output.startswith("Warning:") or "Error:" in result.output

    def test_list_uses_the_two_column_style_of_cao_skills_list(self, cli_env, tmp_path):
        run(cli_env, "add", str(build_plugin(tmp_path / "src", "demo", skills=["alpha"])))
        result = run(cli_env, "list")

        assert "Name" in result.output
        assert "-" * 20 in result.output
