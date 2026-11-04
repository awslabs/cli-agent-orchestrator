"""Kiro agent identity checks (#1074): the header predicate and the agent-store lookup.

kiro-cli answers an ``--agent`` it cannot load with ``agent "<name>" not found,
using "kiro_default"`` and starts its built-in default agent, which has every
built-in tool. The pane strings below are the ones measured on kiro-cli 2.32.0,
plus real captures from ``fixtures/``.

``kiro_cli_tui_2_34_2_fallback_launch.txt`` is the rendered screen (220x50,
``capture-pane -p``) of a CAO launch on kiro-cli 2.34.2 whose agent file did
not load: requested ``sk1-broken``, header ``kiro_default · auto · ◔ 4%``.
``kiro_cli_tui_2_34_2_plan_mode_reconstructed.txt`` is NOT a capture: it is that
screen with the agent loaded and in Kiro's plan mode, as a reviewer described it
on kiro-cli 2.35.1 (header ``Plan · <model>``, hint ``exit plan mode:
shift+tab``). Replace it with a capture when one is taken.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.kiro_identity import (
    _HEADER_ROW,
    KIRO_FALLBACK_AGENTS,
    PLAN_MODE_AGENT,
    KiroAgentIdentityError,
    header_agents,
    in_plan_mode,
    is_partial_agent_name,
    kiro_agent_defined,
    kiro_agent_dirs,
    require_header_identity,
    require_kiro_agent_defined,
)
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

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


def _kiro_captures():
    """Real Kiro captures only; a reconstructed screen says so in its name."""
    return sorted(p for p in FIXTURES_DIR.glob("kiro_cli_*.txt") if "reconstructed" not in p.name)


def _rows_of_header_shape(text: str):
    """(column, agent) for every row of header shape, at any indentation."""
    found = []
    for row in strip_terminal_escapes(text).splitlines():
        stripped = row.lstrip()
        match = _HEADER_ROW.match(stripped)
        if match:
            found.append((len(row) - len(stripped), match.group("agent")))
    return found


class TestHeaderAgents:
    @pytest.mark.parametrize(
        ("fixture", "agent"),
        [
            # kiro-cli 2.x TUI: "developer · claude-opus-4.6-1m · ◔ 3%  ~/project · (main)"
            ("kiro_cli_tui_idle_output.txt", "developer"),
            # kiro-cli 2.25, raw with SGR colour codes around every column.
            ("kiro_cli_tui_2_25_idle_output.txt", "developer"),
            # kiro-cli 2.33.1 running the built-in fallback agent.
            ("kiro_cli_tui_composer_single_line.txt", "kiro_default"),
            # kiro-cli 2.34.2: no auth column, right-aligned cwd, the fallback warning
            # names the requested agent and its file.
            ("kiro_cli_tui_2_34_2_fallback_launch.txt", "kiro_default"),
        ],
    )
    def test_reads_the_agent_from_real_captures(self, fixture, agent):
        assert header_agents(_fixture(fixture)) == [agent]

    def test_every_real_capture_draws_its_header_rows_at_column_0_naming_one_agent(self):
        """The rule's premise, on every Kiro capture in the repo that has a header."""
        checked = 0
        for path in _kiro_captures():
            text = path.read_text(encoding="utf-8")
            rows = _rows_of_header_shape(text)
            if not rows:
                continue
            checked += 1
            assert {column for column, _ in rows} == {0}, path.name
            assert len({agent for _, agent in rows}) == 1, path.name
            (agent,) = {agent for _, agent in rows}
            assert header_agents(text) == [agent], path.name
            if agent not in KIRO_FALLBACK_AGENTS:
                require_header_identity(agent, header_agents(text))
        # 2.x, 2.25, 2.33.1 and 2.34.2 captures; more may be added.
        assert checked >= 13

    def test_the_requested_name_elsewhere_on_screen_is_not_the_identity(self):
        """The fallback screen names the requested agent twice, never in the header."""
        screen = _screen("kiro_default · auto · ◔ 4% · Midway: 10h 20m", fallback=True)
        assert screen.count("developer") == 2
        assert header_agents(screen) == ["kiro_default"]

    def test_no_header_row_reads_as_empty(self):
        screen = "\n".join(_screen("x").splitlines()[:-3])  # drop header and prompt
        assert "developer" in screen
        assert header_agents(screen) == []

    def test_header_rows_naming_two_agents_are_refused_in_either_order(self):
        """No row is the live one: a stale or added header row never decides alone."""
        requested = _screen("developer · auto · ◔ 1% · auth ok")
        fallback = _screen("kiro_default · auto · ◔ 4% · auth ok")
        for screen in (requested + fallback, fallback + requested):
            assert sorted(header_agents(screen)) == ["developer", "kiro_default"]
            with pytest.raises(KiroAgentIdentityError) as excinfo:
                require_header_identity("developer", header_agents(screen))
            assert excinfo.value.kind == "identity_mismatch"
            assert set(excinfo.value.observed.split(", ")) == {"developer", "kiro_default"}

    def test_repeated_header_rows_of_one_agent_pass(self):
        """Redraw residue of the same header, as in the real 2.25 multi-turn capture."""
        multiturn = _fixture("kiro_cli_tui_2_25_multiturn_output.txt")
        assert [agent for _, agent in _rows_of_header_shape(multiturn)] == ["developer"] * 2
        assert header_agents(multiturn) == ["developer"]
        require_header_identity("developer", header_agents(multiturn))
        twice = _screen("developer · auto · ◔ 1% · auth ok") * 2
        require_header_identity("developer", header_agents(twice))

    @pytest.mark.parametrize("indent", [" ", "  ", "\t", "\u00a0", " " * 160])
    def test_an_indented_row_of_header_shape_is_not_a_header_row(self, indent):
        assert header_agents(f"{indent}kiro_default · auto · ◔ 4%") == []
        screen = (
            _screen("developer · auto · ◔ 1% · auth ok") + f"{indent}kiro_default · auto · ◔ 4%"
        )
        assert header_agents(screen) == ["developer"]

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
        assert header_agents(row) == []

    @pytest.mark.parametrize("gauge", ["◔ 1%", "◑ 52%", "● 100%", "◔11%"])
    def test_the_gauge_glyph_is_not_pinned(self, gauge):
        assert header_agents(f"developer · claude-opus-5.5 · {gauge} · Midway: 1h") == ["developer"]


