"""Kiro identity on kiro-cli 2.35.1, on the v2 and v3 engines (#1167).

The ``kiro_cli_tui_2_35_1_*`` fixtures are rendered screens (200x50,
``capture-pane -p``) of kiro-cli 2.35.1 launches, each in a throwaway
``KIRO_HOME``. The only edit is the scratch directory in the right-hand cwd
column, which became the same-width ``kv3.XXXXXX``.

- v2: plain (``v2ro · auto · ◔ 8%``), with a pinned model, and with a pinned
  model and ``--effort low`` (``v2ro · claude-haiku-5.5 · low · ◔ 2%``).
- v3: plain, and with a pinned thinking model at low and medium effort
  (``v3ro · claude-haiku-5.5 · thinking · low · ◔ 1%``).
- The v3 engine's fallback to its ``Default`` agent (read, write, shell and
  web): for a v2-only agent file, a missing name and an invalid agent file, plus
  the ``/tools`` panel of the first. Each prints a notice, ``agent "<name>" …,
  using "default"``, above a ``Default`` header.

Elided, wrapped and plan-mode screens are built from these captures in the tests.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.kiro_cli import IDENTITY_HEADER_READS, KiroCliProvider
from cli_agent_orchestrator.providers.kiro_identity import (
    KIRO_FALLBACK_AGENTS,
    PLAN_MODE_AGENT,
    KiroAgentIdentityError,
    fallback_notice,
    header_agents,
    in_plan_mode,
    is_partial_agent_name,
    require_header_identity,
    require_kiro_agent_defined,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_V2_PLAIN = "kiro_cli_tui_2_35_1_v2_plain.txt"
_V2_MODEL = "kiro_cli_tui_2_35_1_v2_model.txt"
_V2_EFFORT = "kiro_cli_tui_2_35_1_v2_model_effort.txt"
_V3_PLAIN = "kiro_cli_tui_2_35_1_v3_plain.txt"
_V3_THINKING_LOW = "kiro_cli_tui_2_35_1_v3_thinking_low.txt"
_V3_THINKING_MEDIUM = "kiro_cli_tui_2_35_1_v3_thinking_medium.txt"
_V3_NEEDS_UPGRADING = "kiro_cli_tui_2_35_1_v3_fallback_needs_upgrading.txt"
_V3_NOT_FOUND = "kiro_cli_tui_2_35_1_v3_fallback_not_found.txt"
_V3_INVALID = "kiro_cli_tui_2_35_1_v3_fallback_invalid_config.txt"
_V3_TOOLS_PANEL = "kiro_cli_tui_2_35_1_v3_fallback_tools_panel.txt"
# kiro-cli 2.34.2 on v2: ``• agent "sk1-broken" not found, using "kiro_default"; …``
# above a ``kiro_default`` header.
_V2_FALLBACK = "kiro_cli_tui_2_34_2_fallback_launch.txt"

_LONG_AGENT = "kiro-rev-correctness-and-concurrency-and-error-handling-review"  # 61 chars


def _fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _header_index(rows):
    (at,) = [i for i, row in enumerate(rows) if header_agents(row)]
    return at


def _with_header_rows(name: str, new_rows) -> str:
    """The capture ``name`` with its one header row replaced by ``new_rows``."""
    rows = _fixture(name).split("\n")
    at = _header_index(rows)
    return "\n".join(rows[:at] + list(new_rows) + rows[at + 1 :])


def _renamed(name: str, old: str, new: str) -> str:
    """The capture ``name`` with the agent its header names replaced, in that row only."""
    rows = _fixture(name).split("\n")
    at = _header_index(rows)
    assert rows[at].startswith(f"{old} · ")
    rows[at] = new + rows[at][len(old) :]
    return "\n".join(rows)


def _provider_reading(screens, agent):
    backend = MagicMock()
    if isinstance(screens, str):
        backend.get_history.return_value = screens
    else:
        backend.get_history.side_effect = list(screens)
    return backend, KiroCliProvider("hc000001", "hc-session", "hc-window", agent)


async def _verify(backend, provider):
    with (
        patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
        patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0),
    ):
        await provider._verify_agent_identity()


_HEALTHY = [
    (_V2_PLAIN, "v2ro", "v2ro · auto · ◔"),
    (_V2_MODEL, "v2ro", "v2ro · claude-haiku-5.5 · ◔"),
    (_V2_EFFORT, "v2ro", "v2ro · claude-haiku-5.5 · low · ◔"),
    (_V3_PLAIN, "v3ro", "v3ro · auto · ◔"),
    (_V3_THINKING_LOW, "v3ro", "v3ro · claude-haiku-5.5 · thinking · low · ◔"),
    (_V3_THINKING_MEDIUM, "v3star", "v3star · claude-haiku-5.5 · thinking · medium · ◔"),
]


class TestAHeaderWithExtraColumns:
    """``name · model · [extra columns ·] ◔ N%``: the effort and thinking columns."""

    @pytest.mark.parametrize(("fixture", "agent", "drawn"), _HEALTHY)
    def test_every_2_35_1_capture_names_its_agent(self, fixture, agent, drawn):
        screen = _fixture(fixture)
        assert screen.split("\n")[_header_index(screen.split("\n"))].startswith(drawn)
        assert header_agents(screen) == [agent]
        assert fallback_notice(screen, agent) is None
        require_header_identity(
            agent, header_agents(screen), fallback=fallback_notice(screen, agent)
        )

    @pytest.mark.parametrize(
        "extra", [[], ["low"], ["thinking", "low"], ["a", "b", "c"], ["x" * 16]]
    )
    def test_up_to_three_short_extra_columns_are_read(self, extra):
        row = " · ".join(["v2ro", "claude-haiku-5.5", *extra, "◔ 2%"])
        assert header_agents(row) == ["v2ro"]

    @pytest.mark.parametrize(
        "row",
        [
            # Four extra columns: past the bound.
            "v2ro · claude-haiku-5.5 · a · b · c · d · ◔ 2%",
            # One extra column longer than 16 characters.
            "v2ro · claude-haiku-5.5 · " + "x" * 17 + " · ◔ 2%",
            # An extra column holding a space.
            "v2ro · claude-haiku-5.5 · thinking low · ◔ 2%",
            # A wrapped cwd and a log line, each with a `·` and a percentage.
            "~/work/kiro · (main) · uploads · 3 of 4 done · 75%",
            "12:00:01 · build · step 3 of 4 · 75% done",
            # A long row of `·`-separated words ending in a gauge.
            " · ".join(["word"] * 40) + " · ◔ 1%",
        ],
    )
    def test_an_adversarial_row_is_not_a_header_row(self, row):
        assert header_agents(row) == []

    def test_an_adversarial_row_on_the_real_screen_leaves_its_identity(self):
        """Above or below the real header, at column 0, it adds no second agent."""
        rows = _fixture(_V2_EFFORT).split("\n")
        at = _header_index(rows)
        junk = ["~/work/kiro · (main) · uploads · 3 of 4 done · 75%", "12:00:01 · build · step 3"]
        screen = "\n".join(rows[:at] + junk + rows[at : at + 1] + junk + rows[at + 1 :])
        assert header_agents(screen) == ["v2ro"]
        require_header_identity("v2ro", header_agents(screen))

    @pytest.mark.parametrize("indent", [" ", "\t", "\u00a0", " " * 160])
    def test_an_indented_row_with_extra_columns_is_not_a_header_row(self, indent):
        row = f"{indent}kiro_default · claude-haiku-5.5 · thinking · low · ◔ 1%"
        assert header_agents(row) == []
        assert header_agents(_fixture(_V2_EFFORT) + row) == ["v2ro"]

    @pytest.mark.parametrize("fixture", [_V2_EFFORT, _V3_THINKING_LOW])
    def test_a_second_agents_row_with_extra_columns_is_a_mismatch(self, fixture):
        screen = _fixture(fixture) + "kiro_default · claude-haiku-5.5 · low · ◔ 2%\n"
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(header_agents(screen)[0], header_agents(screen))
        assert excinfo.value.kind == "identity_mismatch"


class TestTheV3DefaultFallback:
    """The v3 engine falls back to ``Default``; neither spelling is ever an identity."""

    def test_both_spellings_are_fallback_agents(self):
        assert {"Default", "default"} <= KIRO_FALLBACK_AGENTS

    @pytest.mark.parametrize(
        ("fixture", "requested"),
        [(_V3_NEEDS_UPGRADING, "v2ro"), (_V3_NOT_FOUND, "kv3-nosuch"), (_V3_INVALID, "v3bad")],
    )
    def test_every_fallback_capture_is_refused(self, fixture, requested):
        screen = _fixture(fixture)
        assert f'agent "{requested}"' in screen
        assert header_agents(screen) == ["Default"]
        assert fallback_notice(screen, requested) == "default"
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(
                requested, header_agents(screen), fallback=fallback_notice(screen, requested)
            )
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", "Default")
        assert "No task was sent" in str(excinfo.value)

    @pytest.mark.parametrize("agent", ["Default", "default"])
    def test_a_default_header_is_refused_even_when_requested(self, agent):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(agent, [agent])
        assert excinfo.value.kind == "identity_mismatch"

    @pytest.mark.parametrize("agent", ["Default", "default"])
    def test_an_explicit_request_is_refused_before_launch(self, tmp_path, agent):
        (tmp_path / f"{agent}.json").write_text("{}")
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_kiro_agent_defined(agent, [tmp_path])
        assert excinfo.value.kind == "fallback_agent"

    def test_the_tools_panel_shows_why_and_has_no_header(self):
        screen = _fixture(_V3_TOOLS_PANEL)
        assert {"read", "shell", "web", "write"} <= set(screen.split())
        assert header_agents(screen) == []
        assert fallback_notice(screen, "v2ro") == "default"


class TestTheFallbackNotice:
    """Provisional (#1167): a notice that kiro-cli did not load the requested agent
    refuses, on either engine, even under a header naming that agent. A notice
    naming another agent does not refuse on its own; the header decides."""

    @pytest.mark.parametrize(
        ("fixture", "requested"),
        [(_V3_NEEDS_UPGRADING, "v2ro"), (_V3_NOT_FOUND, "kv3-nosuch"), (_V3_INVALID, "v3bad")],
    )
    def test_a_v3_notice_under_the_requested_header_is_refused(self, fixture, requested):
        screen = _renamed(fixture, "Default", requested)
        assert header_agents(screen) == [requested]
        require_header_identity(requested, header_agents(screen))  # the header alone passes
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(
                requested, header_agents(screen), fallback=fallback_notice(screen, requested)
            )
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", "default")
        assert "No task was sent" in str(excinfo.value)

    def test_a_v2_notice_under_the_requested_header_is_refused(self):
        """The real 2.34.2 fallback screen, its header renamed to the agent asked for."""
        screen = _renamed(_V2_FALLBACK, "kiro_default", "sk1-broken")
        assert header_agents(screen) == ["sk1-broken"]
        require_header_identity("sk1-broken", header_agents(screen))  # the header alone passes
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(
                "sk1-broken",
                header_agents(screen),
                fallback=fallback_notice(screen, "sk1-broken"),
            )
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", "kiro_default")

    @pytest.mark.parametrize(
        ("row", "fallback"),
        [
            ('● agent "x" not found, using "kiro_default"', "kiro_default"),
            (
                '• agent "x" not found, using "kiro_default"; invalid agent config: x.json',
                "kiro_default",
            ),
            ('• agent "x" not found, using "default"', "default"),
            (
                '• agent "x" needs upgrading for this agent engine, using "default" — run /upgrade-agent',
                "default",
            ),
            ('• agent "x" has an invalid config, using "default" — x.json: Schema …', "default"),
            # A fallback outside KIRO_FALLBACK_AGENTS is read the same way.
            ('• agent "x" has an invalid config, using "kiro_planner"', "kiro_planner"),
        ],
    )
    def test_v2_and_v3_notices_for_the_requested_agent_are_read(self, row, fallback):
        assert fallback_notice(row, "x") == fallback

    def test_a_notice_refuses_whatever_fallback_it_names(self):
        """Provisional (#1167): the refusal does not depend on the fallback being a
        known one, so a fallback agent another engine adds is refused too."""
        assert "kiro_planner" not in KIRO_FALLBACK_AGENTS
        row = '• agent "x" has an invalid config, using "kiro_planner"'
        for fallback in ("kiro_planner", fallback_notice(row, "x")):
            with pytest.raises(KiroAgentIdentityError) as excinfo:
                require_header_identity("x", ["x"], fallback=fallback)
            assert (excinfo.value.kind, excinfo.value.observed) == (
                "identity_mismatch",
                "kiro_planner",
            )

    @pytest.mark.parametrize(
        ("row", "agent"),
        [
            ('● agent "reviewer" not found, using "kiro_default"', "developer"),
            ('• agent "developer-2" not found, using "default"', "developer"),
            ('• agent "dev" not found, using "default"', "developer"),
            # The name is compared as text, not as a pattern.
            ('• agent "aXb" not found, using "default"', "a.b"),
        ],
    )
    def test_a_notice_for_another_agent_is_not_read(self, row, agent):
        assert fallback_notice(row, agent) is None

    @pytest.mark.parametrize(
        ("fixture", "fallback"),
        [
            (_V2_FALLBACK, "kiro_default"),
            (_V3_NOT_FOUND, "Default"),
            (_V3_NEEDS_UPGRADING, "Default"),
        ],
    )
    def test_a_notice_for_another_agent_leaves_the_header_to_decide(self, fixture, fallback):
        """The header passes when it names the agent asked for, and refuses when it
        names the fallback; the other agent's notice changes neither."""
        loaded = _renamed(fixture, fallback, "developer")
        assert fallback_notice(loaded, "developer") is None
        require_header_identity(
            "developer", header_agents(loaded), fallback=fallback_notice(loaded, "developer")
        )
        unloaded = _fixture(fixture)
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(
                "developer",
                header_agents(unloaded),
                fallback=fallback_notice(unloaded, "developer"),
            )
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", fallback)

    @pytest.mark.parametrize(
        "row",
        [
            "WARNING: Agent conflict for developer. Using workspace version.",
            "% kiro-cli chat --agent-engine v2 --trust-all-tools --agent developer",
            'kiro-cli chat --agent "developer"',
            " Trust All Tools active, confirmations are off · /quit to exit",
        ],
    )
    def test_other_rows_are_not_a_notice(self, row):
        assert fallback_notice(row, "developer") is None

    def test_a_notice_leaves_a_header_refusals_kind(self):
        """No header, or a partial name, is still header_not_found beside a notice."""
        notice = f'agent "{_LONG_AGENT}" not found, using "default"'
        assert fallback_notice(notice, _LONG_AGENT) == "default"
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_LONG_AGENT, [], fallback="default")
        assert excinfo.value.kind == "header_not_found"
        partial = header_agents(f"{notice}\n─────\nkiro-rev-corr… · claude-haiku-5.5 · low · ◔ 2%")
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(
                _LONG_AGENT, partial, fallback=fallback_notice(notice, _LONG_AGENT)
            )
        assert excinfo.value.kind == "header_not_found"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fixture", "fallback", "requested", "drawn"),
        [
            (_V3_NOT_FOUND, "Default", "kv3-nosuch", "default"),
            (_V2_FALLBACK, "kiro_default", "sk1-broken", "kiro_default"),
        ],
    )
    async def test_the_provider_refuses_and_types_nothing(
        self, fixture, fallback, requested, drawn
    ):
        screen = _renamed(fixture, fallback, requested)
        backend, provider = _provider_reading(screen, requested)
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            await _verify(backend, provider)
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", drawn)
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_provider_passes_beside_another_agents_notice(self):
        backend, provider = _provider_reading(_renamed(_V3_NOT_FOUND, "Default", "v3ro"), "v3ro")
        await _verify(backend, provider)
        assert provider.identity_verified is True
        backend.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_notice_on_an_earlier_read_still_refuses(self):
        """A partial name is read again; the notice seen first is not forgotten."""
        first = _with_header_rows(_V3_NOT_FOUND, ["kv3-n… · auto · ◔ 1%"])
        later = _renamed(_V3_PLAIN, "v3ro", "kv3-nosuch")
        backend, provider = _provider_reading([first, later], "kv3-nosuch")
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            await _verify(backend, provider)
        assert excinfo.value.kind == "identity_mismatch"
        assert backend.get_history.call_count == 2
        assert provider.identity_verified is False


