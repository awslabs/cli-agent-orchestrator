"""Destination-based ownership guard for installs (PR #493, round-3 review).

The guard used to derive occupancy from ``list_agent_profiles()`` and ran only
for OpenCode. Round 3 (haofeif) showed six ways that let the silent overwrite
through; every case below failed on ``db57df34`` and passes with the guard
reading the destination path itself and running for every provider.

The ``workspace`` fixture (``conftest.py``) points ``AGENT_CONTEXT_DIR`` and the
default ``cao_installed`` mapping at the same temp context dir, exactly as in
production; the install helpers come from ``install_helpers.py``.
"""

import logging
import os
from pathlib import Path
from test.cli.commands.install_helpers import _install, _install_for, _ok, _refused, _write_profile
from typing import Any, Dict

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.services import install_service, settings_service
from cli_agent_orchestrator.services.install_service import (
    _CONTEXT_SOURCE_STEM_KEY,
    _write_context_file,
)
from cli_agent_orchestrator.utils import skill_injection

# ---------------------------------------------------------------------------
# Finding 1: occupancy comes from the destination, not from lossy discovery.
# ---------------------------------------------------------------------------


class TestOccupancyIsReadFromTheDestination:
    def test_profile_installed_under_its_own_name_still_owns_its_id(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """The ordinary case: ``shared.md`` with ``name: shared``.

        Discovery keeps the first profile per stem, so the local-store file won
        and the installed copy was relegated to ``duplicated_in``; a guard that
        only counted ``source == "installed"`` candidates never saw the owner and
        ``other.md`` (``name: shared``) replaced it. On ``db57df34`` the second
        install printed "installed successfully".
        """
        store = workspace["local_store"]
        agent_file = workspace["agents_dir"] / "shared.md"
        _write_profile(store / "shared.md", name="shared", body="FIRST")
        _ok(_install(runner, "shared"))
        assert "FIRST" in agent_file.read_text()

        _write_profile(store / "other.md", name="shared", body="SECOND")
        r2 = _install(runner, "other")

        _refused(r2)
        assert "shared" in r2.output and "other" in r2.output
        assert "FIRST" in agent_file.read_text()
        assert "SECOND" not in (workspace["context_dir"] / "shared.md").read_text()

    def test_tilde_spelling_of_the_context_directory_does_not_hide_the_owner(
        self,
        runner: CliRunner,
        workspace: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``cao_installed: ~/agent-context`` names the same directory the copies
        are in. Discovery scanned the unexpanded string and found nothing there;
        the guard now expands the setting and probes the directory itself."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))

        # workspace["context_dir"] is tmp_path / "agent-context"
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"cao_installed": "~/agent-context"},
        )
        _write_profile(store / "beta.md", name="shared", body="BETA")
        r2 = _install(runner, "beta")

        _refused(r2)
        assert "ALPHA" in (workspace["agents_dir"] / "shared.md").read_text()

    def test_guard_does_not_depend_on_profile_discovery(
        self, runner: CliRunner, workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A discovery failure used to skip the guard silently."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))

        def _broken() -> None:
            raise RuntimeError("discovery is down")

        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles", _broken
        )
        _write_profile(store / "beta.md", name="shared", body="BETA")
        r2 = _install(runner, "beta")

        _refused(r2)
        assert "ALPHA" in (workspace["agents_dir"] / "shared.md").read_text()

    def test_disabled_context_directory_does_not_hide_the_owner(
        self, runner: CliRunner, workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Disabling ``cao_installed`` in Settings hides its profiles from listing
        and loading; it must not hide who owns an id from the guard."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_disabled_agent_dirs",
            lambda: [str(workspace["context_dir"])],
        )
        _write_profile(store / "beta.md", name="shared", body="BETA")
        r2 = _install(runner, "beta")

        _refused(r2)
        assert "ALPHA" in (workspace["agents_dir"] / "shared.md").read_text()


# ---------------------------------------------------------------------------
# Finding 2: the guard runs for every provider, because every provider
# overwrites the shared context copy and re-stamps its provenance.
# ---------------------------------------------------------------------------


