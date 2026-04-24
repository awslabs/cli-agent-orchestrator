import pytest

pytestmark = pytest.mark.smoke


def test_spawn_send_get_kill(multiplexer, tmp_path, wait_for_text):
    multiplexer.create_session(
        session_name="cao-smoke-basics",
        window_name="bash",
        terminal_id="smoke-basics",
        working_directory=str(tmp_path),
    )
    try:
        multiplexer.send_keys("cao-smoke-basics", "bash", "echo hello-smoke", enter_count=1)
        assert wait_for_text(multiplexer, "cao-smoke-basics", "bash", "hello-smoke", timeout=10)
    finally:
        multiplexer.kill_session("cao-smoke-basics")