class TestAPartialNameWithExtraColumnsStillRefuses:
    def test_an_elided_name_is_header_not_found(self):
        drawn = "kiro-rev-correctness-and-concurr…"
        screen = _renamed(_V2_EFFORT, "v2ro", drawn)
        assert header_agents(screen) == [drawn] and is_partial_agent_name(drawn)
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_LONG_AGENT, header_agents(screen))
        assert excinfo.value.kind == "header_not_found"

    def test_a_name_wrapped_at_the_pane_edge_is_header_not_found(self):
        head, tail = _LONG_AGENT[:30], _LONG_AGENT[30:]
        screen = _with_header_rows(
            _V3_THINKING_LOW, [head, f"{tail} · claude-haiku-5.5 · thinking · low · ◔ 1%"]
        )
        assert header_agents(screen) == [f"…{tail}"]
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_LONG_AGENT, header_agents(screen))
        assert excinfo.value.kind == "header_not_found"

    @pytest.mark.asyncio
    async def test_the_provider_reads_again_then_refuses(self):
        screen = _renamed(_V3_THINKING_LOW, "v3ro", "kiro-rev-correctness-a…")
        backend, provider = _provider_reading(screen, _LONG_AGENT)
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            await _verify(backend, provider)
        assert excinfo.value.kind == "header_not_found"
        assert backend.get_history.call_count == IDENTITY_HEADER_READS
        assert provider.identity_verified is False


