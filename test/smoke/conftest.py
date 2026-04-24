import shutil
import time

import pytest

from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer


def _which_or_skip(name: str) -> str:
    path = shutil.which(name) or shutil.which(f"{name}.cmd")
    if not path:
        pytest.skip(f"{name} not on PATH; skipping smoke test")
    return path


@pytest.fixture(scope="session")
def wezterm_bin() -> str:
    return _which_or_skip("wezterm")


@pytest.fixture(scope="session")
def claude_bin() -> str:
    return _which_or_skip("claude")


@pytest.fixture(scope="session")
def codex_bin() -> str:
    return _which_or_skip("codex")


@pytest.fixture
def multiplexer(wezterm_bin: str) -> WezTermMultiplexer:
    return WezTermMultiplexer(wezterm_bin=wezterm_bin)


def _wait_for_text(
    multiplexer: WezTermMultiplexer,
    session: str,
    window: str,
    needle: str,
    timeout: float = 15.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = multiplexer.get_history(session, window)
        if needle in text:
            return True
        time.sleep(0.5)
    return False


@pytest.fixture
def wait_for_text():
    return _wait_for_text