# A row naming the requested agent, placed on the real 2.34.2 fallback screen at
# CAO's fixed pane width (clients/tmux.py: 220 columns). Whether kiro-cli ever
# renders such a row is not known; what each case models is named on it.
_WIDTH = 220
_REQUESTED = "sk1-broken"
_PAYLOAD = f"{_REQUESTED} · auto · ◔ 1%"
_LIVE_LEFT = "kiro_default · auto · ◔ 4%"


def _fallback_rows():
    rows = _fixture("kiro_cli_tui_2_34_2_fallback_launch.txt").split("\n")
    (at,) = [i for i, row in enumerate(rows) if header_agents(row)]
    assert rows[at].startswith(_LIVE_LEFT)
    return rows, at


def _header_line(cwd: str) -> str:
    """The 2.34.2 header layout: agent, model and gauge, then the cwd right-aligned."""
    return _LIVE_LEFT + " " * max(2, _WIDTH - len(_LIVE_LEFT) - len(cwd)) + cwd


def _header_replaced_by(new_rows):
    rows, at = _fallback_rows()
    return "\n".join(rows[:at] + new_rows + rows[at + 1 :])


def _inserted_after_header(new_rows, offset=1):
    rows, at = _fallback_rows()
    return "\n".join(rows[: at + offset] + new_rows + rows[at + offset :])


def _inserted_above_header(new_rows):
    """Below the fallback warning on row 1, above the chrome and the real header."""
    rows, _ = _fallback_rows()
    return "\n".join(rows[:1] + new_rows + rows[1:])