class TestGuardRunsForEveryProvider:
    def test_kiro_install_cannot_take_over_an_id_another_profile_owns(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """On ``db57df34`` the kiro install of ``beta`` rewrote ``shared.md`` with
        ``x-cao-source-stem: beta``, after which an OpenCode install of ``beta``
        read the copy as its own and replaced ``alpha``'s agent file."""
        store = workspace["local_store"]
        context_copy = workspace["context_dir"] / "shared.md"
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))
        alpha_copy = context_copy.read_text()
        assert f"{_CONTEXT_SOURCE_STEM_KEY}: 'alpha'" in alpha_copy

        _write_profile(store / "beta.md", name="shared", body="BETA")
        rk = _install_for(runner, "beta", "kiro_cli")

        _refused(rk)
        assert "alpha" in rk.output and "beta" in rk.output
        assert "kiro_cli" in rk.output
        assert context_copy.read_text() == alpha_copy
        # ...and the OpenCode install that used to follow cannot take the id either.
        r2 = _install(runner, "beta")
        _refused(r2)
        assert "ALPHA" in (workspace["agents_dir"] / "shared.md").read_text()

    def test_same_profile_installs_for_a_second_provider(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """Ownership is by install stem, so the SAME profile installs everywhere."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared")
        _ok(_install(runner, "alpha"))
        _ok(_install_for(runner, "alpha", "kiro_cli"))
        _ok(_install(runner, "alpha"))


# ---------------------------------------------------------------------------
# Finding 3: the installed copy's own ``name:`` is never parsed, so a
# placeholder there cannot make the comparison miss.
# ---------------------------------------------------------------------------


class TestInstalledIdIsTheFilenameNotTheParsedName:
    @pytest.fixture()
    def alias_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            install_service,
            "resolve_env_vars",
            lambda raw: raw.replace("${ALIAS}", "shared"),
        )

    def test_placeholder_named_profile_owns_its_resolved_id(
        self, runner: CliRunner, workspace: Dict[str, Any], alias_env: None
    ) -> None:
        """``aliased.md`` declares ``name: ${ALIAS}`` and installs as ``shared``.

        Its context copy keeps the raw placeholder, so a guard that parsed the
        copy's ``name:`` compared ``${ALIAS}`` with ``shared`` and let
        ``other.md`` (``name: shared``) replace it.
        """
        store = workspace["local_store"]
        _write_profile(store / "aliased.md", name="${ALIAS}", body="ALIASED")
        _ok(_install(runner, "aliased"))
        agent_file = workspace["agents_dir"] / "shared.md"
        assert "ALIASED" in agent_file.read_text()
        assert "${ALIAS}" in (workspace["context_dir"] / "shared.md").read_text()

        _write_profile(store / "other.md", name="shared", body="OTHER")
        r2 = _install(runner, "other")

        _refused(r2)
        assert "aliased" in r2.output
        assert "ALIASED" in agent_file.read_text()

    def test_placeholder_named_profile_reinstalls_as_itself(
        self, runner: CliRunner, workspace: Dict[str, Any], alias_env: None
    ) -> None:
        store = workspace["local_store"]
        _write_profile(store / "aliased.md", name="${ALIAS}", body="ONE")
        _ok(_install(runner, "aliased"))
        _write_profile(store / "aliased.md", name="${ALIAS}", body="TWO")
        _ok(_install(runner, "aliased"))
        assert "TWO" in (workspace["agents_dir"] / "shared.md").read_text()


# ---------------------------------------------------------------------------
# Finding 4: a blank or relative ``cao_installed`` is not a directory.
# ---------------------------------------------------------------------------


class TestOverrideMustBeAnAbsoluteDirectory:
    @pytest.mark.parametrize("configured", ["", "   ", "relative/dir", "./here"])
    def test_blank_or_relative_setting_falls_back_to_the_default_with_a_warning(
        self, configured: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``Path("")`` is ``Path(".")``: the server's working directory would have
        become the trusted write root, and a profile named ``README`` or
        ``AGENTS`` would have landed on a repository file."""
        monkeypatch.setattr(
            settings_service, "get_agent_dirs", lambda: {"cao_installed": configured}
        )
        with caplog.at_level(logging.WARNING, logger=settings_service.__name__):
            assert settings_service.installed_context_dir_override() is None
        assert any("cao_installed" in record.getMessage() for record in caplog.records)

    def test_tilde_spelling_is_expanded_not_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {"cao_installed": "~/ctx"})
        assert settings_service.installed_context_dir_override() == tmp_path / "ctx"

    def test_blank_setting_does_not_make_the_working_directory_a_profile_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same raw value reached discovery and the lookup behind
        ``cao install <name>``: with ``cao_installed: ""`` a ``README.md`` in the
        server's working directory was listed as an installed profile and served
        as the content of ``cao install README``."""
        from cli_agent_orchestrator.utils import agent_profiles

        workdir = tmp_path / "repo"
        workdir.mkdir()
        (workdir / "README.md").write_text("# not a profile\n", encoding="utf-8")
        monkeypatch.chdir(workdir)
        monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", tmp_path / "no-store")
        monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {"cao_installed": ""})
        monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])

        listed = {p["name"]: p["source"] for p in agent_profiles.list_agent_profiles()}
        assert listed.get("README") != "installed"
        with pytest.raises(FileNotFoundError):
            agent_profiles._read_agent_profile_source("README")

    def test_tilde_spelled_directory_is_scanned_by_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Discovery used to scan the literal ``~/...`` string, which exists nowhere,
        so a profile installed under a ``~``-spelled directory was never listed."""
        from cli_agent_orchestrator.utils import agent_profiles

        home = tmp_path / "home"
        (home / "ctx").mkdir(parents=True)
        _write_profile(home / "ctx" / "shared.md", name="shared")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", tmp_path / "no-store")
        monkeypatch.setattr(settings_service, "get_agent_dirs", lambda: {"cao_installed": "~/ctx"})
        monkeypatch.setattr(settings_service, "get_extra_agent_dirs", lambda: [])
        monkeypatch.setattr(settings_service, "get_disabled_agent_dirs", lambda: [])

        listed = {p["name"]: p["source"] for p in agent_profiles.list_agent_profiles()}
        assert listed.get("shared") == "installed"

    def test_writer_refuses_a_relative_context_directory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sink's own refusal, independent of how the directory was resolved."""
        monkeypatch.setattr(install_service, "_context_dir", lambda: Path("relative-ctx"))
        with pytest.raises(ValueError, match="not an absolute path"):
            _write_context_file("README", "---\nname: README\n---\nbody\n", "README")
        assert not Path("relative-ctx").exists()


