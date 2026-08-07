"""The pinned `kimi --session <id>` resume argv.

Installed Kimi Code 0.29.0 exposes resume as ``-S, --session [id]`` with
an optional argument: with an id it resumes that session, without one it
opens an interactive picker. Every case here exists because a resume that
silently became a picker would attach the pane to an arbitrary session
while CAO's durable record still named the intended one.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import kimi_native_launch as knl

SESSION = "session_326c5026"


class TestResumeArgv:
    def test_the_pinned_form_is_session_followed_by_the_id(self):
        assert knl.build_resume_argv(session_id=SESSION) == ["kimi", "--session", SESSION]

    def test_the_id_immediately_follows_the_option(self):
        argv = knl.build_resume_argv(session_id=SESSION, extra_args=["--yolo", "--model", "k2"])
        assert argv == ["kimi", "--yolo", "--model", "k2", "--session", SESSION]
        assert argv[argv.index("--session") + 1] == SESSION

    def test_an_explicit_binary_path_is_honored(self):
        argv = knl.build_resume_argv(session_id=SESSION, kimi_binary="/opt/homebrew/bin/kimi")
        assert argv[0] == "/opt/homebrew/bin/kimi"

    def test_an_empty_binary_is_refused(self):
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id=SESSION, kimi_binary="")


class TestNoBareResumeOption:
    """A bare `--session` opens a picker; it must be unreachable."""

    @pytest.mark.parametrize("session_id", ["", None, 0, False, [], {}])
    def test_a_missing_id_is_refused(self, session_id):
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id=session_id)

    @pytest.mark.parametrize(
        "session_id",
        ["--yolo", "-S", "-c", "--session", "-"],
    )
    def test_a_flag_shaped_id_is_refused(self, session_id):
        """The parser would read it as the next flag, leaving --session bare."""
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id=session_id)

    @pytest.mark.parametrize(
        "session_id",
        ["session one", "session\tone", "session\nid", " session", "session "],
    )
    def test_a_whitespace_bearing_id_is_refused(self, session_id):
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id=session_id)

    @pytest.mark.parametrize(
        "session_id",
        ["session;rm -rf /", "session$(id)", "session`id`", "session|cat", "sess*ion"],
    )
    def test_a_shell_metacharacter_id_is_refused(self, session_id):
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id=session_id)

    def test_an_overlong_id_is_refused(self):
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id="s" * 512)

    def test_a_real_provider_session_id_is_accepted(self):
        assert knl.validate_session_id("session_326c5026-4f11-4a1e-9b77-000000000000")


class TestOneResumeOptionOnly:
    @pytest.mark.parametrize("duplicate", ["--session", "-S", "--session=other"])
    def test_a_second_resume_option_in_extra_args_is_refused(self, duplicate):
        with pytest.raises(knl.KimiNativeLaunchError) as exc:
            knl.build_resume_argv(session_id=SESSION, extra_args=["--yolo", duplicate])
        assert "second resume option" in str(exc.value)

    def test_a_non_string_extra_arg_is_refused(self):
        with pytest.raises(knl.KimiNativeLaunchError):
            knl.build_resume_argv(session_id=SESSION, extra_args=["--yolo", 7])


class TestResumesExactly:
    def test_the_built_argv_resumes_exactly_the_requested_session(self):
        argv = knl.build_resume_argv(session_id=SESSION, extra_args=["--yolo"])
        assert knl.resumes_exactly(argv, SESSION) is True

    def test_a_different_session_is_not_an_exact_resume(self):
        argv = knl.build_resume_argv(session_id=SESSION)
        assert knl.resumes_exactly(argv, "session_other") is False

    def test_an_argv_with_no_resume_option_is_not_a_resume(self):
        assert knl.resumes_exactly(["kimi", "--yolo"], SESSION) is False

    def test_a_bare_trailing_resume_option_is_not_a_resume(self):
        assert knl.resumes_exactly(["kimi", "--session"], SESSION) is False

    def test_two_resume_options_are_not_an_exact_resume(self):
        """Which session wins would be the parser's decision, not ours."""
        argv = ["kimi", "--session", SESSION, "-S", "session_other"]
        assert knl.resumes_exactly(argv, SESSION) is False

    def test_the_short_form_is_recognized_when_auditing_a_foreign_argv(self):
        assert knl.resumes_exactly(["kimi", "-S", SESSION], SESSION) is True


