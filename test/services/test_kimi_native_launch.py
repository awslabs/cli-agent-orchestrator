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
