"""Installer and resolver error paths — every one has a stated contract.

The install pipeline's value is that it fails *predictably*: a store error becomes
a ``PluginInstallError`` the CLI can print, a missing git binary becomes an
actionable message rather than a traceback, and a best-effort refresh that fails
degrades to a warning instead of failing an install that already succeeded. Those
are the branches below.
"""

from __future__ import annotations

import logging
import subprocess

import pytest

from cli_agent_orchestrator.agent_plugins import installer as installer_mod
from cli_agent_orchestrator.agent_plugins import resolver as resolver_mod
from cli_agent_orchestrator.agent_plugins.installer import (
    PluginInstallError,
    has_fatal,
    install,
    installed_findings,
    uninstall,
)
from cli_agent_orchestrator.agent_plugins.models import (
    Finding,
    PluginSource,
    Severity,
)
from cli_agent_orchestrator.agent_plugins.resolver import ResolverError, resolve
from cli_agent_orchestrator.agent_plugins.store import PluginStoreError

from .conftest import build_plugin
from .test_store import make_record


class TestStoreFailuresBecomeInstallErrors:
    def test_a_publish_failure_is_reported_as_an_install_error(
        self, store, skills_dir, tmp_path, monkeypatch
    ):
        """The CLI prints PluginInstallError; a raw store error would escape it."""
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        monkeypatch.setattr(
            type(store),
            "publish",
            lambda *a, **k: (_ for _ in ()).throw(PluginStoreError("disk on fire")),
        )

        with pytest.raises(PluginInstallError, match="disk on fire"):
            install(
                PluginSource(kind="path", location=str(source)),
                store=store,
                skills_dir=skills_dir,
                refresh_agents=False,
            )

    def test_an_invalid_name_on_remove_is_reported_as_an_install_error(self, store, skills_dir):
        """`unpublish` raises ValueError for an unsafe name; it must be wrapped."""
        with pytest.raises(PluginInstallError):
            uninstall("../escape", store=store, skills_dir=skills_dir)


class TestBestEffortRefreshesNeverFailAnInstall:
    def test_a_failing_prompt_refresh_only_warns(self, monkeypatch, caplog):
        import cli_agent_orchestrator.utils.skill_injection as si

        monkeypatch.setattr(
            si,
            "refresh_all_cao_managed_agents",
            lambda: (_ for _ in ()).throw(RuntimeError("profile store unreadable")),
        )
        with caplog.at_level(logging.WARNING):
            installer_mod._refresh_agent_artifacts()

        assert any(
            "Could not refresh installed agent prompts" in r.getMessage() for r in caplog.records
        )

    def test_a_failing_mcp_refresh_only_warns(self, monkeypatch, caplog):
        import cli_agent_orchestrator.services.install_service as isvc

        monkeypatch.setattr(
            isvc,
            "refresh_installed_agents_for_plugin_mcp",
            lambda: (_ for _ in ()).throw(RuntimeError("provider config locked")),
            raising=False,
        )
        with caplog.at_level(logging.WARNING):
            installer_mod._refresh_agent_artifacts()

        assert any("Could not refresh" in r.getMessage() for r in caplog.records)


class TestFindingHelpers:
    def test_installed_findings_returns_the_records_findings(self):
        finding = Finding(
            severity=Severity.WARNING,
            code="manifest.unknown_field",
            spec_ref="§5.2",
            message="ignored",
            path="plugin.json",
        )
        record = make_record("demo", findings=(finding,))
        assert installed_findings(record) == (finding,)

    def test_has_fatal_is_true_only_for_a_fatal_finding(self):
        def f(severity):
            return Finding(
                severity=severity, code="c", spec_ref="§1", message="m", path="plugin.json"
            )

        assert has_fatal((f(Severity.FATAL),)) is True
        assert has_fatal((f(Severity.WARNING), f(Severity.SKIPPED))) is False
        assert has_fatal(()) is False


class TestResolverErrorPaths:
    def test_an_unreadable_local_source_is_a_resolver_error(self, tmp_path, monkeypatch):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        monkeypatch.setattr(
            resolver_mod.shutil,
            "copytree",
            lambda *a, **k: (_ for _ in ()).throw(OSError("permission denied")),
        )
        with pytest.raises(ResolverError, match="Could not copy plugin source"):
            resolve(PluginSource(kind="path", location=str(source)), dest=tmp_path / "dest")

    def test_an_empty_git_url_is_rejected(self, tmp_path):
        with pytest.raises(ResolverError, match="git URL is empty"):
            resolve(PluginSource(kind="git", location="   "), dest=tmp_path / "dest")

    def test_a_missing_git_binary_says_so(self, tmp_path, monkeypatch):
        """The message must name the prerequisite, not surface a FileNotFoundError."""
        monkeypatch.setattr(
            resolver_mod.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")),
        )
        with pytest.raises(ResolverError, match="git executable not found"):
            resolve(
                PluginSource(kind="git", location="https://example.test/x.git"),
                dest=tmp_path / "dest",
            )

    def test_a_git_timeout_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            resolver_mod.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="git clone", timeout=1)
            ),
        )
        with pytest.raises(ResolverError, match="timed out"):
            resolve(
                PluginSource(kind="git", location="https://example.test/x.git"),
                dest=tmp_path / "dest",
            )

    def test_a_git_failure_reports_the_command_that_failed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            resolver_mod.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(
                subprocess.CalledProcessError(returncode=128, cmd="git clone", stderr="nope")
            ),
        )
        with pytest.raises(ResolverError):
            resolve(
                PluginSource(kind="git", location="https://example.test/x.git"),
                dest=tmp_path / "dest",
            )
