"""Regression for the #803 review: an explicit ``allowedTools: []`` is a restriction.

Skipping the MCP append for an explicit list (issue #772) made ``allowedTools: []``
resolve to ``[]`` rather than to ``['@cao-mcp-server']``. ``[]`` is falsy, and the
consumers below tested the recorded policy for truth instead of for ``None``, so a
deliberate deny-all arrived looking like "nothing was resolved", which every one of
them treats as unrestricted. The resolver assertion cannot see any of this, because
each widening happens after the resolver returns.
"""

import shlex
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, create_terminal, get_terminal_metadata
from cli_agent_orchestrator.mcp_server import server as S

SRC = Path(S.__file__).resolve().parents[1]


class TestEmptyPolicySurvivesThePersistenceBoundary:
    """``create_terminal`` serialized ``[]`` as SQL NULL, so it read back as None."""

    @pytest.fixture
    def db(self, tmp_path, monkeypatch):
        db_path = tmp_path / "terminals.db"
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=engine)
        local_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", local_session)
        return local_session

    @staticmethod
    def _roundtrip(tid, allowed):
        create_terminal(tid, "cao-session", "window-0", "codex", "narrow", allowed_tools=allowed)
        return get_terminal_metadata(tid)["allowed_tools"]

    def test_a_deny_all_reads_back_as_a_deny_all(self, db):
        assert self._roundtrip("t-empty", []) == []

    def test_an_unresolved_policy_still_reads_back_as_none(self, db):
        """The control: None has to stay distinguishable from an empty list."""
        assert self._roundtrip("t-none", None) is None

    def test_a_restricted_policy_is_unchanged(self, db):
        assert self._roundtrip("t-some", ["fs_read"]) == ["fs_read"]

    def test_the_stored_policy_still_denies_discovery(self, db):
        """The whole path that widened: resolver, then database, then the guard.

        Driving ``_require_discovery_marker`` with a literal ``[]`` passes either
        way, because the value never reaches it as ``[]``. Only a round trip
        through the database shows the widening.
        """
        recorded = self._roundtrip("t-e2e", [])
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"allowed_tools": recorded})
        with patch.object(S, "requests") as req:
            req.get = MagicMock(return_value=resp)
            verdict = S._require_discovery_marker(
                "11111111-1111-1111-1111-111111111111", "list siblings"
            )
        assert verdict is not None, "a stored deny-all must not pass the discovery guard"


class TestEmptyPolicyStillDeniesDiscovery:
    """``_require_discovery_marker`` reads the recorded list and allows on None."""

    @staticmethod
    def _verdict(recorded):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"allowed_tools": recorded})
        with patch.object(S, "requests") as req:
            req.get = MagicMock(return_value=resp)
            return S._require_discovery_marker("11111111-1111-1111-1111-111111111111", "list")

    def test_a_deny_all_is_refused_discovery(self):
        assert self._verdict([]) is not None

    def test_an_unresolved_policy_is_still_unrestricted(self):
        assert self._verdict(None) is None

    def test_an_explicit_discovery_grant_still_passes(self):
        assert self._verdict(["discovery"]) is None

    def test_a_wildcard_still_passes(self):
        assert self._verdict(["*"]) is None

    def test_a_restricted_policy_without_the_marker_is_still_refused(self):
        assert self._verdict(["fs_read"]) is not None


