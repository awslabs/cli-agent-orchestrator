"""Kiro agent identity checks (#1074): the header predicate and the agent-store lookup.

kiro-cli answers an ``--agent`` it cannot load with ``agent "<name>" not found,
using "kiro_default"`` and starts its built-in default agent, which has every
built-in tool. The pane strings below are the ones measured on kiro-cli 2.32.0
(spike SK0), plus real captures from ``fixtures/``.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.providers.kiro_identity import (
    KIRO_FALLBACK_AGENTS,
    KiroAgentIdentityError,
    header_agent,
    kiro_agent_defined,
    kiro_agent_dirs,
    require_header_identity,
    require_kiro_agent_defined,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SEPARATOR = "─" * 60


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _screen(header: str, requested: str = "developer", *, fallback: bool = False) -> str:
    """A launch screen in the measured layout: echoed command, chrome, header, prompt."""
    rows = [f"bash-3.2$ kiro-cli chat --agent-engine v2 --trust-all-tools --agent {requested}"]
    if fallback:
        rows.append(f'● agent "{requested}" not found, using "kiro_default"')
    rows += [
        SEPARATOR,
        " Trust All Tools active, confirmations are off · /quit to exit",
        SEPARATOR,
        f"{header}                                          /private/tmp/repo",
        "",
        "›  ask a question or describe a task ↵",
    ]
    return "\n".join(rows) + "\n"


class TestHeaderAgent:
    @pytest.mark.parametrize(
        ("fixture", "agent"),
        [
            # kiro-cli 2.x TUI: "developer · claude-opus-4.6-1m · ◔ 3%  ~/project · (main)"
            ("kiro_cli_tui_idle_output.txt", "developer"),
            # kiro-cli 2.25, raw with SGR colour codes around every column.
            ("kiro_cli_tui_2_25_idle_output.txt", "developer"),
            # kiro-cli 2.33.1 running the built-in fallback agent.
            ("kiro_cli_tui_composer_single_line.txt", "kiro_default"),
        ],
    )
    def test_reads_the_agent_from_real_captures(self, fixture, agent):
        assert header_agent(_fixture(fixture)) == agent

    def test_the_requested_name_elsewhere_on_screen_is_not_the_identity(self):
        """The fallback screen names the requested agent twice, never in the header."""
        screen = _screen("kiro_default · auto · ◔ 4% · Midway: 10h 20m", fallback=True)
        assert screen.count("developer") == 2
        assert header_agent(screen) == "kiro_default"

    def test_no_header_row_reads_as_none(self):
        screen = "\n".join(_screen("x").splitlines()[:-3])  # drop header and prompt
        assert "developer" in screen
        assert header_agent(screen) is None

    def test_the_last_header_row_is_the_live_one(self):
        stale = _screen("developer · auto · ◔ 1% · auth ok")
        live = _screen("kiro_default · auto · ◔ 4% · auth ok")
        assert header_agent(stale + live) == "kiro_default"
        assert header_agent(live + stale) == "developer"

    @pytest.mark.parametrize(
        "row",
        [
            "~/project · (main)",  # the cwd column wrapped onto its own row
            " Trust All Tools active, confirmations are off · /quit to exit",
            "kiro_default · auto",  # no context gauge
            'agent "developer" not found, using "kiro_default"',
        ],
    )
    def test_rows_without_the_header_shape_are_ignored(self, row):
        assert header_agent(row) is None

    @pytest.mark.parametrize("gauge", ["◔ 1%", "◑ 52%", "● 100%", "◔11%"])
    def test_the_gauge_glyph_is_not_pinned(self, gauge):
        assert header_agent(f"developer · claude-opus-5.5 · {gauge} · Midway: 1h") == "developer"


class TestRequireHeaderIdentity:
    def test_the_expected_agent_passes(self):
        require_header_identity(
            "sk0-readonly", header_agent("sk0-readonly · claude-opus-5.5 · ◔ 1% · Midway: 10h 23m")
        )

    @pytest.mark.parametrize("observed", sorted(KIRO_FALLBACK_AGENTS) + ["reviewer"])
    def test_any_other_agent_is_a_mismatch(self, observed):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("developer", observed)
        assert excinfo.value.kind == "identity_mismatch"
        assert excinfo.value.observed == observed
        assert excinfo.value.agent == "developer"
        assert "No task was sent" in str(excinfo.value)

    @pytest.mark.parametrize("agent", sorted(KIRO_FALLBACK_AGENTS))
    def test_a_fallback_identity_never_passes_even_when_requested(self, agent):
        with pytest.raises(KiroAgentIdentityError, match="instead of"):
            require_header_identity(agent, agent)

    def test_no_header_fails_closed(self):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("developer", None)
        assert excinfo.value.kind == "header_not_found"
        assert excinfo.value.observed is None

    def test_the_error_is_a_value_error(self):
        """API routes map it with the other Kiro configuration refusals (400)."""
        assert issubclass(KiroAgentIdentityError, ValueError)


class TestKiroAgentDefined:
    def test_the_file_cao_install_writes_counts(self, tmp_path):
        (tmp_path / "developer.json").write_text(json.dumps({"name": "developer"}))
        assert kiro_agent_defined("developer", [tmp_path])

    def test_an_aim_style_agent_directory_counts(self, tmp_path):
        """docs/kiro-cli.md: AIM installs ``<agents>/librarian/agent-spec.json``."""
        (tmp_path / "librarian").mkdir()
        (tmp_path / "librarian" / "agent-spec.json").write_text("{}")
        assert kiro_agent_defined("librarian", [tmp_path])

    @pytest.mark.parametrize("relative", ["custom-file.json", "pack/agent.json"])
    def test_a_spec_whose_name_matches_counts(self, tmp_path, relative):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": "developer"}))
        assert kiro_agent_defined("developer", [tmp_path])

    @pytest.mark.parametrize(
        "content",
        [json.dumps({"name": "reviewer"}), "not json", json.dumps(["developer"])],
    )
    def test_other_specs_do_not_count(self, tmp_path, content):
        (tmp_path / "other.json").write_text(content)
        assert not kiro_agent_defined("developer", [tmp_path])

    def test_a_directory_named_like_the_file_does_not_count(self, tmp_path):
        (tmp_path / "developer.json").mkdir()
        assert not kiro_agent_defined("developer", [tmp_path])

    def test_an_empty_agent_directory_does_not_count(self, tmp_path):
        (tmp_path / "developer").mkdir()
        assert not kiro_agent_defined("developer", [tmp_path])

    def test_missing_directories_are_skipped(self, tmp_path):
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "developer.json").write_text("{}")
        assert kiro_agent_defined("developer", [tmp_path / "a", tmp_path / "b"])
        assert not kiro_agent_defined("developer", [tmp_path / "a"])

    def test_a_separator_in_the_name_cannot_leave_the_directory(self, tmp_path):
        outside = tmp_path / "outside.json"
        outside.write_text("{}")
        agents = tmp_path / "agents"
        agents.mkdir()
        assert not kiro_agent_defined("../outside", [agents])


class TestKiroAgentDirs:
    def test_workspace_install_home_and_kiro_home_are_searched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("KIRO_HOME", str(tmp_path / "server-kiro"))
        dirs = kiro_agent_dirs(
            str(tmp_path / "repo"),
            tmp_path / "installed",
            {"KIRO_HOME": str(tmp_path / "pane-kiro")},
        )
        assert dirs == [
            tmp_path / "repo" / ".kiro" / "agents",
            tmp_path / "installed",
            tmp_path / "home" / ".kiro" / "agents",
            tmp_path / "pane-kiro" / "agents",
            tmp_path / "server-kiro" / "agents",
        ]

    def test_a_relative_kiro_home_adds_nothing_and_duplicates_collapse(self, tmp_path, monkeypatch):
        """kiro-cli reads a relative KIRO_HOME as ~/.kiro, which is already searched."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("KIRO_HOME", "relative/kiro")
        dirs = kiro_agent_dirs(str(tmp_path), tmp_path / ".kiro" / "agents")
        assert dirs == [tmp_path / ".kiro" / "agents"]


class TestRequireKiroAgentDefined:
    def test_a_defined_agent_passes(self, tmp_path):
        (tmp_path / "developer.json").write_text("{}")
        require_kiro_agent_defined("developer", [tmp_path])

    def test_a_missing_agent_is_refused_with_the_remedy(self, tmp_path):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_kiro_agent_defined("developer", [tmp_path])
        message = str(excinfo.value)
        assert excinfo.value.kind == "agent_not_found"
        assert "kiro_default" in message and str(tmp_path) in message
        assert "cao install developer --provider kiro_cli" in message

    @pytest.mark.parametrize("agent", sorted(KIRO_FALLBACK_AGENTS))
    def test_a_fallback_name_is_refused_even_with_a_file(self, tmp_path, agent):
        (tmp_path / f"{agent}.json").write_text(json.dumps({"name": agent}))
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_kiro_agent_defined(agent, [tmp_path])
        assert excinfo.value.kind == "fallback_agent"