def _soft_wrapped_cwd():
    """A cwd that overflows the header row; the terminal wraps it at column 220."""
    prefix = "/tmp/" + "a" * (_WIDTH - len(_LIVE_LEFT) - 2 - len("/tmp/"))
    line = _header_line(prefix + _PAYLOAD)
    assert line[_WIDTH:].startswith(_REQUESTED)
    return _header_replaced_by([line[i : i + _WIDTH] for i in range(0, len(line), _WIDTH)])


def _box_wrapped_cwd():
    """The TUI wraps its own cwd column: the continuation row is indented to it."""
    column = _WIDTH - 60
    return _header_replaced_by(
        [
            _LIVE_LEFT + " " * (column - len(_LIVE_LEFT)) + "/tmp/" + "b" * 55,
            " " * column + _PAYLOAD,
        ]
    )


def _newline_in_cwd():
    """A directory name holding a newline, rendered raw."""
    return _header_replaced_by(_header_line("/tmp/x\n" + _PAYLOAD).split("\n"))


def _bare_word_above_header():
    """A bare word replaces the rule above the real header, so its name reads as the
    tail of a wrapped one; a column-0 requested-agent row follows it."""
    rows, at = _fallback_rows()
    rows = rows[: at - 1] + ["x"] + rows[at:]
    return "\n".join(rows[: at + 1] + [_PAYLOAD] + rows[at + 1 :])


def _header_lost_its_shape():
    """The real header wraps before its gauge; the payload gets its own indented row."""
    return _header_replaced_by(
        [
            "kiro_default · auto ·   /tmp/aaaaaaaa",
            "◔ 4%                   bbbbbbbb",
            " " * 22 + _PAYLOAD,
        ]
    )


_SPOOFED_SCREENS = {
    "soft-wrapped cwd": (_soft_wrapped_cwd, "identity_mismatch"),
    "box-wrapped cwd": (_box_wrapped_cwd, "identity_mismatch"),
    "newline in the cwd": (_newline_in_cwd, "identity_mismatch"),
    "column-0 row under the header": (
        lambda: _inserted_after_header([_PAYLOAD]),
        "identity_mismatch",
    ),
    "column-0 row above the header": (
        lambda: _inserted_above_header([_PAYLOAD]),
        "identity_mismatch",
    ),
    "NBSP-joined indented row": (
        lambda: _inserted_after_header([" " * 180 + _PAYLOAD.replace(" ", "\u00a0")]),
        "identity_mismatch",
    ),
    "a crafted rule, then the row": (
        lambda: _inserted_after_header([" " * 180 + "─" * 30, " " * 180 + _PAYLOAD]),
        "identity_mismatch",
    ),
    "composer continuation line": (
        lambda: _inserted_after_header(["› hi", "  " + _PAYLOAD], offset=2),
        "identity_mismatch",
    ),
    "composer line at column 0": (
        lambda: _inserted_after_header(["› hi", _PAYLOAD], offset=2),
        "identity_mismatch",
    ),
    "row below the hint": (
        lambda: _inserted_after_header(["   " + _PAYLOAD], offset=4),
        "identity_mismatch",
    ),
    "real header lost its shape": (_header_lost_its_shape, "header_not_found"),
    "a bare word above the real header": (_bare_word_above_header, "header_not_found"),
}