# ---------------------------------------------------------------------------
# Rendered native-header exact-session proof (COND-0312)
#
# Kimi Code 0.31.0 rewrites its process title to ``kimi-code`` after parsing,
# so the resumed ``--session <id>`` is no longer observable in the kernel
# argv (Darwin ``KERN_PROCARGS2`` returns the rewritten title).  The TUI
# instead renders a strict native header whose ``Session:`` line names the
# session it is running.  These tests pin the parser that turns that header
# into an exact-session proof, and the version gate that makes the proof
# fail closed for any build whose title-rewrite + header behaviour was not
# read.
# ---------------------------------------------------------------------------

PINNED_0310 = "0.31.0"
DIRECTORY = "/Users/colin/Projects/cao/worktree"


def _header_rows(
    *,
    session: str = SESSION,
    directory: str = DIRECTORY,
    version: str = PINNED_0310,
    model: str = "K3",
) -> list[str]:
    """The Kimi native header, framed the way ``capture-pane`` renders it.

    The label rows sit inside the rounded box's ``│`` verticals exactly as
    the live 0.31.0 pane paints them, so the parser must tolerate that
    chrome rather than be handed pre-stripped text.
    """
    return [
        "│  Welcome to Kimi Code!                                                                              │",
        "│  Send /help for help information.                                                                   │",
        f"│  Directory: {directory}                                                                              │",
        f"│  Session:   {session}                                                                                │",
        f"│  Model:     {model}                                                                                  │",
        f"│  Version:   {version}                                                                                │",
    ]


class TestRenderedSessionProofGate:
    def test_the_live_verified_0310_build_has_a_rendered_session_proof(self):
        proof = knl.rendered_session_proof_for(PINNED_0310)
        assert proof is not None
        assert proof.rule == "kimi-native-header-v1"
        # The evidence must name the build's bytes and the rewrite it proves.
        assert "689fc2a123dfc3145dab26a8e6a86c71a5dc8552b13fe0449679e065ce96774e" in proof.evidence
        assert "kimi-code" in proof.evidence

    def test_the_stage_verified_0320_build_has_a_rendered_session_proof(self):
        # cond-0315: 0.32.0 keeps the title rewrite, so it earns its own
        # keyed proof — never an inheritance from 0.31.0.
        proof = knl.rendered_session_proof_for("0.32.0")
        assert proof is not None
        assert proof.rule == "kimi-native-header-v1"
        assert "b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79" in proof.evidence
        assert "kimi-code" in proof.evidence

    def test_the_stage_verified_0330_build_has_a_rendered_session_proof(self):
        # cond-0315: 0.33.0 keeps the title rewrite, so it earns its own
        # keyed proof — never an inheritance from 0.31.0 or 0.32.0.
        proof = knl.rendered_session_proof_for("0.33.0")
        assert proof is not None
        assert proof.rule == "kimi-native-header-v1"
        assert "0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2" in proof.evidence
        assert "kimi-code" in proof.evidence

    @pytest.mark.parametrize(
        "version",
        ["0.30.0", "0.29.2", "0.29.1", "0.29.0", "0.31.1", "0.32.1", "0.33.1", ""],
    )
    def test_every_other_build_is_unproven_and_must_fail_closed(self, version):
        """Only a build whose title-rewrite + header were read earns the
        rendered proof.  Everything else is None so the launch keeps using
        the argv proof (or freezes, if that build also rewrote its title)."""
        assert knl.rendered_session_proof_for(version) is None


class TestParsesRenderedSession:
    def test_the_live_0310_header_proves_the_exact_session(self):
        assert (
            knl.renders_session_exactly(_header_rows(), SESSION, provider_version=PINNED_0310)
            is True
        )

    def test_a_wrong_session_is_not_proven(self):
        assert (
            knl.renders_session_exactly(
                _header_rows(session="session_deadbeef"), SESSION, provider_version=PINNED_0310
            )
            is False
        )

    def test_an_unproven_build_is_not_proven_even_with_a_perfect_header(self):
        # A header shaped like 0.31.0's must not prove anything for a build
        # whose behaviour was never read -- this is the negative that keeps
        # an unknown future build from inheriting the proof by accident.
        assert (
            knl.renders_session_exactly(_header_rows(), SESSION, provider_version="0.30.0") is False
        )

    def test_the_rendered_version_must_equal_the_proven_build(self):
        # The header is the TUI's own statement of which version is running;
        # a mismatch with the proven build is unproven rather than coerced.
        assert (
            knl.renders_session_exactly(
                _header_rows(version="0.30.0"), SESSION, provider_version=PINNED_0310
            )
            is False
        )


