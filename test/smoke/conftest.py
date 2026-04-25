import os
import shutil
import time

import pytest

from cli_agent_orchestrator.multiplexers.wezterm import WezTermMultiplexer


def _which_or_skip(name: str) -> str:
    path = shutil.which(name) or shutil.which(f"{name}.cmd")
    if not path:
        pytest.skip(f"{name} not on PATH; skipping smoke test")
    return path


def _resolve_wezterm_bin() -> str:
    """Resolve the wezterm CLI executable for smoke testing.

    Resolution order:
      1. ``CAO_WEZTERM_BIN`` env override — explicit path for tests/CI.
      2. ``shutil.which("wezterm")`` — system PATH.
      3. ``WEZTERM_EXECUTABLE_DIR`` + ``wezterm.exe`` — set by the WezTerm
         GUI itself when CAO runs inside a WezTerm pane (note: portable
         extracts are not on PATH but expose this var). Fall through to
         ``wezterm`` (no extension) for non-Windows installs.
    """
    override = os.environ.get("CAO_WEZTERM_BIN")
    if override:
        return override

    found = shutil.which("wezterm") or shutil.which("wezterm.cmd")
    if found:
        return found

    install_dir = os.environ.get("WEZTERM_EXECUTABLE_DIR")
    if install_dir:
        for candidate in ("wezterm.exe", "wezterm"):
            full = os.path.join(install_dir, candidate)
            if os.path.isfile(full):
                return full

    pytest.skip(
        "wezterm not resolved; set CAO_WEZTERM_BIN, add to PATH, or run "
        "inside a WezTerm pane (WEZTERM_EXECUTABLE_DIR)"
    )


@pytest.fixture(scope="session")
def wezterm_bin() -> str:
    return _resolve_wezterm_bin()


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