class TestARequestedAgentRowOnTheRealFallbackScreen:
    """kiro-cli 2.34.2 fell back to ``kiro_default``; a row naming the request is added."""

    def test_the_unmodified_capture_is_refused(self):
        rows, _ = _fallback_rows()
        assert _REQUESTED in rows[0]  # the fallback warning names the request
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_REQUESTED, header_agents("\n".join(rows)))
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", "kiro_default")

    @pytest.mark.parametrize("case", sorted(_SPOOFED_SCREENS))
    def test_the_launch_is_refused(self, case):
        build, kind = _SPOOFED_SCREENS[case]
        screen = build()
        assert _PAYLOAD.replace(" ", "") in screen.replace(" ", "").replace("\u00a0", "")
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_REQUESTED, header_agents(screen))
        assert excinfo.value.kind == kind
        assert excinfo.value.agent == _REQUESTED
        assert excinfo.value.observed != _REQUESTED

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", sorted(_SPOOFED_SCREENS))
    async def test_the_provider_records_no_identity_and_types_nothing(self, case):
        build, kind = _SPOOFED_SCREENS[case]
        backend = MagicMock()
        backend.get_history.return_value = build()
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", _REQUESTED)
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity()
        assert excinfo.value.kind == kind
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_same_screen_with_the_requested_agent_loaded_passes(self):
        """Back-compat on 2.34.2: the real layout, with the agent that was asked for."""
        rows, at = _fallback_rows()
        rows = rows[1:]  # no fallback warning when the agent loads
        rows[at - 1] = rows[at - 1].replace("kiro_default", _REQUESTED, 1)
        backend = MagicMock()
        backend.get_history.return_value = "\n".join(rows)
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", _REQUESTED)
        with patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend):
            await provider._verify_agent_identity()
        assert provider.identity_verified is True
        assert backend.get_history.call_count == 1


_LONG_AGENT = "kiro-rev-correctness-and-concurrency-and-error-handling-review"  # 61 chars
_RULE = "─" * 60


def _with_header(header_rows):
    """The 2.34.2 screen with the agent loaded, its header replaced by ``header_rows``."""
    rows, at = _fallback_rows()
    return "\n".join(rows[1:at] + header_rows + rows[at + 1 :])


class TestAPartialNameIsNotAnIdentity:
    """An elided or wrapped header name maps to header_not_found, not identity_mismatch."""

    @pytest.mark.parametrize(
        "drawn",
        [
            "kiro-rev-correctness-and-concurr…",  # elided at the end
            "kiro-rev-correctness-and-concurr...",  # ASCII ellipsis
            "kiro-rev-corr…error-handling-review",  # elided in the middle
        ],
    )
    def test_an_elided_name_is_header_not_found(self, drawn):
        screen = _with_header([f"{drawn} · auto · ◔ 1%"])
        assert header_agents(screen) == [drawn] and is_partial_agent_name(drawn)

        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_LONG_AGENT, header_agents(screen))

        assert excinfo.value.kind == "header_not_found"
        assert excinfo.value.observed == drawn
        assert "partial agent name" in str(excinfo.value) and "No task was sent" in str(
            excinfo.value
        )

    def test_a_name_wrapped_at_the_pane_edge_is_header_not_found(self):
        """A 30-column pane cuts the 61-character name: its tail opens the next row."""
        head, tail = _LONG_AGENT[:30], _LONG_AGENT[30:]
        screen = "\n".join([_RULE[:30], head, f"{tail} · auto · ◔ 1%", "", "› "])
        assert header_agents(screen) == [f"…{tail}"]

        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_LONG_AGENT, header_agents(screen))

        assert excinfo.value.kind == "header_not_found"

    def test_a_header_wrapped_before_its_gauge_is_header_not_found(self):
        screen = _with_header([f"{_LONG_AGENT} · auto ·", "◔ 1%"])
        assert header_agents(screen) == []
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_LONG_AGENT, header_agents(screen))
        assert excinfo.value.kind == "header_not_found"

    def test_a_full_name_of_another_agent_still_wins(self):
        """The fallback's full name beside a partial one is a mismatch, as before."""
        screen = _inserted_after_header(["sk1-bro… · auto · ◔ 1%"])
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_REQUESTED, header_agents(screen))
        assert excinfo.value.kind == "identity_mismatch"
        assert "kiro_default" in excinfo.value.observed

    def test_the_requested_name_beside_a_partial_one_is_header_not_found(self):
        screen = _with_header([f"{_REQUESTED} · auto · ◔ 1%", "", "sk1-bro… · auto · ◔ 1%"])
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity(_REQUESTED, header_agents(screen))
        assert excinfo.value.kind == "header_not_found"

    def test_every_real_capture_has_only_full_names(self):
        for path in _kiro_captures():
            assert not any(
                is_partial_agent_name(n) for n in header_agents(path.read_text(encoding="utf-8"))
            ), path.name

    @pytest.mark.asyncio
    async def test_the_provider_reads_again_then_refuses(self):
        from cli_agent_orchestrator.providers.kiro_cli import IDENTITY_HEADER_READS

        backend = MagicMock()
        backend.get_history.return_value = _with_header(["kiro-rev-correctness-a… · auto · ◔ 1%"])
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", _LONG_AGENT)
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity()
        assert excinfo.value.kind == "header_not_found"
        assert backend.get_history.call_count == IDENTITY_HEADER_READS
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_name_drawn_whole_on_a_later_read_passes(self):
        backend = MagicMock()
        backend.get_history.side_effect = [
            _with_header(["kiro-rev-correctness-a… · auto · ◔ 1%"]),
            _with_header([f"{_LONG_AGENT} · auto · ◔ 1%"]),
        ]
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", _LONG_AGENT)
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0),
        ):
            await provider._verify_agent_identity()
        assert provider.identity_verified is True