# ---------------------------------------------------------------------------
# Finding 5: ownership recorded at the default directory stays in force
# after an operator configures an override.
# ---------------------------------------------------------------------------


class TestLegacyDefaultDirectoryStaysInForce:
    def test_owner_recorded_at_the_default_blocks_under_an_override(
        self,
        runner: CliRunner,
        workspace: Dict[str, Any],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Earlier releases wrote every copy to ``AGENT_CONTEXT_DIR`` even with an
        override configured. Switching every consumer to the override without
        looking back orphaned those records: ``beta`` replaced ``alpha``'s agent
        file, and Copilot skill refresh stopped recognising the agent."""
        store = workspace["local_store"]
        legacy_dir = workspace["context_dir"]  # == AGENT_CONTEXT_DIR in this fixture
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))
        legacy_copy = legacy_dir / "shared.md"
        legacy_bytes = legacy_copy.read_bytes()

        override_dir = tmp_path / "configured-elsewhere"
        override_dir.mkdir()
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"cao_installed": str(override_dir)},
        )

        _write_profile(store / "beta.md", name="shared", body="BETA")
        r2 = _install(runner, "beta")
        _refused(r2)
        assert str(legacy_copy) in r2.output
        assert "ALPHA" in (workspace["agents_dir"] / "shared.md").read_text()
        assert not (override_dir / "shared.md").exists()

        # The owner itself still reinstalls, and its new copy lands in the override.
        r3 = _install(runner, "alpha")
        _ok(r3)
        assert (override_dir / "shared.md").exists()
        assert legacy_copy.read_bytes() == legacy_bytes

    def test_copilot_probe_recognises_agents_recorded_at_the_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        constant_dir = tmp_path / "context"
        constant_dir.mkdir()
        (constant_dir / "developer.md").write_text("x", encoding="utf-8")
        override_dir = tmp_path / "configured-elsewhere"
        override_dir.mkdir()
        monkeypatch.setattr(skill_injection, "AGENT_CONTEXT_DIR", constant_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
            lambda: {"cao_installed": str(override_dir)},
        )

        assert skill_injection._is_cao_managed_copilot_agent("developer") is True
        assert skill_injection._is_cao_managed_copilot_agent("nobody") is False


# ---------------------------------------------------------------------------
# Finding 6: the destination check inherits the filesystem's case rules.
# ---------------------------------------------------------------------------


class TestCaseFoldingFilesystems:
    def test_differently_cased_name_is_refused_where_the_filesystem_folds_case(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """``agent`` and ``Agent`` are one file on default macOS storage.

        The guard probes ``<context dir>/Agent.md`` and finds ``agent.md``'s
        copy, stamped ``agent`` -- a different stem -- so the second install is
        refused before it can overwrite the first's agent file or leave two
        differently-cased ``agent.<id>`` keys in ``opencode.json``. On a
        case-sensitive filesystem the two really are distinct and both install.
        """
        store = workspace["local_store"]
        _write_profile(store / "agent.md", name="agent", body="LOWER")
        _ok(_install(runner, "agent"))
        folds_case = (workspace["context_dir"] / "AGENT.MD").exists()

        _write_profile(store / "Agent.md", name="Agent", body="UPPER")
        r2 = _install(runner, "Agent")

        installed = sorted(p.name for p in workspace["agents_dir"].iterdir())
        if folds_case:
            _refused(r2)
            assert installed == ["agent.md"]
            assert "LOWER" in (workspace["agents_dir"] / "agent.md").read_text()
        else:
            _ok(r2)
            assert installed == ["Agent.md", "agent.md"]


class TestUnreadableCopyIsAnIoFaultNotAnOrphan:
    def test_permission_denied_names_access_not_deletion(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """An EACCES on the occupying copy used to fall through to the collision
        error, whose remedy tells the operator to delete the file -- discarding the
        ownership record over an I/O fault."""
        if os.geteuid() == 0:
            pytest.skip("root ignores file modes")
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))
        copy = workspace["context_dir"] / "shared.md"
        copy.chmod(0)
        try:
            _write_profile(store / "beta.md", name="shared", body="BETA")
            r2 = _install(runner, "beta")
        finally:
            copy.chmod(0o600)

        _refused(r2)
        assert "could not be read" in r2.output, r2.output
        assert "Fix the file's permissions" in r2.output
        assert "delete it and reinstall" not in r2.output
        assert "ALPHA" in (workspace["agents_dir"] / "shared.md").read_text()


class TestSourceDeclaredMarkerIsReplacedNotRefused:
    def test_plain_declaration_is_restamped_with_the_real_stem(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """What the docs now say: a column-0 ``x-cao-source-stem`` in the source is
        replaced by CAO's own line; only a spelling that reads back differently is
        refused (see TestProvenanceMarkerSpoofRefused)."""
        store = workspace["local_store"]
        (store / "alpha.md").write_text(
            "---\nname: shared\ndescription: Test agent\n"
            f"{_CONTEXT_SOURCE_STEM_KEY}: 'somebody-else'\n---\nBody\n",
            encoding="utf-8",
        )
        _ok(_install(runner, "alpha"))
        copy = (workspace["context_dir"] / "shared.md").read_text()
        assert f"{_CONTEXT_SOURCE_STEM_KEY}: 'alpha'" in copy
        assert "somebody-else" not in copy


# ---------------------------------------------------------------------------
# Round 5 (haofeif P2): the context directory's case rules are not the provider
# directory's. ``_entry_occupying`` is the one seam through which the guard asks
# a directory which entry a write would replace, so a per-directory rule table
# stands in for the two filesystems and the scenario runs the same way on a
# case-folding macOS tmp and a case-sensitive Linux one.
# ---------------------------------------------------------------------------


def _rules_filesystem(rules: Dict[Path, str]):
    """``_entry_occupying`` under per-directory rules: ``sensitive`` or ``folding``.

    Directories not in ``rules`` keep the real filesystem's answer.
    """
    real = install_service._entry_occupying

    def fake(path: Path):
        rule = rules.get(path.parent)
        if rule is None:
            return real(path)
        try:
            names = os.listdir(path.parent)
        except FileNotFoundError:
            return None
        if rule == "sensitive":
            return path.name if path.name in names else None
        assert rule == "folding"
        return next((n for n in names if n.casefold() == path.name.casefold()), None)

    return fake


class TestMixedCaseRulesAcrossContextAndProviderDirectories:
    def _mixed(
        self, monkeypatch: pytest.MonkeyPatch, workspace: Dict[str, Any], provider_dir: Path
    ):
        monkeypatch.setattr(
            install_service,
            "_entry_occupying",
            _rules_filesystem({workspace["context_dir"]: "sensitive", provider_dir: "folding"}),
        )

    def test_opencode_alias_at_the_provider_destination_is_refused(
        self, runner: CliRunner, workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """haofeif's reproduction: ``agent`` then ``Agent`` with a case-sensitive
        context dir and a case-folding OpenCode dir. On ``6d3d522e`` both installs
        succeeded, two context records existed, and ``Agent.md`` had replaced
        alpha's body at the same physical provider file."""
        self._mixed(monkeypatch, workspace, workspace["agents_dir"])
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="agent", body="ALPHA-BODY")
        _ok(_install(runner, "alpha"))

        _write_profile(store / "beta.md", name="Agent", body="BETA-BODY")
        r2 = _install(runner, "beta")

        _refused(r2)
        assert "alpha" in r2.output and "beta" in r2.output
        assert "spelled 'agent.md' on disk" in r2.output, r2.output
        assert "Rename one of these profiles" in r2.output
        # One context record, one provider file, alpha's body untouched.
        assert sorted(p.name for p in workspace["context_dir"].iterdir()) == ["agent.md"]
        assert sorted(p.name for p in workspace["agents_dir"].iterdir()) == ["agent.md"]
        assert "ALPHA-BODY" in (workspace["agents_dir"] / "agent.md").read_text()
        # ...and nothing landed in opencode.json for the refused id (the file
        # may not exist at all: a profile without mcpServers writes none).
        config_file = workspace["config_file"]
        assert not config_file.exists() or '"Agent"' not in config_file.read_text()

    def test_kiro_alias_at_the_provider_destination_is_refused(
        self, runner: CliRunner, workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every provider sink is probed, not only OpenCode's."""
        self._mixed(monkeypatch, workspace, workspace["kiro_agents_dir"])
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="agent", body="ALPHA-BODY")
        _ok(_install_for(runner, "alpha", "kiro_cli"))
        before = (workspace["kiro_agents_dir"] / "agent.json").read_text()

        _write_profile(store / "beta.md", name="Agent", body="BETA-BODY")
        r2 = _install_for(runner, "beta", "kiro_cli")

        _refused(r2)
        assert "kiro_cli agent file" in r2.output, r2.output
        assert "spelled 'agent.json' on disk" in r2.output
        assert sorted(p.name for p in workspace["kiro_agents_dir"].iterdir()) == ["agent.json"]
        assert (workspace["kiro_agents_dir"] / "agent.json").read_text() == before
        assert sorted(p.name for p in workspace["context_dir"].iterdir()) == ["agent.md"]

    def test_the_same_profile_may_change_the_case_of_its_own_name(
        self, runner: CliRunner, workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The alias is owned by the installing stem: a rename of ``alpha``'s own
        ``name:`` from ``agent`` to ``Agent`` replaces alpha's own provider file."""
        self._mixed(monkeypatch, workspace, workspace["agents_dir"])
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="agent", body="OLD-CASE")
        _ok(_install(runner, "alpha"))

        _write_profile(store / "alpha.md", name="Agent", body="NEW-CASE")
        _ok(_install(runner, "alpha"))

    def test_distinct_case_names_both_install_where_neither_directory_folds(
        self, runner: CliRunner, workspace: Dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: with both directories case-sensitive the two really are distinct
        files, and the probe must not refuse what the filesystem keeps apart."""
        monkeypatch.setattr(
            install_service,
            "_entry_occupying",
            _rules_filesystem(
                {workspace["context_dir"]: "sensitive", workspace["agents_dir"]: "sensitive"}
            ),
        )
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="agent")
        _ok(_install(runner, "alpha"))
        _write_profile(store / "beta.md", name="Agent")
        r2 = _install(runner, "beta")
        folds_for_real = (workspace["agents_dir"] / "AGENT.MD").exists()
        if folds_for_real:
            # The real tmp dir folds case (macOS): the write would alias after
            # all, and the guard's answer is whatever ``_entry_occupying``
            # says; this control is about the probe's logic, not the host.
            return
        _ok(r2)
        assert sorted(p.name for p in workspace["agents_dir"].iterdir()) == ["Agent.md", "agent.md"]


class TestOrphanedProviderArtifact:
    def test_a_provider_file_with_no_context_record_is_not_overwritten(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """Ownership used to be keyed solely on the context copy, so a hand-deleted
        copy left the provider file free to be silently replaced (gutosantos82)."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="ALPHA-BODY")
        _ok(_install(runner, "alpha"))
        (workspace["context_dir"] / "shared.md").unlink()

        _write_profile(store / "beta.md", name="shared", body="BETA-BODY")
        r2 = _install(runner, "beta")

        _refused(r2)
        assert "no installed profile CAO knows of" in r2.output, r2.output
        assert "delete it and reinstall" in r2.output
        assert "ALPHA-BODY" in (workspace["agents_dir"] / "shared.md").read_text()

        # The remedy works: remove the orphan and the install goes through.
        (workspace["agents_dir"] / "shared.md").unlink()
        _ok(_install(runner, "beta"))
        assert "BETA-BODY" in (workspace["agents_dir"] / "shared.md").read_text()

    def test_self_reinstall_over_own_provider_file_still_works(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="V1")
        _ok(_install(runner, "alpha"))
        _write_profile(store / "alpha.md", name="shared", body="V2")
        _ok(_install(runner, "alpha"))
        assert "V2" in (workspace["agents_dir"] / "shared.md").read_text()


class TestRefusedInstallWritesNothing:
    def test_local_store_copy_is_byte_identical_after_a_refusal(
        self, runner: CliRunner, workspace: Dict[str, Any]
    ) -> None:
        """The guard runs before the local-store ``provider:`` rewrite, not only
        before the context write (gutosantos82, round 5). On ``6d3d522e`` a refused
        ``beta`` still came back re-serialised with ``provider: opencode_cli``."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="shared", body="ALPHA")
        _ok(_install(runner, "alpha"))

        _write_profile(store / "beta.md", name="shared", body="BETA")
        before = (store / "beta.md").read_bytes()
        assert b"provider:" not in before

        _refused(_install(runner, "beta"))
        assert (store / "beta.md").read_bytes() == before


class TestProviderArtifactPathMirrorsTheInstaller:
    @pytest.mark.parametrize("provider", ["opencode_cli", "kiro_cli", "copilot_cli"])
    def test_guard_probes_the_file_the_install_reports(
        self, runner: CliRunner, workspace: Dict[str, Any], provider: str
    ) -> None:
        """``_provider_artifact_path`` is a mirror of the installer's sinks; if a
        sink moves, this is the test that notices."""
        store = workspace["local_store"]
        _write_profile(store / "alpha.md", name="mirror_me", body="You are an agent.")
        result = install_service.install_agent("alpha", provider)
        assert result.success, result.message
        assert result.agent_file is not None
        assert install_service._provider_artifact_path(provider, "mirror_me") == Path(
            result.agent_file
        )

    def test_providers_without_a_per_agent_file_probe_nothing(self) -> None:
        assert install_service._provider_artifact_path("claude_code", "x") is None


class TestEntryOccupying:
    def test_missing_path_is_free(self, tmp_path: Path) -> None:
        assert install_service._entry_occupying(tmp_path / "nothing.md") is None

    def test_exact_file_is_named(self, tmp_path: Path) -> None:
        (tmp_path / "agent.md").write_text("x")
        assert install_service._entry_occupying(tmp_path / "agent.md") == "agent.md"

    def test_alias_is_named_by_its_on_disk_spelling_where_the_directory_folds(
        self, tmp_path: Path
    ) -> None:
        (tmp_path / "agent.md").write_text("x")
        answer = install_service._entry_occupying(tmp_path / "Agent.md")
        if (tmp_path / "AGENT.MD").exists():  # case-folding tmp (macOS default)
            assert answer == "agent.md"
        else:
            assert answer is None

    def test_symlink_entry_is_matched_as_itself(self, tmp_path: Path) -> None:
        (tmp_path / "real.md").write_text("x")
        os.symlink(tmp_path / "real.md", tmp_path / "link.md")
        assert install_service._entry_occupying(tmp_path / "link.md") == "link.md"
