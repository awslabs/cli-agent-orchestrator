import time

import pytest

pytestmark = pytest.mark.smoke


def test_pipe_pane_captures_rapid_output(multiplexer, tmp_path):
    log_path = tmp_path / "pane.log"
    multiplexer.create_session(
        session_name="cao-smoke-pipe",
        window_name="bash",
        terminal_id="smoke-pipe",
        working_directory=str(tmp_path),
    )
    try:
        multiplexer.pipe_pane("cao-smoke-pipe", "bash", str(log_path))
        for i in range(5):
            multiplexer.send_keys("cao-smoke-pipe", "bash", f"echo MARK-{i}", enter_count=1)

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
            if all(f"MARK-{i}" in text for i in range(5)):
                multiplexer.stop_pipe_pane("cao-smoke-pipe", "bash")
                return
            time.sleep(0.5)
        pytest.fail(
            "Poller did not capture all markers; last log:\n"
            f"{log_path.read_text(encoding='utf-8') if log_path.exists() else '<no file>'}"
        )
    finally:
        multiplexer.kill_session("cao-smoke-pipe")
