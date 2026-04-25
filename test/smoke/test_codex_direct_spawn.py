import sys

import pytest

from cli_agent_orchestrator.multiplexers.launch import build_launch_spec
from cli_agent_orchestrator.providers.codex import (
    CODEX_WELCOME_PATTERN,
    TRUST_PROMPT_PATTERN,
)

pytestmark = pytest.mark.smoke


def test_codex_direct_spawn_two_step_send(multiplexer, codex_bin, tmp_path, wait_for_text):
    # Mirror CodexProvider._build_codex_argv: without --no-alt-screen, output goes
    # to the alt-screen and `wezterm cli get-text` (scrollback) sees nothing.
    flags = ["--yolo", "--no-alt-screen", "--disable", "shell_snapshot"]
    if sys.platform == "win32":
        flags = ["-c", "hooks=[]", *flags]
    spec = build_launch_spec(
        "codex",
        [codex_bin, *flags],
        platform="windows" if sys.platform == "win32" else "unix",
    )
    multiplexer.create_session(
        session_name="cao-smoke-codex",
        window_name="codex-0",
        terminal_id="smoke-codex",
        working_directory=str(tmp_path),
        launch_spec=spec,
    )
    try:
        # Codex shows a workspace trust prompt on first open of an unknown
        # directory (tmp_path). Mirror CodexProvider._handle_trust_prompt:
        # wait for the trust banner, dismiss with Enter, then confirm the
        # welcome banner appears before driving the composer.
        if wait_for_text(
            multiplexer, "cao-smoke-codex", "codex-0", TRUST_PROMPT_PATTERN, timeout=15
        ):
            multiplexer.send_special_key("cao-smoke-codex", "codex-0", "Enter")
        assert wait_for_text(
            multiplexer, "cao-smoke-codex", "codex-0", CODEX_WELCOME_PATTERN, timeout=15
        ), "Codex never reached its welcome banner"
        multiplexer.send_keys("cao-smoke-codex", "codex-0", "/help", enter_count=1)
        assert wait_for_text(multiplexer, "cao-smoke-codex", "codex-0", "/help", timeout=15)
    finally:
        multiplexer.kill_session("cao-smoke-codex")
