"""Contract tests for multiplexer backend selection."""

from __future__ import annotations

import sys
import types
from unittest.mock import Mock

import pytest

from cli_agent_orchestrator.multiplexers import get_multiplexer
from cli_agent_orchestrator.multiplexers.tmux import TmuxMultiplexer

WEZTERM_MODULE = "cli_agent_orchestrator.multiplexers.wezterm"


@pytest.fixture(autouse=True)
def reset_selection_state(monkeypatch: pytest.MonkeyPatch) -> None:
    get_multiplexer.cache_clear()
    for name in ("CAO_MULTIPLEXER", "TMUX", "WEZTERM_PANE", "TERM_PROGRAM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delitem(sys.modules, WEZTERM_MODULE, raising=False)
    yield
    get_multiplexer.cache_clear()
    monkeypatch.delitem(sys.modules, WEZTERM_MODULE, raising=False)


def install_fake_wezterm(monkeypatch: pytest.MonkeyPatch, sentinel: object) -> None:
    module = types.ModuleType(WEZTERM_MODULE)
    module.WezTermMultiplexer = sentinel
    monkeypatch.setitem(sys.modules, WEZTERM_MODULE, module)


def test_override_tmux_wins_over_other_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_MULTIPLEXER", "tmux")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")
    monkeypatch.setenv("WEZTERM_PANE", "66")
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")

    multiplexer = get_multiplexer()

    assert isinstance(multiplexer, TmuxMultiplexer)


def test_override_wezterm_imports_lazy_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    factory = Mock(return_value=sentinel)
    install_fake_wezterm(monkeypatch, factory)
    monkeypatch.setenv("CAO_MULTIPLEXER", "wezterm")

    multiplexer = get_multiplexer()

    assert multiplexer is sentinel


def test_invalid_override_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_MULTIPLEXER", "foo")

    with pytest.raises(
        ValueError,
        match=r"Unknown CAO_MULTIPLEXER: 'foo'; expected 'tmux' or 'wezterm'",
    ):
        get_multiplexer()


def test_tmux_env_selects_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1234,0")

    multiplexer = get_multiplexer()

    assert isinstance(multiplexer, TmuxMultiplexer)


def test_wezterm_pane_selects_wezterm(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    factory = Mock(return_value=sentinel)
    install_fake_wezterm(monkeypatch, factory)
    monkeypatch.setenv("WEZTERM_PANE", "66")

    multiplexer = get_multiplexer()

    assert multiplexer is sentinel


def test_term_program_wezterm_selects_wezterm(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    factory = Mock(return_value=sentinel)
    install_fake_wezterm(monkeypatch, factory)
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")

    multiplexer = get_multiplexer()

    assert multiplexer is sentinel


def test_win32_default_selects_wezterm(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    factory = Mock(return_value=sentinel)
    install_fake_wezterm(monkeypatch, factory)
    monkeypatch.setattr(sys, "platform", "win32")

    multiplexer = get_multiplexer()

    assert multiplexer is sentinel


def test_non_windows_default_selects_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")

    multiplexer = get_multiplexer()

    assert isinstance(multiplexer, TmuxMultiplexer)


def test_get_multiplexer_uses_singleton_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAO_MULTIPLEXER", "tmux")

    first = get_multiplexer()
    second = get_multiplexer()

    assert first is second


def test_cache_is_clear_between_tests() -> None:
    assert get_multiplexer.cache_info().currsize == 0