class TestSoftProvidersKeepAZeroToolInstruction:
    """The SOFT_ENFORCEMENT_PROVIDERS carry their policy as prompt text only.

    With ``[]`` the guard fell through, so the emitted prompt lost both the security
    preamble and the tool sentence while the launch stays unrestricted.
    """

    NO_TOOLS = "You may not use any tools."

    def test_codex_emits_it(self, tmp_path):
        from cli_agent_orchestrator.providers.codex import CodexProvider

        profile = MagicMock(
            model=None, system_prompt="Original.", mcpServers=None, codexProfile=None
        )
        provider = CodexProvider("tid", "sess", "win", "agent", allowed_tools=[])
        with (
            patch(
                "cli_agent_orchestrator.providers.codex.load_agent_profile", return_value=profile
            ),
            patch("cli_agent_orchestrator.providers.codex.CAO_HOME_DIR", tmp_path),
        ):
            command = provider._build_codex_command()
        path = Path(command.split("$(cat ")[1].split(")")[0])
        assert self.NO_TOOLS in path.read_text(encoding="utf-8")

    def test_omp_emits_it(self, tmp_path, monkeypatch):
        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.providers.omp import OmpProvider

        monkeypatch.setattr("cli_agent_orchestrator.providers.omp.CAO_HOME_DIR", tmp_path)
        profile = AgentProfile(name="analyst", description="t", system_prompt="Role")
        provider = OmpProvider(
            terminal_id="t",
            session_name="s",
            window_name="w",
            agent_profile="analyst",
            allowed_tools=[],
        )
        with (
            patch("cli_agent_orchestrator.providers.omp.shutil.which", return_value="/usr/bin/omp"),
            patch("cli_agent_orchestrator.providers.omp.load_agent_profile", return_value=profile),
        ):
            parts = shlex.split(provider._build_omp_command())
        context = Path(parts[parts.index("--append-system-prompt") + 1])
        assert self.NO_TOOLS in context.read_text(encoding="utf-8")

    def test_minimax_emits_it(self, tmp_path):
        from cli_agent_orchestrator.models.agent_profile import AgentProfile
        from cli_agent_orchestrator.providers.minimax_code import MiniMaxCodeProvider

        profile = AgentProfile(name="reviewer", description="t", system_prompt="Review.")
        provider = MiniMaxCodeProvider(
            terminal_id="deadbeef",
            session_name="s",
            window_name="w",
            agent_profile="reviewer",
            allowed_tools=[],
        )
        with (
            patch("cli_agent_orchestrator.providers.minimax_code.CAO_HOME_DIR", tmp_path),
            patch(
                "cli_agent_orchestrator.providers.minimax_code.load_agent_profile",
                return_value=profile,
            ),
            patch(
                "cli_agent_orchestrator.providers.minimax_code.shutil.which",
                return_value="/usr/local/bin/mcode",
            ),
        ):
            command = provider._build_command()
        assert self.NO_TOOLS in shlex.split(command)[4]

    def test_kimi_emits_it(self):
        from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

        profile = MagicMock(model=None, system_prompt="Custom.", mcpServers=None)
        provider = KimiCliProvider("t", "s", "w", agent_profile="dev", allowed_tools=[])
        try:
            with patch(
                "cli_agent_orchestrator.providers.kimi_cli.load_agent_profile",
                return_value=profile,
            ):
                provider._build_kimi_command()
            system_md = Path(provider._temp_dir) / "system.md"
            assert self.NO_TOOLS in system_md.read_text(encoding="utf-8")
        finally:
            provider.cleanup()


class TestNoPolicyGuardTestsTheListForTruth:
    """A source scan, because the widening is one falsy value away at every site.

    Six modules wrote this guard by hand. Three others already used an ``is not None``
    test (``grok_cli``, ``copilot_cli``, ``claude_code``), which is what the correct
    form looks like and is why the divergence was easy to miss.
    """

    SITES = (
        "providers/codex.py",
        "providers/kimi_cli.py",
        "providers/omp.py",
        "providers/minimax_code.py",
        "providers/antigravity_cli.py",
        "providers/hermes.py",
        "services/terminal_service.py",
        "clients/database.py",
    )

    def test_no_site_tests_an_allowlist_for_truth(self):
        offenders = []
        for rel in self.SITES:
            for line in (SRC / rel).read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if "allowed_tools and" in stripped and "not in" in stripped:
                    offenders.append(f"{rel}: {stripped}")
                if "dumps(allowed_tools) if allowed_tools else" in stripped:
                    offenders.append(f"{rel}: {stripped}")
        assert not offenders, (
            "an empty allowedTools is a deny-all; testing it for truth reads it as "
            f"unrestricted: {offenders}"
        )