class TestPlanModeWithExtraColumns:
    def _plan_screen(self):
        screen = _renamed(_V2_EFFORT, "v2ro", PLAN_MODE_AGENT)
        assert "/copy to clipboard" in screen
        return screen.replace("/copy to clipboard", "exit plan mode: shift+tab")

    def test_the_plan_header_is_read_and_refused(self):
        screen = self._plan_screen()
        assert header_agents(screen) == [PLAN_MODE_AGENT]
        assert in_plan_mode(screen)
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("v2ro", header_agents(screen), plan_mode=True)
        assert excinfo.value.kind == "identity_mismatch"
        assert "plan mode" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_the_provider_refuses_persistent_plan_mode(self):
        backend, provider = _provider_reading(self._plan_screen(), "v2ro")
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            await _verify(backend, provider)
        assert excinfo.value.kind == "identity_mismatch"
        assert provider.identity_verified is False


class TestAV2LaunchWithEffortPassesIdentity:
    """Regression: before #1167 this screen was refused as ``header_not_found``."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("fixture", "agent"), [(_V2_EFFORT, "v2ro"), (_V3_THINKING_LOW, "v3ro")]
    )
    async def test_the_provider_verifies_on_the_first_read(self, fixture, agent):
        backend, provider = _provider_reading(_fixture(fixture), agent)
        await _verify(backend, provider)
        assert provider.identity_verified is True
        assert backend.get_history.call_count == 1
        backend.send_keys.assert_not_called()