_PLAN_SCREEN = "kiro_cli_tui_2_34_2_plan_mode_reconstructed.txt"


class TestPlanMode:
    """Plan mode's ``Plan`` header hides the agent's name; it is never an identity."""

    def test_the_screen_reads_as_plan_mode(self):
        screen = _fixture(_PLAN_SCREEN)
        assert header_agents(screen) == [PLAN_MODE_AGENT]
        assert in_plan_mode(screen)

    def test_a_plan_header_without_the_hint_is_not_plan_mode(self):
        screen = _fixture(_PLAN_SCREEN).replace("exit plan mode: shift+tab", "/copy to clipboard")
        assert header_agents(screen) == [PLAN_MODE_AGENT]
        assert not in_plan_mode(screen)

    def test_plan_mode_is_a_mismatch_for_any_other_agent(self):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("developer", [PLAN_MODE_AGENT], plan_mode=True)
        assert excinfo.value.kind == "identity_mismatch"
        assert "plan mode" in str(excinfo.value)

    def test_an_agent_literally_named_plan_passes_in_plan_mode(self):
        require_header_identity(PLAN_MODE_AGENT, [PLAN_MODE_AGENT], plan_mode=True)

    @pytest.mark.asyncio
    async def test_persistent_plan_mode_is_read_again_then_refused(self):
        from cli_agent_orchestrator.providers.kiro_cli import IDENTITY_HEADER_READS

        backend = MagicMock()
        backend.get_history.return_value = _fixture(_PLAN_SCREEN)
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", "developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity()
        assert excinfo.value.kind == "identity_mismatch"
        assert "plan mode" in str(excinfo.value)
        assert backend.get_history.call_count == IDENTITY_HEADER_READS
        assert provider.identity_verified is False
        backend.send_keys.assert_not_called()
        backend.send_special_key.assert_not_called()

    @pytest.mark.asyncio
    async def test_plan_mode_that_clears_is_verified_from_the_agent_header(self):
        loaded = _fixture(_PLAN_SCREEN).replace("Plan · auto", "developer · auto", 1)
        backend = MagicMock()
        backend.get_history.side_effect = [_fixture(_PLAN_SCREEN), loaded]
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", "developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            patch("cli_agent_orchestrator.providers.kiro_cli.IDENTITY_HEADER_READ_INTERVAL_S", 0),
        ):
            await provider._verify_agent_identity()
        assert provider.identity_verified is True
        assert backend.get_history.call_count == 2

    @pytest.mark.asyncio
    async def test_a_plan_header_without_the_hint_is_refused_on_the_first_read(self):
        backend = MagicMock()
        backend.get_history.return_value = _fixture(_PLAN_SCREEN).replace(
            "exit plan mode: shift+tab", "/copy to clipboard"
        )
        provider = KiroCliProvider("rv000001", "rv-session", "rv-window", "developer")
        with (
            patch("cli_agent_orchestrator.providers.kiro_cli.get_backend", return_value=backend),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            await provider._verify_agent_identity()
        assert (excinfo.value.kind, excinfo.value.observed) == ("identity_mismatch", "Plan")
        assert backend.get_history.call_count == 1


class TestRequireHeaderIdentity:
    def test_the_expected_agent_passes(self):
        require_header_identity(
            "sk0-readonly", header_agents("sk0-readonly · claude-opus-5.5 · ◔ 1% · Midway: 10h 23m")
        )

    @pytest.mark.parametrize("observed", sorted(KIRO_FALLBACK_AGENTS) + ["reviewer"])
    def test_any_other_agent_is_a_mismatch(self, observed):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("developer", [observed])
        assert excinfo.value.kind == "identity_mismatch"
        assert excinfo.value.observed == observed
        assert excinfo.value.agent == "developer"
        assert "No task was sent" in str(excinfo.value)

    @pytest.mark.parametrize(
        "observed",
        [["kiro_default", "developer"], ["developer", "kiro_default"], ["developer", "reviewer"]],
    )
    def test_the_expected_agent_among_others_is_a_mismatch(self, observed):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("developer", observed)
        assert excinfo.value.kind == "identity_mismatch"
        assert excinfo.value.observed == ", ".join(observed)
        assert "more than one agent" in str(excinfo.value)
        assert "No task was sent" in str(excinfo.value)

    @pytest.mark.parametrize("agent", sorted(KIRO_FALLBACK_AGENTS))
    def test_a_fallback_identity_never_passes_even_when_requested(self, agent):
        with pytest.raises(KiroAgentIdentityError, match="instead of"):
            require_header_identity(agent, [agent])

    def test_no_header_fails_closed(self):
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_header_identity("developer", [])
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

    def test_deeply_nested_json_declares_nothing_and_the_search_goes_on(self, tmp_path):
        """A repo's ``.kiro/agents/x.json`` of nested brackets overflows the JSON
        parser's recursion; it must read as "no name here", not escape the create
        as a RecursionError (a 500) for every Kiro launch from that repo."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "x.json").write_text("[" * 200_000)
        installed = tmp_path / "installed"
        installed.mkdir()
        (installed / "developer.json").write_text("{}")

        assert not kiro_agent_defined("developer", [workspace])
        assert kiro_agent_defined("developer", [workspace, installed])

    def test_a_spec_over_the_size_cap_is_not_parsed_for_its_name(self, tmp_path):
        """Files over 1 MiB are skipped by the ``name`` scan; the stem still counts."""
        big = json.dumps({"name": "developer", "prompt": "x" * (1024 * 1024)})
        (tmp_path / "big-spec.json").write_text(big)
        assert not kiro_agent_defined("developer", [tmp_path])

        (tmp_path / "developer.json").write_text(big)
        assert kiro_agent_defined("developer", [tmp_path])

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
        assert "kiro_default" in message
        assert "cao install developer --provider kiro_cli" in message

    def test_the_searched_directories_go_to_the_server_log_not_the_message(self, tmp_path, caplog):
        """The message is the API's 400 body; a shared server's paths stay in its log."""
        workspace, installed = tmp_path / "repo" / ".kiro" / "agents", tmp_path / "installed"
        with (
            caplog.at_level("WARNING", logger="cli_agent_orchestrator.providers.kiro_identity"),
            pytest.raises(KiroAgentIdentityError) as excinfo,
        ):
            require_kiro_agent_defined("developer", [workspace, installed])
        message = str(excinfo.value)
        assert str(tmp_path) not in message
        assert not re.search(r"(^|[\s(,])/", message), message  # no absolute path at all
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert str(workspace) in logged and str(installed) in logged

    @pytest.mark.parametrize("agent", sorted(KIRO_FALLBACK_AGENTS))
    def test_a_fallback_name_is_refused_even_with_a_file(self, tmp_path, agent):
        (tmp_path / f"{agent}.json").write_text(json.dumps({"name": agent}))
        with pytest.raises(KiroAgentIdentityError) as excinfo:
            require_kiro_agent_defined(agent, [tmp_path])
        assert excinfo.value.kind == "fallback_agent"