class TestRenderedSessionParserIsStrict:
    """Every "unproven" answer below is a freeze, never a pass."""

    def test_no_header_at_all_is_unproven(self):
        # A pane that has not rendered yet -- a cold boot, or a pane running
        # something else entirely -- proves nothing.
        assert knl.parse_native_header(["", "auto  K3 thinking: max"]) is None

    def test_a_missing_session_label_is_unproven(self):
        rows = [r for r in _header_rows() if "Session:" not in r]
        assert knl.parse_native_header(rows) is None

    def test_a_duplicated_session_label_is_unproven(self):
        """Two Session lines means which one is real is unknowable from here."""
        rows = _header_rows() + [
            f"│  Session:   session_other                                            │"
        ]
        assert knl.parse_native_header(rows) is None

    def test_a_duplicated_version_label_is_unproven(self):
        rows = _header_rows() + [
            f"│  Version:   0.30.0                                                   │"
        ]
        assert knl.parse_native_header(rows) is None

    def test_a_missing_directory_label_is_unproven(self):
        rows = [r for r in _header_rows() if "Directory:" not in r]
        assert knl.parse_native_header(rows) is None

    def test_an_empty_session_value_is_unproven(self):
        # The picker hazard, rendered: a Session line with no id.
        rows = [r for r in _header_rows() if "Session:" not in r]
        rows.append("│  Session:                                                            │")
        assert knl.parse_native_header(rows) is None

    def test_a_header_scattered_among_other_rendering_is_still_parsed(self):
        # The header is not the only thing on screen; a transcript or status
        # bar around it must not defeat the parse, and must not supply a
        # second stray Session line either.
        rows = [
            "auto  K3 thinking: max  .../worktree  review/branch   /model: switch model",
            *_header_rows(),
            " >",
            "context: 0% (0/1M)",
        ]
        parsed = knl.parse_native_header(rows)
        assert parsed is not None
        assert parsed["session"] == SESSION
        assert parsed["version"] == PINNED_0310
        assert parsed["directory"] == DIRECTORY
        assert parsed["model"] == "K3"

    def test_a_stray_session_substring_outside_a_label_is_not_a_match(self):
        # The session id can appear in a transcript echo; only a labelled
        # ``Session:`` line is the header's own statement.
        rows = [
            f"resuming {SESSION}...",
            *_header_rows(),
        ]
        parsed = knl.parse_native_header(rows)
        assert parsed is not None and parsed["session"] == SESSION


def _gutter_header_rows(
    *,
    session: str = SESSION,
    directory: str = DIRECTORY,
    version: str = PINNED_0310,
    model: str = "K3",
    mcp: bool = False,
) -> list[str]:
    """The header the way a real ``capture-pane`` paints it.

    The TUI mounts the boot header inside ``GutterContainer(1, 1)`` (read
    from the 0.31.0, 0.32.0, and 0.33.0 bundles), so every painted row
    carries a one-cell left pad before the box vertical and trailing pad
    after it.  A parse that requires the row to *start* with the vertical
    never matches the screen the proof actually reads.
    """
    rows = [
        " ╭─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╮",
        " │  Welcome to Kimi Code!                                                                                                                                    │",
        f" │  Directory: {directory}                                                                                                                                    │",
        f" │  Session:   {session}                                                                                                                                      │",
        f" │  Model:     {model}                                                                                                                                        │",
        f" │  Version:   {version}                                                                                                                                      │",
    ]
    if mcp:
        rows.append(
            " │  MCP:       5 connected                                                                                                                                    │"
        )
    rows.append(
        " ╰─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────╯"
    )
    return rows


class TestRealGeometryRenderedHeader:
    """The painted gutter is chrome too: the proof must read the real screen."""

    def test_the_gutter_padded_header_parses(self):
        parsed = knl.parse_native_header(_gutter_header_rows())
        assert parsed is not None
        assert parsed["session"] == SESSION
        assert parsed["version"] == PINNED_0310
        assert parsed["directory"] == DIRECTORY
        assert parsed["model"] == "K3"

    def test_the_gutter_padded_header_with_an_mcp_row_parses(self):
        # A live boot with MCP servers paints one extra ignorable label row.
        parsed = knl.parse_native_header(_gutter_header_rows(mcp=True))
        assert parsed is not None and parsed["session"] == SESSION

    def test_the_gutter_padded_header_proves_the_exact_session(self):
        assert (
            knl.renders_session_exactly(
                _gutter_header_rows(), SESSION, provider_version=PINNED_0310
            )
            is True
        )

    def test_a_gutter_padded_wrong_session_proves_nothing(self):
        assert (
            knl.renders_session_exactly(
                _gutter_header_rows(session="session_deadbeef"),
                SESSION,
                provider_version=PINNED_0310,
            )
            is False
        )

    def test_a_gutter_padded_duplicated_label_fails_closed(self):
        rows = _gutter_header_rows() + [
            f" │  Session:   session_other                                                                                                                                  │"
        ]
        assert knl.parse_native_header(rows) is None
