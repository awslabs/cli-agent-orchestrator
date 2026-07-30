"""Exact-session argv binding for the managed Codex native TUI."""

import pytest

from cli_agent_orchestrator.services import codex_native_launch as cnl

SESSION = "019fb17d-0c6d-7161-a408-6b1fa61c8f2d"


def test_resume_argv_keeps_the_exact_id_as_the_final_selector():
    argv = cnl.build_resume_argv(
        session_id=SESSION,
        codex_binary="/opt/homebrew/bin/codex",
        extra_args=["--yolo", "--model", "gpt-5.6-sol"],
    )
    assert argv == [
        "/opt/homebrew/bin/codex",
        "--yolo",
        "--model",
        "gpt-5.6-sol",
        "resume",
        SESSION,
    ]
    assert cnl.resumes_exactly(argv, SESSION) is True


@pytest.mark.parametrize(
    "session_id",
    ["", None, "not-a-uuid", "019FB17D-0C6D-7161-A408-6B1FA61C8F2D"],
)
def test_noncanonical_session_ids_are_refused(session_id):
    with pytest.raises(cnl.CodexNativeLaunchError):
        cnl.build_resume_argv(session_id=session_id)


@pytest.mark.parametrize("selector", ["--last", "fork", "--ephemeral", "--no-session-persistence"])
def test_indirect_or_nonpersistent_selectors_are_refused(selector):
    with pytest.raises(cnl.CodexNativeLaunchError):
        cnl.build_resume_argv(session_id=SESSION, extra_args=[selector])


@pytest.mark.parametrize("option", ["--help", "-h", "--version", "-V", "--"])
def test_early_exit_and_parser_delimiter_options_are_refused(option):
    with pytest.raises(cnl.CodexNativeLaunchError, match="not an admitted"):
        cnl.build_resume_argv(session_id=SESSION, extra_args=[option])
    assert cnl.resumes_exactly(["codex", option, "resume", SESSION], SESSION) is False


def test_a_different_or_duplicate_resume_is_not_exact():
    assert cnl.resumes_exactly(["codex", "resume", SESSION], SESSION) is True
    assert (
        cnl.resumes_exactly(["codex", "resume", SESSION], "11111111-1111-7111-8111-111111111111")
        is False
    )
    assert (
        cnl.resumes_exactly(
            ["codex", "resume", SESSION, "resume", SESSION],
            SESSION,
        )
        is False
    )
    assert cnl.resumes_exactly(["codex", "resume", SESSION, "ignored"], SESSION) is False
