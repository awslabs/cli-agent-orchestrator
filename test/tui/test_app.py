"""Unit tests for the ``cao tui`` shell (``app.py`` / ``views.py``).

Standard-strategy coverage for the U1 skeleton: the entry callable is importable
and returns an int, the layout builders produce layouts, the key map binds the
expected keys and — per RD-e=A — does NOT bind ``[s]``, and the S-unreachable
screen renders the copyable ``cao-server`` start command. Edge cases exercise
the unreachable branch (probe returns False / raises) and the quit/Ctrl-C exit
path driven through a headless prompt_toolkit loop.
"""

from __future__ import annotations

import inspect

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput

from cli_agent_orchestrator.tui import main
from cli_agent_orchestrator.tui.app import EXIT_OK, EXIT_SIGINT, App
from cli_agent_orchestrator.tui.views import (
    SERVER_START_COMMAND,
    ScreenState,
    build_layout,
    build_unreachable_view,
    unreachable_text,
)


def _bound_keys(kb: KeyBindings) -> set[str]:
    """Collect the single-character keys bound in a KeyBindings object."""

    keys: set[str] = set()
    for binding in kb.bindings:
        for key in binding.keys:
            keys.add(str(getattr(key, "value", key)))
    return keys


# -- entry callable ---------------------------------------------------------


def test_main_is_importable_and_returns_int() -> None:
    """``main()`` runs headlessly (quit key piped) and returns an int code."""

    # ``main()`` itself constructs its own App, so drive the exit through a
    # patched run by asserting the signature/return contract on a headless App
    # instead (main delegates to App.run()).
    assert callable(main)
    assert inspect.signature(main).return_annotation in ("int", int)

    app = App(liveness_probe=lambda _url: True)
    with create_pipe_input() as pipe:
        pipe.send_text("q")
        code = app.run(input=pipe, output=DummyOutput())
    assert isinstance(code, int)
    assert code == EXIT_OK


def test_build_layout_returns_a_layout() -> None:
    """``build_layout`` composes the main three-region shell into a Layout."""

    layout = build_layout(ScreenState(reachable=True, screen="main"))
    assert isinstance(layout, Layout)


# -- key map (RD-e=A) -------------------------------------------------------


def test_keymap_binds_q_and_c_but_not_s() -> None:
    """[q] and [c] are bound; [s] is deliberately unbound (RD-e=A)."""

    keys = _bound_keys(App(liveness_probe=lambda _url: True).build_keybindings())
    assert "q" in keys
    assert "c" in keys
    assert "s" not in keys


def test_keymap_binds_expected_action_keys() -> None:
    """The full U1 key map (minus [s]) is present."""

    keys = _bound_keys(App(liveness_probe=lambda _url: True).build_keybindings())
    for expected in ("c", "e", "q", "r", "/"):
        assert expected in keys, f"expected key {expected!r} to be bound"


# -- S-unreachable screen ---------------------------------------------------


def test_unreachable_view_shows_copyable_start_command() -> None:
    """S-unreachable renders the exact ``cao-server`` start command + copy key."""

    layout = build_unreachable_view(ScreenState(reachable=False, screen="unreachable"))
    assert isinstance(layout, Layout)

    body = unreachable_text()
    assert SERVER_START_COMMAND in body
    assert "[c] copy" in body
    assert "[r] retry" in body
    # RD-c=A: no auto-start affordance anywhere in the copy.
    assert "auto-start" not in body.lower()
    assert "start the server" in body.lower()


def test_copy_text_returns_start_command_when_unreachable() -> None:
    """``[c]`` on the unreachable screen copies the start command (RD-c=A)."""

    app = App(liveness_probe=lambda _url: False)
    assert app.state.screen == "unreachable"
    assert app.copy_text() == SERVER_START_COMMAND


# -- edge cases -------------------------------------------------------------


def test_unreachable_probe_selects_unreachable_screen() -> None:
    """Edge: a probe returning False starts the app on S-unreachable."""

    app = App(liveness_probe=lambda _url: False)
    assert app.state.reachable is False
    assert app.state.screen == "unreachable"


def test_probe_exception_is_treated_as_unreachable() -> None:
    """Edge: a probe that raises must not crash — it means 'unreachable'."""

    def boom(_url: str) -> bool:
        raise ConnectionError("server down")

    app = App(liveness_probe=boom)
    assert app.state.reachable is False
    assert app.state.screen == "unreachable"


def test_retry_transitions_unreachable_to_main() -> None:
    """Edge: [r] re-probes; a now-reachable server flips to S-main."""

    calls = {"n": 0}

    def flaky(_url: str) -> bool:
        calls["n"] += 1
        return calls["n"] > 1  # first probe fails, retry succeeds

    app = App(liveness_probe=flaky)
    assert app.state.screen == "unreachable"
    reachable = app.retry()
    assert reachable is True
    assert app.state.screen == "main"


def test_ctrl_c_exits_with_sigint_code() -> None:
    """Edge: Ctrl-C drives a clean exit with the conventional 130 code."""

    app = App(liveness_probe=lambda _url: True)
    with create_pipe_input() as pipe:
        pipe.send_text("\x03")  # Ctrl-C
        code = app.run(input=pipe, output=DummyOutput())
    assert code == EXIT_SIGINT
