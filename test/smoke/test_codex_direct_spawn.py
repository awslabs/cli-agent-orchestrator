import sys

import pytest

from cli_agent_orchestrator.multiplexers.launch import build_launch_spec

pytestmark = pytest.mark.smoke


def test_codex_direct_spawn_two_step_send(multiplexer, codex_bin, tmp_path, wait_for_text):
    spec = build_launch_spec(
        "codex",
        [codex_bin],
        platform="windows" if sys.platform == "win32" else "unix",
        working_directory=str(tmp_path),
    )
    multiplexer.create_session(
        session_name="cao-smoke-codex",
        window_name="codex-0",
        terminal_id="smoke-codex",
        working_directory=str(tmp_path),
        launch_spec=spec,
    )
    try:
        multiplexer.send_keys("cao-smoke-codex", "codex-0", "/help", enter_count=1)
        assert wait_for_text(multiplexer, "cao-smoke-codex", "codex-0", "/help", timeout=15)
    finally:
        multiplexer.kill_session("cao-smoke-codex")
