"""Regression guards for the PR #516 review-round remediation (FR-1…FR-11).

Every test here is **mutation-proven** (NFR-5): each names, in its docstring, the
exact production line whose faithful revert turns it RED. A guard that passes on
the unmodified `9a1581b` tree proves nothing, so the assertions are written
against behaviour an operator would notice, not against the presence of code.

Three construction rules this file obeys throughout:

* **C-6 — headless termination.** Every test that opens the input overlay
  terminates the loop with ``\\x03`` (Ctrl-C, bound under *both* key filters),
  never a printable key. Once the overlay is open, ``q``/``p``/``x``/``c``/``e``
  are typed INTO the buffer and the loop hangs forever.
* **NFR-4 — no hidden network / subprocess.** Every ``App(...)`` here is handed a
  ``catalog=`` double (no ``cao --help`` shell-out) and a ``client=`` double (no
  HTTP GET).
* **FR-5.1 — never the real clipboard.** Every ``App(...)`` is handed a
  ``clipboard=`` double. Omitting it constructs a ``PyperclipClipboard``, which
  would write to the developer's actual OS clipboard during a test run.

Why the FR-2 completion tests drive an **async** loop rather than
:func:`_run_keys`: ``Buffer.insert_text`` schedules its completer as a background
task on the running event loop, so a synchronous ``pipe.send_text`` burst pushes
Tab through *before* the completer coroutine has produced any candidate — the
completion is never applied and a synchronous test would pass for the wrong
reason. Interleaving ``await asyncio.sleep(0)``-style yields lets the completer
resolve, which is what a real operator's keystroke timing does.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence
from unittest import mock

import pytest
from prompt_toolkit.input.base import Input
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.layout.containers import FloatContainer
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.output import DummyOutput

from cli_agent_orchestrator.tui import views
from cli_agent_orchestrator.tui.app import EXIT_CATALOG_FATAL, EXIT_SIGINT, App
from cli_agent_orchestrator.tui.command_builder import CommandBuilder
from cli_agent_orchestrator.tui.command_catalog import (
    CatalogError,
    Command,
    CommandCatalog,
    CommandGroup,
    Param,
)
from cli_agent_orchestrator.tui.runner import CommandRunner
from cli_agent_orchestrator.tui.server_client import ProfileSummary, ServerClient

# Raw terminal byte sequences, as a real terminal sends them.
KEY_DOWN = "\x1b[B"
KEY_UP = "\x1b[A"
KEY_ESC = "\x1b"
KEY_ENTER = "\r"
KEY_TAB = "\t"
KEY_CTRL_C = "\x03"  # the ONLY safe headless terminator once the overlay is open


# --------------------------------------------------------------------------- #
# Fixtures / doubles.                                                           #
# --------------------------------------------------------------------------- #

_GROUPS = [
    CommandGroup("launch", "Launch a session"),
    CommandGroup("session", "Manage sessions"),
    CommandGroup("workflow", "Run workflows"),
]

_SESSION_COMMANDS = [Command("status", "Session status", ["session", "status"])]

# TWO params, so the FR-4.1 wrap is observable: press 1 → param 1, press 2 →
# param 2, press 3 → BACK to param 1. A non-wrapping implementation stalls.
_SESSION_STATUS_PARAMS = [
    Param("--json", "option", required=False, takes_value=False, choices=None, help="JSON"),
    Param("SESSION_NAME", "argument", required=True, takes_value=True, choices=None, help=""),
]

# A choice-bearing option, so the ArgCompleter has something to offer (FR-2).
_LAUNCH_PARAMS = [
    Param(
        "--provider",
        "option",
        required=False,
        takes_value=True,
        choices=["kiro_cli", "claude_code"],
        help="Provider",
    )
]

# A THREE-param leaf. Needed for the cycle-reset test: with only a 1-param second
# command the stale index is masked by the ``% len(params)`` wrap (index 1 into a
# 1-param list is index 0 either way), so a reset-removal mutation would pass.
# Three params make the stale index land on a DIFFERENT param than a fresh cycle.
_WORKFLOW_PARAMS = [
    Param("--one", "option", required=False, takes_value=True, choices=None, help=""),
    Param("--two", "option", required=False, takes_value=True, choices=None, help=""),
    Param("--three", "option", required=False, takes_value=True, choices=None, help=""),
]

_COMMANDS_BY_GROUP: Dict[str, List[Command]] = {
    "session": _SESSION_COMMANDS,
    "launch": [],
    "workflow": [],
}
_PARAMS_BY_PATH = {
    ("session", "status"): _SESSION_STATUS_PARAMS,
    ("launch",): _LAUNCH_PARAMS,
    ("workflow",): _WORKFLOW_PARAMS,
}


def _catalog() -> mock.MagicMock:
    """A catalog double serving the fixtures above (never shells out to ``cao``)."""

    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.groups.return_value = list(_GROUPS)
    catalog.commands.side_effect = lambda group: list(_COMMANDS_BY_GROUP.get(group, []))
    catalog.params.side_effect = lambda path: list(_PARAMS_BY_PATH.get(tuple(path), []))
    return catalog


def _raising_catalog(exc: CatalogError) -> mock.MagicMock:
    """A catalog whose ``groups()`` — the read the arrow path needs — raises.

    ``NavigationModel.move`` clamps against the visible list, and computing that
    list calls ``groups()``. This is the FR-1 fixture.
    """

    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.groups.side_effect = exc
    catalog.commands.side_effect = exc
    catalog.params.side_effect = exc
    return catalog


def _timeout_error(argv: List[str]) -> CatalogError:
    """A TRANSIENT ``CatalogError`` (a ``--help`` timeout)."""

    import subprocess

    exc = CatalogError(argv, message=f"`{' '.join(argv)}` timed out after 10.0s")
    exc.__cause__ = subprocess.TimeoutExpired(cmd=" ".join(argv), timeout=10.0)
    return exc


def _missing_binary_error(argv: List[str]) -> CatalogError:
    """A FATAL ``CatalogError`` (the ``cao`` binary itself is gone)."""

    exc = CatalogError(argv, message="`cao` executable not found")
    exc.__cause__ = FileNotFoundError("cao")
    return exc


class _FakeClipboard:
    """A prompt_toolkit-shaped clipboard double; optionally one that raises.

    Implements only what :meth:`CommandRunner.copy` touches (``set_text``) plus
    the rest of the ``Clipboard`` surface as inert stubs, so the App can pass it
    straight to ``Application(clipboard=...)``.
    """

    def __init__(self, *, raises: bool = False) -> None:
        self.texts: List[str] = []
        self.raises = raises

    def set_text(self, text: str) -> None:
        if self.raises:
            raise RuntimeError("no clipboard mechanism available")
        self.texts.append(text)

    def set_data(self, data: object) -> None:  # pragma: no cover - unused by copy()
        pass

    def get_data(self) -> object:  # pragma: no cover - unused by copy()
        raise NotImplementedError

    def rotate(self) -> None:  # pragma: no cover - unused by copy()
        pass


class _SpyRunner(CommandRunner):
    """A runner that records instead of spawning, and reports copy SUCCESS.

    ``copy`` returns ``True`` deliberately: :meth:`App.copy_current` branches on
    that return value, so a spy returning ``None`` would make every copy look like
    the *fallback* path and quietly invert the FR-5.3 notice assertions.
    """

    def __init__(self) -> None:
        self.ran_argv: Optional[List[str]] = None
        self.copied: List[str] = []

    def run_in_app(self, argv: Sequence[str]) -> None:  # type: ignore[override]
        self.ran_argv = list(argv)

    def copy(self, text: str) -> bool:  # type: ignore[override]
        self.copied.append(text)
        return True


def _app(**kwargs: Any) -> App:
    """An App with every external seam replaced by a double (NFR-4 / FR-5.1)."""

    kwargs.setdefault("catalog", _catalog())
    kwargs.setdefault("client", mock.MagicMock(spec=ServerClient))
    kwargs.setdefault("clipboard", _FakeClipboard())
    return App(liveness_probe=lambda _url: True, **kwargs)


def _run_keys(app: App, *keys: str) -> int:
    """Pipe raw key sequences through a headless loop, terminating with Ctrl-C.

    C-6: the terminator is ``\\x03`` and never a printable key — while the input
    overlay holds focus a printable key is typed into the buffer and the loop
    never exits.
    """

    with create_pipe_input() as pipe:
        pipe.send_text("".join(keys) + KEY_CTRL_C)
        return app.run(input=pipe, output=DummyOutput())


def _rendered(app: App) -> str:
    """Everything the LIVE layout would paint, flattened to text.

    Reads the app's current layout the way prompt_toolkit does at repaint, so an
    assertion here is about what the operator sees — not about model state a
    renderer might never consult.
    """

    lines: List[str] = []
    for window in app.application.layout.find_all_windows():
        if isinstance(window.content, BufferControl):
            # The overlay's own BufferControl lazily loads its history through
            # ``get_app().create_background_task``, which needs a RUNNING event
            # loop — rendering it after ``run()`` has returned raises. Its content
            # is the raw typed text, which no assertion here is about.
            continue
        content = window.content.create_content(200, 100)
        for row in range(min(content.line_count, 40)):
            lines.append("".join(fragment[1] for fragment in content.get_line(row)))
    return "\n".join(lines)


def _drive_async(app: App, script: Callable[[Input], Any]) -> None:
    """Run the app on a real event loop while ``script`` feeds it keys.

    Needed only by the FR-2 completion tests: the completer runs as a background
    task, so the keystrokes must be interleaved with loop yields rather than
    pushed as one synchronous burst.
    """

    async def main() -> None:
        with create_pipe_input() as pipe:
            app.application = app._build_application(input=pipe, output=DummyOutput())

            async def guarded() -> None:
                # C-6, enforced structurally: the Ctrl-C terminator is sent in a
                # ``finally``, so a failing assertion inside ``script`` can never
                # leave the headless loop running forever (it would otherwise hang
                # the whole suite, and a hang reads as "no result" rather than as
                # the failure it is).
                try:
                    await script(pipe)
                finally:
                    pipe.send_text(KEY_CTRL_C)
                    await asyncio.sleep(0.05)

            task = asyncio.ensure_future(guarded())
            await app.application.run_async()
            await task

    asyncio.run(main())


async def _type(pipe: Input, *chunks: str, settle: float = 0.05) -> None:
    """Send chunks with a loop yield after each, so background tasks can run."""

    for chunk in chunks:
        pipe.send_text(chunk)
        await asyncio.sleep(settle)


# =========================================================================== #
# FR-1 (the MUST-FIX) — an arrow key must not leak CatalogError.                #
# =========================================================================== #


def test_move_selection_routes_a_transient_catalog_error_instead_of_raising() -> None:
    """FR-1.1: ``move_selection`` swallows nothing and raises nothing — it CLASSIFIES.

    Mutation target: ``app.py`` :meth:`App.move_selection` — the
    ``try: self.navigation.move(delta) / except CatalogError as exc:
    self._handle_catalog_error(exc)`` block. Reverting it to a bare
    ``self.navigation.move(delta)`` makes this RED immediately, with the
    ``CatalogError`` propagating out of the call (verified).

    This is the fast, unambiguous form of the guard: it drives the exact method
    both arrow bindings call, so nothing else in the app can satisfy it. The
    keyboard-driven tests below then prove the bindings really route through here.
    """

    app = _app(catalog=_raising_catalog(_timeout_error(["cao", "--help"])))
    app.runner = _SpyRunner()

    app.move_selection(1)  # must not raise
    app.move_selection(-1)  # must not raise

    assert app.state.screen == "main", "a transient failure must not leave S-main"
    assert app.fatal_message is None
    assert app.catalog_notice is not None and "timed out" in app.catalog_notice


def test_move_selection_routes_a_fatal_catalog_error_to_the_fatal_screen() -> None:
    """FR-1.1: the fatal branch of the same guard, on the same method.

    Mutation target: the identical ``except CatalogError`` block. Reverting it
    propagates the missing-binary ``CatalogError`` out of ``move_selection``, so
    ``screen`` never becomes ``catalog_fatal`` and this REDs.
    """

    app = _app(catalog=_raising_catalog(_missing_binary_error(["cao", "--help"])))
    app.runner = _SpyRunner()

    app.move_selection(1)  # must not raise

    assert app.state.screen == "catalog_fatal"
    assert app.fatal_message is not None
    assert app.catalog_notice is None, "the fatal path is not a transient notice"


@pytest.mark.parametrize("arrow", [KEY_DOWN, KEY_UP], ids=["down", "up"])
def test_both_arrow_keys_route_through_the_guard_not_around_it(arrow: str) -> None:
    """FR-1.1 names BOTH handlers (``app.py:686`` down and ``:693`` up).

    Guarding only one leaves the other a live crash path, so each arrow is driven
    separately rather than assumed symmetric.

    Mutation target: replacing ``self.move_selection(delta)`` with
    ``self.navigation.move(delta)`` in either binding — the key then bypasses the
    guard, the ``CatalogError`` escapes the handler, and prompt_toolkit paints its
    unrecoverable "Press ENTER to continue…" prompt (verified: the reverted run
    HANGS awaiting that ENTER on a pipe that is already closed, which is exactly
    the reported defect — the event loop dies).
    """

    app = _app(catalog=_raising_catalog(_timeout_error(["cao", "--help"])))
    app.runner = _SpyRunner()

    code = _run_keys(app, arrow)

    assert code == EXIT_SIGINT, "the loop survived the keystroke and exited via Ctrl-C"
    assert app.state.screen == "main"
    assert app.catalog_notice is not None and "timed out" in app.catalog_notice
    assert "Press ENTER to continue" not in _rendered(app)


def test_an_arrow_key_on_a_missing_cao_binary_exits_cleanly_with_the_fatal_screen() -> None:
    """FR-1.1: the fatal arrow path reaches ``EXIT_CATALOG_FATAL`` through the KEY.

    The catalog here is HEALTHY at first paint and fails only inside
    ``navigation.move`` — deliberately, so the pre-existing ``after_render``
    ``_catalog_fatal_guard`` cannot satisfy this test on the first-paint route. The
    requirement's rationale is exactly this: the arrow handlers raised BEFORE any
    render callback ran, so the only clean route to ``EXIT_CATALOG_FATAL`` never
    fired on the arrow path.

    Mutation target: the ``self._exit_if_catalog_fatal(event)`` call in the
    ``down`` binding (plus ``move_selection``'s guard). Removing the exit call
    leaves the app on the fatal screen but never ends the loop, so the run
    terminates via the trailing Ctrl-C with ``EXIT_SIGINT`` and the exit-code
    assertion REDs.
    """

    app = _app()
    app.runner = _SpyRunner()

    with mock.patch.object(
        app.navigation, "move", side_effect=_missing_binary_error(["cao", "--help"])
    ):
        code = _run_keys(app, KEY_DOWN)

    assert code == EXIT_CATALOG_FATAL, f"expected a clean fatal exit, got {code}"
    assert app.state.screen == "catalog_fatal"
    assert app.fatal_message is not None
    rendered = _rendered(app)
    assert "Press ENTER to continue" not in rendered
    # The operator actually SEES the fatal guidance, not just a bare exit code.
    assert "PATH" in rendered


def test_move_selection_on_the_profiles_screen_moves_that_list_not_the_catalog() -> None:
    """The profiles screen's arrows read already-loaded summaries — no catalog call.

    Mutation target: the ``if self.state.screen == "profiles"`` early return in
    :meth:`App.move_selection`. Deleting it routes the profiles arrows into
    ``navigation.move`` instead, so the profile highlight stops moving.
    """

    client = mock.MagicMock(spec=ServerClient)
    client.profiles.return_value = [
        ProfileSummary(name="architect"),
        ProfileSummary(name="developer"),
    ]
    app = _app(client=client)
    app.runner = _SpyRunner()

    _run_keys(app, "p", KEY_DOWN)

    assert app.state.screen == "profiles"
    assert app.profiles_browser.selected_index == 1
    assert app.navigation.selected_index == 0, "the command list must NOT have moved"


# =========================================================================== #
# FR-2 — the completions menu must render, and an accepted completion must take  #
# the same builder path a typed value takes.                                     #
# =========================================================================== #


def test_layout_root_is_a_float_container_with_a_completions_menu() -> None:
    """FR-2.1: the structural precondition for a completion ever being drawn.

    Mutation target: ``views.build_layout``'s ``root = FloatContainer(content=...,
    floats=[Float(..., content=CompletionsMenu(...))])``. Reverting it to
    ``root = HSplit(rows)`` makes this RED — and no completion can be rendered,
    which is the reported defect: the ``ArgCompleter`` computed candidates that
    had nowhere to appear.
    """

    app = _app()
    layout = app._select_layout()

    assert isinstance(layout.container, FloatContainer)  # type: ignore[attr-defined]
    menus = [
        float_.content
        for float_ in layout.container.floats  # type: ignore[attr-defined]
        if isinstance(float_.content, CompletionsMenu)
    ]
    assert menus, "the root FloatContainer carries no CompletionsMenu float"


def test_typed_prefix_renders_a_visible_completion_in_the_menu() -> None:
    """FR-2.1: a typed prefix produces a completion the operator can SEE.

    The structural assertion above is necessary but not sufficient — this drives
    real keys and reads the float's own rendered content, so a menu that is
    present but never populated fails. Mutation target: the same ``FloatContainer``
    root (an ``HSplit`` root leaves the menu out of the layout entirely, so there
    is nothing to read).
    """

    app = _app()
    app.runner = _SpyRunner()
    seen: List[str] = []

    async def script(pipe: Input) -> None:
        # Enter opens the `launch` leaf; [e] opens the overlay on --provider.
        await _type(pipe, KEY_ENTER, "e", "k")
        for float_ in app.application.layout.container.floats:  # type: ignore[attr-defined]
            if isinstance(float_.content, CompletionsMenu):
                content = float_.content.content.content.create_content(40, 10)
                for row in range(content.line_count):
                    seen.append("".join(f[1] for f in content.get_line(row)))

    _drive_async(app, script)

    assert any("kiro_cli" in line for line in seen), f"menu rendered nothing usable: {seen}"


def test_accepted_completion_and_typed_commit_both_go_through_set_arg() -> None:
    """FR-2.2: one builder path for both, asserted with a SINGLE spy on ``set_arg``.

    An accepted completion must not grow a second write path into the builder. The
    spy also records every OTHER mutator on the builder, so a commit that reached
    ``state.args`` directly (bypassing validation) would be caught.

    Mutation target: :meth:`App.commit_input`'s ``self.set_arg(param, text)``.
    Replacing it with a direct ``self.builder.state.args[param] = text`` (the
    plausible "second path" a completion feature invites) makes this RED: the spy
    records nothing, and the path-validation the real ``set_arg`` performs is
    silently bypassed.

    Deliberately NOT claimed as a ``FloatContainer``-root guard. Verified: with the
    root reverted to a plain ``HSplit`` this test still PASSES, because
    prompt_toolkit's ``menu_complete`` applies a completion whether or not the menu
    is DRAWN. The rendering half of FR-2.1 is held by the two tests above; this one
    holds the FR-2.2 single-path claim.
    """

    accepted = _app()
    accepted.runner = _SpyRunner()
    real_set_arg = accepted.builder.set_arg
    other_mutators: List[str] = []
    accepted.builder.clear_arg = mock.Mock(  # type: ignore[method-assign]
        side_effect=lambda name: other_mutators.append(f"clear_arg({name})")
    )

    with mock.patch.object(accepted.builder, "set_arg", side_effect=real_set_arg) as accepted_spy:

        async def script(pipe: Input) -> None:
            await _type(pipe, KEY_ENTER, "e", "kir")
            await _type(pipe, KEY_TAB)  # menu_complete: applies the sole candidate
            await _type(pipe, KEY_ENTER)  # commit the accepted text

        _drive_async(accepted, script)

    assert accepted_spy.call_args_list == [mock.call("--provider", "kiro_cli")]
    assert accepted.builder.state.args["--provider"] == "kiro_cli"
    assert other_mutators == [], f"a second builder mutator was used: {other_mutators}"

    # The typed path reaches set_arg with the identical call shape.
    typed = _app()
    typed.runner = _SpyRunner()
    typed_real = typed.builder.set_arg
    with mock.patch.object(typed.builder, "set_arg", side_effect=typed_real) as typed_spy:
        _run_keys(typed, KEY_ENTER, "e", "kiro_cli", KEY_ENTER)

    assert typed_spy.call_args_list == accepted_spy.call_args_list


# =========================================================================== #
# FR-3 — the profiles screen must be reachable and must degrade, not crash.      #
# =========================================================================== #


def test_p_key_renders_profile_rows_and_escape_returns_to_main() -> None:
    """FR-3.1: ``[p]`` swaps to a profiles screen showing rows from ``ProfilesBrowser``.

    Mutation target: the ``@kb.add("p", filter=navigating)`` binding in
    :meth:`App.build_keybindings` (and :meth:`App.open_profiles`). Removing the
    binding leaves ``screen == "main"`` and the profile names unrendered —
    ``ProfilesBrowser`` goes back to being constructed and referenced nowhere.
    """

    client = mock.MagicMock(spec=ServerClient)
    client.profiles.return_value = [
        ProfileSummary(name="architect"),
        ProfileSummary(name="backend-dev"),
    ]
    app = _app(client=client)
    app.runner = _SpyRunner()

    _run_keys(app, "p")

    assert app.state.screen == "profiles"
    rendered = _rendered(app)
    assert "architect" in rendered
    assert "backend-dev" in rendered

    # Esc returns to main (the existing Esc-goes-back model).
    _run_keys(app, KEY_ESC)
    assert app.state.screen == "main"


def test_p_key_does_nothing_from_the_unreachable_screen() -> None:
    """Profiles are a live read, so ``[p]`` is inert on S-unreachable by design.

    Offering it there would guarantee the unavailable notice, and the
    S-unreachable key map deliberately advertises only keys that work (FR-10.1).
    """

    app = App(
        liveness_probe=lambda _url: False,
        catalog=_catalog(),
        client=mock.MagicMock(spec=ServerClient),
        clipboard=_FakeClipboard(),
    )
    app.runner = _SpyRunner()

    _run_keys(app, "p")

    assert app.state.screen == "unreachable"


def test_a_failed_profiles_read_renders_a_notice_never_a_traceback() -> None:
    """FR-3.2: a server-down read degrades to rendered text on the profiles screen."""

    from cli_agent_orchestrator.tui.server_client import ServerUnavailable

    client = mock.MagicMock(spec=ServerClient)
    client.profiles.side_effect = ServerUnavailable("cao-server down")
    app = _app(client=client)
    app.runner = _SpyRunner()

    _run_keys(app, "p")  # must not raise

    assert app.state.screen == "profiles"
    assert "unavailable" in _rendered(app).lower()


# =========================================================================== #
# FR-4 — [e] cycles through EVERY param and wraps; [x] clears one.               #
# =========================================================================== #


def test_e_key_cycles_through_every_param_and_wraps_to_the_first() -> None:
    """FR-4.1: on a 2-param command, press 3 returns to param 1, prompt naming each.

    Mutation target: :meth:`App.begin_edit`'s
    ``self._edit_index = (self._edit_index + 1) % len(params)`` advance. Reverting
    to the pre-fix ``unset = [...]; target = unset[0] if unset else params[0]``
    pins the target to the first unset param, so presses 2 and 3 both report
    ``--json`` and the wrap assertion REDs. (Both params are left UNSET here on
    purpose — that is exactly the state in which the pre-fix code never moved.)
    """

    app = _app()
    app.runner = _SpyRunner()

    # Open `session status` (2 params) through the model, then press [e] for real.
    _run_keys(app, KEY_DOWN, KEY_ENTER, KEY_ENTER)
    assert app.navigation.active_command is not None
    assert app.navigation.active_command.path == ["session", "status"]
    assert [p.name for p in app.builder.params] == ["--json", "SESSION_NAME"]

    observed: List[str] = []
    for _ in range(3):
        # Esc cancels the overlay without committing, so the next press is a
        # pure cycle step. C-6: the loop is terminated by _run_keys' Ctrl-C.
        _run_keys(app, "e")
        assert app.input_param is not None
        observed.append(app.input_prompt())
        _run_keys(app, KEY_ESC)

    assert observed == ["--json: ", "SESSION_NAME: ", "--json: "], observed


def test_the_first_e_press_still_lands_on_the_first_unset_param() -> None:
    """Preservation constraint: the common "fill in what is missing" flow is unchanged.

    With param 1 already set, the FIRST press must target param 2, not restart at
    param 1 — so the cycle is an addition, not a regression of the prior behaviour.
    """

    app = _app()
    app.runner = _SpyRunner()
    _run_keys(app, KEY_DOWN, KEY_ENTER, KEY_ENTER)
    app.set_arg("--json", "on")

    _run_keys(app, "e")

    assert app.input_prompt() == "SESSION_NAME: "


def test_opening_a_different_command_restarts_the_edit_cycle() -> None:
    """Edge: the cycle index is an offset into THIS command's params.

    Mutation target: :meth:`App._reset_edit_cycle_if_command_changed`'s
    ``self._edit_index = None`` (replacing the method body with a bare ``return``
    is a faithful revert — the method still loads and every caller still calls it).
    A stale index then carries into an unrelated command.

    The fixture is chosen so the stale index is OBSERVABLE: cycle to index 1 on the
    2-param ``session status``, then open the 3-param ``workflow``. A fresh cycle
    targets ``--one``; a stale index-1 advances to ``(1 + 1) % 3 == 2`` → ``--three``.
    A 1-param second command would NOT distinguish them, because ``% len(params)``
    collapses every index to 0 — verified, and the reason this test uses three.
    """

    app = _app()
    app.runner = _SpyRunner()

    # session status: press [e] twice so the cycle sits at index 1.
    _run_keys(app, KEY_DOWN, KEY_ENTER, KEY_ENTER)
    _run_keys(app, "e")
    _run_keys(app, KEY_ESC)
    _run_keys(app, "e")
    _run_keys(app, KEY_ESC)
    assert app.edit_target() == "SESSION_NAME", "precondition: the cycle is at index 1"

    # Back out to the group list (Esc resets the highlight to row 0) and open the
    # 3-param `workflow` leaf, which is row 2.
    _run_keys(app, KEY_ESC)
    assert app.navigation.visible_names() == ["launch", "session", "workflow"]
    _run_keys(app, KEY_DOWN, KEY_DOWN, KEY_ENTER)
    assert app.navigation.active_command is not None
    assert app.navigation.active_command.path == ["workflow"]

    _run_keys(app, "e")

    assert (
        app.input_prompt() == "--one: "
    ), "the [e] cycle inherited a stale index from the previous command"


def test_x_key_clears_the_targeted_param_and_removes_it_from_the_preview() -> None:
    """FR-4.2: set two params, cycle to the first, ``[x]`` it ⇒ ``(unset)`` + gone.

    Mutation target: the ``@kb.add("x", filter=navigating)`` binding and
    :meth:`App.clear_current_arg`'s ``self.builder.clear_arg(target)`` call —
    ``CommandBuilder.clear_arg`` had ZERO call sites before this, so a
    mis-entered argument could only be overwritten, never removed.
    """

    app = _app()
    app.runner = _SpyRunner()
    _run_keys(app, KEY_DOWN, KEY_ENTER, KEY_ENTER)

    # Set both params through the real keyboard path.
    _run_keys(app, "e", "on", KEY_ENTER)
    _run_keys(app, "e", "my-session", KEY_ENTER)
    assert app.preview_text() == "cao session status --json my-session"

    # Cycle the target back to param 1 (--json), then clear it.
    _run_keys(app, "e")
    _run_keys(app, KEY_ESC)
    assert app.edit_target() == "--json"

    _run_keys(app, "x")

    assert app.builder.state.args.get("--json") is None
    assert "--json" not in app.preview_text()
    assert app.preview_text() == "cao session status my-session"
    assert "--json: (unset)" in app._build_text()
    assert "cleared --json" in _rendered(app)


def test_x_key_is_inert_with_no_command_open() -> None:
    """Edge: ``[x]`` with nothing open returns None and mutates nothing."""

    app = _app()
    app.runner = _SpyRunner()

    _run_keys(app, "x")

    assert app.builder.state.args == {}
    assert app.status_notice is None


def test_x_and_p_are_typed_as_TEXT_while_the_overlay_is_open() -> None:
    """Both new keys are bound under ``navigating`` ONLY (C-6's whole premise).

    If ``[x]``/``[p]`` were bound unfiltered, a value containing them could never
    be typed — and the headless loop would exit mid-edit.
    """

    app = _app()
    app.runner = _SpyRunner()
    _run_keys(app, KEY_ENTER)  # open `launch`

    _run_keys(app, "e", "xpx", KEY_ENTER)

    assert app.builder.state.args.get("--provider") == "xpx"
    assert app.state.screen == "main", "[p] inside the overlay must not swap screens"


# =========================================================================== #
# FR-5 — [c] reaches a real clipboard, with a visible notice either way, and      #
# writes NOTHING to the owned terminal on the live-app path.                      #
# =========================================================================== #


def test_copy_key_places_the_exact_preview_on_the_clipboard_with_a_notice() -> None:
    """FR-5.1 / FR-5.3: the App's clipboard receives the byte-exact preview.

    Note this deliberately uses the REAL :class:`CommandRunner` (no spy) so the
    text travels the production route ``[c]`` → ``copy_current`` →
    ``runner.copy`` → ``get_app_or_none().clipboard.set_text``.

    Mutation target: the ``clipboard=self._clipboard`` kwarg in
    :meth:`App._build_application`. Removing it restores prompt_toolkit's default
    ``InMemoryClipboard``, the double never sees the text, and this REDs — which
    is exactly the reported defect (``[c]`` copied into a process-local buffer
    discarded on exit).
    """

    clipboard = _FakeClipboard()
    app = _app(clipboard=clipboard)

    _run_keys(app, KEY_ENTER, "c")

    expected = "cao launch"
    assert clipboard.texts == [expected]
    assert app.last_copied == expected
    assert app.status_notice == "copied to clipboard"
    assert "status: copied to clipboard" in _rendered(app)


def test_copy_notice_is_rendered_not_merely_stored() -> None:
    """FR-5.3: the confirmation must reach the SCREEN, not just model state.

    Mutation target: :meth:`App._build_text`'s
    ``notice_lines = [f"status: {self.status_notice}", ""] if self.status_notice else []``
    prepend, and the two ``notice_lines + [...]`` splices that consume it. Deleting
    the prepend leaves ``status_notice`` set but never drawn, so the operator gets
    no feedback at all — which is the reported defect (there was previously NO
    feedback on either outcome).

    Deliberately NOT claimed as a guard on the ``self._apply_screen()`` call inside
    the ``@kb.add("c")`` handler. Verified: with that call removed the notice STILL
    reaches a rendered frame — prompt_toolkit repaints after every keypress anyway
    (measured: 1 of 2 rendered frames carry the notice, identically with and
    without the explicit repaint). Claiming it would be a false proof; the explicit
    ``_apply_screen()`` is belt-and-braces, not the load-bearing line.

    The assertion is made on the frames prompt_toolkit ACTUALLY requested during the
    run, not on a post-hoc re-render, so a notice that appeared only after the loop
    finished would not satisfy it.
    """

    app = _app()
    frames: List[str] = []
    original_build_text = app._build_text

    def recording_build_text() -> str:
        text = original_build_text()
        frames.append(text)
        return text

    with mock.patch.object(app, "_build_text", side_effect=recording_build_text):
        _run_keys(app, KEY_ENTER, "c")

    assert frames, "the build panel was never rendered — the observation proves nothing"
    assert any(
        "status: copied to clipboard" in frame for frame in frames
    ), f"the notice never reached a rendered frame: {frames}"


def test_a_raising_clipboard_yields_a_fallback_notice_and_zero_bytes_on_the_streams(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """FR-5.2 ⇄ FR-11.1: on the live-app path a clipboard failure writes NOTHING.

    The resolution recorded in the requirements: ``print(text, file=sys.stdout)``
    lands on top of the interface the UI is drawing, so it is removed from the
    live-app path and replaced by the in-UI notice. The non-live fallback survives
    (pinned by ``test_runner.py``'s no-app and broken-stdout tests).

    Mutation target: ``runner.py``'s ``if app is not None: return False`` guard.
    Deleting it lets the ``print(text, file=sys.stdout)`` below it run on the
    live-app path, and the zero-bytes assertion REDs.
    """

    clipboard = _FakeClipboard(raises=True)
    app = _app(clipboard=clipboard)

    capsys.readouterr()  # discard anything the harness emitted before this point
    _run_keys(app, KEY_ENTER, "c")  # must not raise
    captured = capsys.readouterr()

    assert captured.out == "", f"wrote to stdout on the live-app path: {captured.out!r}"
    assert captured.err == "", f"wrote to stderr on the live-app path: {captured.err!r}"
    assert clipboard.texts == []
    assert app.status_notice is not None
    assert "clipboard unavailable" in app.status_notice
    assert "clipboard unavailable" in _rendered(app)


def test_copy_current_returns_the_text_and_never_raises_when_the_clipboard_dies() -> None:
    """FR-5.2: the failure path returns normally — no exception escapes ``[c]``."""

    app = _app(clipboard=_FakeClipboard(raises=True))
    app.runner = CommandRunner()

    with mock.patch(
        "cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=app.application
    ):
        result = app.copy_current()

    assert result == app.copy_text()
    assert app.status_notice is not None and "clipboard unavailable" in app.status_notice


# =========================================================================== #
# FR-10 — every key-map variant fits 80 columns and advertises only live keys.    #
# =========================================================================== #


@pytest.mark.parametrize("screen", sorted(views.KEY_MAPS))
def test_every_key_map_variant_fits_the_eighty_column_budget(screen: str) -> None:
    """FR-10.2: the line lives in a ``wrap_lines=False`` Window — overflow is silent.

    Mutation target: any variant in ``views.KEY_MAPS``. Restoring the single
    116-char ``KEY_MAP_HINT`` for the main screen REDs immediately (116 > 80), and
    the operator silently loses the truncated keys.
    """

    hint = views.KEY_MAPS[screen]
    assert len(hint) <= views.KEY_MAP_MAX_WIDTH, f"{screen}: {len(hint)} chars — {hint!r}"


def test_the_editing_variant_also_fits_and_wins_over_the_screen_variant() -> None:
    """FR-10.1: while the overlay owns the keyboard, the navigation keys are a lie."""

    editing = views.key_map_hint("main", editing=True)
    assert editing == views.KEY_MAP_EDITING
    assert len(editing) <= views.KEY_MAP_MAX_WIDTH
    assert "Esc: cancel" in editing
    # The navigation-only keys are NOT advertised while editing.
    for suppressed in ("p:prof", "x:clr", "c:copy", "r:retry"):
        assert suppressed not in editing


def test_the_main_variant_advertises_every_key_bound_in_navigating_mode() -> None:
    """FR-10.1: abbreviated, but nothing is dropped — all 11 keys are named."""

    hint = views.key_map_hint("main")
    for key in ("arw", "Tab", "Ent", "Esc", "c:", "e:", "x:", "p:", "/:", "r:", "q:"):
        assert key in hint, f"the main key map does not advertise {key!r}: {hint!r}"


def test_the_fatal_variant_advertises_only_keys_bound_on_that_screen() -> None:
    """FR-10.1: the reported defect — the fatal screen offered keys that do nothing.

    Mutation target: ``views.KEY_MAP_CATALOG_FATAL``. Restoring the shared
    ``KEY_MAP_HINT`` there REDs, because `cao` is not runnable so nothing can be
    navigated, built, copied or retried.
    """

    hint = views.key_map_hint("catalog_fatal")
    assert "[q] quit" in hint
    for dead in ("copy", "edit", "search", "retry", "arrows", "Tab", "Enter", "find", "prof"):
        assert dead not in hint, f"the fatal key map advertises a dead affordance: {dead!r}"


def test_the_rendered_footer_uses_the_context_variant_for_each_screen() -> None:
    """The variants must reach the SCREEN, not merely exist as constants."""

    fatal = views.build_catalog_fatal_view("`cao` executable not found")
    unreachable = views.build_unreachable_view(views.ScreenState(reachable=False))

    def flatten(layout: object) -> str:
        lines = []
        for window in layout.find_all_windows():  # type: ignore[attr-defined]
            content = window.content.create_content(200, 40)
            for row in range(min(content.line_count, 40)):
                lines.append("".join(f[1] for f in content.get_line(row)))
        return "\n".join(lines)

    assert views.KEY_MAP_CATALOG_FATAL in flatten(fatal)
    assert views.KEY_MAP_UNREACHABLE in flatten(unreachable)


def test_the_main_screen_footer_swaps_to_the_editing_variant_while_the_overlay_is_open() -> None:
    """FR-10.1: the swap must happen LIVE, at repaint, not only in ``key_map_hint``.

    Mutation target: ``views.build_layout``'s
    ``FormattedTextControl(text=lambda: key_map_hint("main", editing=keymap_filter()))``.
    Reverting it to a static ``FormattedTextControl(text=KEY_MAP_MAIN)`` keeps the
    pure function correct while the SCREEN never changes — the overlay would keep
    advertising navigation keys that its own ``editing`` filter suppresses. The
    constants-only tests above cannot catch that, so this reads the live footer.
    """

    app = _app()
    app.runner = _SpyRunner()
    footers: List[str] = []

    # Capture the footer at each repaint while the overlay is open. The keymap
    # Window is the only one whose text is exactly a KEY_MAPS value.
    def capture() -> None:
        for window in app.application.layout.find_all_windows():
            if isinstance(window.content, BufferControl):
                continue
            content = window.content.create_content(200, 100)
            for row in range(min(content.line_count, 3)):
                line = "".join(f[1] for f in content.get_line(row)).rstrip()
                if line in set(views.KEY_MAPS.values()):
                    footers.append(line)

    # Closed overlay → the main variant.
    _run_keys(app, KEY_ENTER)
    capture()
    assert (
        views.KEY_MAP_MAIN in footers
    ), f"the closed-overlay footer was not the main variant: {footers}"

    # Open the overlay and leave it open (Ctrl-C terminates — C-6), then read again.
    footers.clear()
    _run_keys(app, "e")
    assert app.input_active is True
    capture()
    assert views.KEY_MAP_EDITING in footers, f"the footer did not swap while editing: {footers}"
    assert views.KEY_MAP_MAIN not in footers


def test_an_unknown_screen_falls_back_to_the_main_variant_not_to_nothing() -> None:
    """Edge: an unrecognised screen name must not render an empty footer."""

    assert views.key_map_hint("no-such-screen") == views.KEY_MAP_MAIN


# =========================================================================== #
# FR-11 — a spawn failure must not write into the terminal the TUI owns.          #
#                                                                                #
# The writer is ``logging.lastResort``, NOT a configured handler: the             #
# ``cli_agent_orchestrator.tui`` logger tree is genuinely handler-less under a     #
# live `cao tui` (verified: ``handlers == []`` at ...tui.runner, ...tui,           #
# cli_agent_orchestrator and root), so Python falls back to a ``_StderrHandler``   #
# writing straight to the stream the UI has taken over.                           #
#                                                                                #
# ``capsys`` is NOT a valid probe here: pytest's own ``LogCaptureHandler`` gives    #
# the tree a handler, so ``lastResort`` never fires under pytest and a             #
# capsys-based no-output assertion passes on unmodified `9a1581b` — vacuous. The   #
# probe below temporarily SWAPS ``logging.lastResort`` for a StreamHandler over a  #
# StringIO, which is the only faithful stand-in for the live process's stderr.     #
# =========================================================================== #


def _last_resort_output(*, quiet: bool) -> str:
    """Capture what ``logging.lastResort`` would write for a `cao` spawn failure.

    Args:
        quiet: When ``True`` the failure is provoked INSIDE
            :meth:`App._quiet_tui_logging` — the context the run loop enters.
            When ``False`` it is provoked outside it, reproducing the pre-fix
            behaviour of a live ``cao tui``.

    Returns:
        Everything the ``lastResort`` stream received.
    """

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setLevel(logging.WARNING)  # matches _StderrHandler's level
    original = logging.lastResort
    logging.lastResort = handler  # type: ignore[assignment]
    # Detach the ROOT handlers for the duration of the probe. pytest's logging
    # plugin installs a ``LogCaptureHandler`` there for every test, which gives the
    # tree a handler and so stops ``lastResort`` from ever firing — this is exactly
    # the trap that makes a ``capsys``-based FR-11 assertion vacuous. Removing them
    # reproduces the real ``cao tui`` process, where the tree is handler-less.
    root = logging.getLogger()
    root_handlers = list(root.handlers)
    root.handlers = []
    try:

        def fake_run_in_terminal(func: Callable[[], Any]) -> Any:
            func()  # simulate prompt_toolkit running the scheduled task
            return mock.Mock()

        runner = CommandRunner()
        with (
            mock.patch(
                "cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=mock.Mock()
            ),
            mock.patch(
                "cli_agent_orchestrator.tui.runner.run_in_terminal",
                side_effect=fake_run_in_terminal,
            ),
            mock.patch(
                "cli_agent_orchestrator.tui.runner.subprocess.run",
                side_effect=FileNotFoundError("cao"),
            ),
        ):
            if quiet:
                # Entered directly on an un-__init__'d instance so the probe needs
                # no event loop; App.run() enters this same context manager.
                app = App.__new__(App)
                with app._quiet_tui_logging():
                    runner.run_in_app(["cao", "agent", "start"])
            else:
                runner.run_in_app(["cao", "agent", "start"])
    finally:
        root.handlers = root_handlers
        logging.lastResort = original  # type: ignore[assignment]
    return buffer.getvalue()


def test_the_tui_logger_tree_really_is_handler_less() -> None:
    """The premise the FR-11 guard rests on, asserted rather than assumed.

    If some import ever attaches a handler to this tree, ``lastResort`` stops
    firing and the guard below would pass for the wrong reason. This test makes
    that silent change loud. (``caplog``/``capsys`` are deliberately NOT used in
    this file's FR-11 tests — pytest's LogCaptureHandler is attached at the ROOT
    handler list only while a capturing fixture is active, and this assertion is
    about the *module-level* logger objects.)
    """

    for name in (
        "cli_agent_orchestrator.tui.runner",
        "cli_agent_orchestrator.tui",
        "cli_agent_orchestrator",
    ):
        assert logging.getLogger(name).handlers == [], f"{name} unexpectedly has a handler"


def test_a_spawn_failure_writes_nothing_to_the_last_resort_stream_while_the_ui_runs() -> None:
    """FR-11.1 / FR-11.2: the stream write is suppressed inside the UI's lifetime.

    Mutation target: :meth:`App._quiet_tui_logging`'s
    ``tui_logger.addHandler(handler)``. Reverting the method's body to a bare
    ``yield`` (a faithful revert — the method still loads and the run loop still
    enters it) makes this RED with the pre-fix bytes
    ``'failed to launch `cao agent start`: cao\\n'`` on the stream.

    The control assertion below the fix assertion is what makes this
    non-vacuous: it shows the probe DOES observe the write when the guard is not
    in force, so a green result means suppression, not a blind probe.
    """

    unguarded = _last_resort_output(quiet=False)
    guarded = _last_resort_output(quiet=True)

    # Control: the probe genuinely observes the pre-fix write.
    assert (
        "failed to launch" in unguarded
    ), "the probe saw nothing even WITHOUT the guard — it cannot prove suppression"
    # The fix: nothing reaches the stream the full-screen UI owns.
    assert guarded == "", f"the TUI wrote to the owned terminal: {guarded!r}"


def test_the_log_record_is_still_emitted_only_the_stream_write_is_suppressed() -> None:
    """FR-11.2 explicitly: removing the RECORD is not the remedy (BR-7 reconciliation).

    ``test_runner.py:188`` asserts via ``caplog`` that the failure IS logged at
    ERROR, and that assertion is retained. A ``StreamHandler`` attached to the
    ``cli_agent_orchestrator.tui`` logger still receives the record even inside
    ``_quiet_tui_logging`` — proving the ``NullHandler`` suppresses only the
    ``lastResort`` fallback, never the record itself.
    """

    buffer = io.StringIO()
    observer = logging.StreamHandler(buffer)
    observer.setLevel(logging.DEBUG)
    tui_logger = logging.getLogger("cli_agent_orchestrator.tui")
    tui_logger.addHandler(observer)
    try:

        def fake_run_in_terminal(func: Callable[[], Any]) -> Any:
            func()
            return mock.Mock()

        runner = CommandRunner()
        app = App.__new__(App)
        with (
            mock.patch(
                "cli_agent_orchestrator.tui.runner.get_app_or_none", return_value=mock.Mock()
            ),
            mock.patch(
                "cli_agent_orchestrator.tui.runner.run_in_terminal",
                side_effect=fake_run_in_terminal,
            ),
            mock.patch(
                "cli_agent_orchestrator.tui.runner.subprocess.run",
                side_effect=FileNotFoundError("cao"),
            ),
            app._quiet_tui_logging(),
        ):
            runner.run_in_app(["cao", "agent", "start"])
    finally:
        tui_logger.removeHandler(observer)

    assert "failed to launch" in buffer.getvalue()


def test_quiet_tui_logging_removes_its_handler_on_exit_including_on_error() -> None:
    """Importing / running the TUI must never permanently mute a host process."""

    tui_logger = logging.getLogger("cli_agent_orchestrator.tui")
    before = list(tui_logger.handlers)

    app = App.__new__(App)
    with app._quiet_tui_logging():
        assert len(tui_logger.handlers) == len(before) + 1
    assert tui_logger.handlers == before

    with pytest.raises(RuntimeError):
        with app._quiet_tui_logging():
            raise RuntimeError("boom")
    assert tui_logger.handlers == before, "the handler leaked after an exception"


def test_the_run_loop_actually_enters_the_quiet_context() -> None:
    """The guard is worthless if ``run()`` never enters it.

    Mutation target: the ``with self._quiet_tui_logging():`` wrapper around
    ``self.application.run()`` in :meth:`App.run`. Removing it leaves the guard
    defined but unused, and this REDs.
    """

    app = _app()
    app.runner = _SpyRunner()
    tui_logger = logging.getLogger("cli_agent_orchestrator.tui")
    baseline = len(tui_logger.handlers)
    observed: List[int] = []

    # Observe from INSIDE the loop, via a text provider prompt_toolkit calls at
    # every repaint. Patching the ``Application`` object instead would not work:
    # ``run(input=..., output=...)`` REBUILDS ``self.application`` before entering
    # the loop, discarding any patch applied to the pre-existing instance.
    original_nav_text = app._nav_text

    def observing_nav_text() -> str:
        observed.append(len(tui_logger.handlers))
        return original_nav_text()

    with mock.patch.object(app, "_nav_text", side_effect=observing_nav_text):
        _run_keys(app)

    assert observed, "the render path never ran — the observation proves nothing"
    assert all(
        count == baseline + 1 for count in observed
    ), f"the run loop was not inside _quiet_tui_logging: {observed} vs baseline {baseline}"
    # And the handler is gone once the loop has returned.
    assert len(tui_logger.handlers) == baseline


# =========================================================================== #
# Preservation constraints (C-4, C-5) — pinned so a later edit cannot undo them. #
# =========================================================================== #


def test_the_escape_bindings_keep_eager_true() -> None:
    """C-5: bare Esc must act immediately, not wait out the escape-sequence timeout.

    FR-1's arrow changes touch the same handler neighbourhood, so this is pinned
    explicitly rather than trusted.
    """

    eager_escapes = [
        binding
        for binding in _app().build_keybindings().bindings
        if [str(getattr(key, "value", key)) for key in binding.keys] == ["escape"]
    ]
    assert eager_escapes, "no Esc binding found at all"
    assert all(binding.eager() for binding in eager_escapes), "an Esc binding lost eager=True"


def test_the_preview_stays_byte_identical_to_the_copied_text_and_the_run_argv() -> None:
    """The FR-3.1 invariant survives the [e]-cycle and [x]-clear additions.

    Computes the expectation through an independent real :class:`CommandBuilder`
    so it is not a hand-massaged literal.
    """

    app = _app()
    spy = _SpyRunner()
    app.runner = spy

    _run_keys(app, KEY_DOWN, KEY_ENTER, KEY_ENTER)
    _run_keys(app, "e", "on", KEY_ENTER)
    _run_keys(app, "e", "my-session", KEY_ENTER)

    reference = CommandBuilder()
    reference.select(["session", "status"], params=_SESSION_STATUS_PARAMS)
    reference.set_arg("--json", "on")
    reference.set_arg("SESSION_NAME", "my-session")
    expected = reference.preview_string()

    assert app.preview_text() == expected
    assert app.copy_current() == expected
    app.run_current()
    assert spy.ran_argv == list(reference.preview_argv())


# =========================================================================== #
# §12a review — the shared status line must be TRANSIENT and VISIBLE wherever    #
# it can be set (FR-5.3 / OQ-2).                                                 #
# =========================================================================== #


def _unreachable_app(**kwargs: Any) -> App:
    """An App that starts on S-unreachable, with every external seam doubled."""

    kwargs.setdefault("catalog", _catalog())
    kwargs.setdefault("client", mock.MagicMock(spec=ServerClient))
    kwargs.setdefault("clipboard", _FakeClipboard())
    return App(liveness_probe=lambda _url: False, **kwargs)


def test_the_copy_notice_is_rendered_on_the_unreachable_screen() -> None:
    """FR-5.3 on S-unreachable, where ``[c]`` is the FIRST advertised action.

    ``KEY_MAP_UNREACHABLE`` leads with ``[c] copy start command``, yet the notice
    was spliced only into :meth:`App._build_text` — a provider
    :func:`views.build_layout` alone consumes. So a copy here set
    ``status_notice`` and the operator saw nothing at all on the one screen where
    ``[c]`` is the primary affordance.

    Mutation target: ``views.build_unreachable_view``'s ``alert_body()`` notice
    prefix (equivalently, the ``notice_text=self._status_notice_text`` argument
    :meth:`App._select_layout` now passes). Reverting the body to a bare
    ``FormattedTextControl(text=unreachable_text(), ...)`` keeps ``status_notice``
    set while the rendered screen carries no confirmation, and this REDs.
    """

    clipboard = _FakeClipboard()
    app = _unreachable_app(clipboard=clipboard)

    _run_keys(app, "c")

    assert app.state.screen == "unreachable"
    assert clipboard.texts == [views.SERVER_START_COMMAND]
    assert app.status_notice == "copied to clipboard"
    rendered = _rendered(app)
    assert "status: copied to clipboard" in rendered, rendered
    # The screen's own copy is still there — the notice is prefixed, not a swap.
    assert views.SERVER_START_COMMAND in rendered


def test_the_clipboard_failure_notice_is_rendered_on_the_unreachable_screen() -> None:
    """The fallback half of the same guard: a dead clipboard must be VISIBLE here.

    Same mutation target as above. On S-unreachable the in-UI notice is the only
    channel (nothing may be written to the terminal the UI owns), so an invisible
    fallback means the operator believes the start command was copied when it was
    not.
    """

    app = _unreachable_app(clipboard=_FakeClipboard(raises=True))

    _run_keys(app, "c")  # must not raise

    assert app.status_notice is not None and "clipboard unavailable" in app.status_notice
    assert "clipboard unavailable" in _rendered(app)


def test_the_clear_arg_notice_is_rendered_on_the_profiles_screen() -> None:
    """FR-5.3 on S-profiles: ``[x]`` fires there, so its notice must be drawable.

    ``[x]`` is filtered on ``navigating`` (the input overlay), NOT on the screen,
    so it fires on S-profiles and sets ``cleared <param>``. Before this the notice
    had no region on that screen to appear in.

    Mutation target: ``views.build_profiles_view``'s ``detail_body()`` notice
    prefix (equivalently the ``notice_text=self._status_notice_text`` argument in
    :meth:`App._select_layout`). Reverting the detail Window to
    ``text=lambda: detail_provider()`` leaves the notice set but undrawn, and this
    REDs.
    """

    client = mock.MagicMock(spec=ServerClient)
    client.profiles.return_value = [ProfileSummary(name="architect")]
    app = _app(client=client)
    app.runner = _SpyRunner()

    # Open `launch` (a leaf with one param) so [x] has a target, then go to profiles.
    _run_keys(app, KEY_ENTER)
    _run_keys(app, "p")
    assert app.state.screen == "profiles"

    _run_keys(app, "x")

    assert app.status_notice == "cleared --provider"
    assert "status: cleared --provider" in _rendered(app)


def test_the_status_notice_expires_on_the_next_selection_move() -> None:
    """OQ-2: the shared line is TRANSIENT — an arrow key must expire it.

    The reported defect: copy on ``launch``, then navigate away, and a stale
    ``status: …`` sat beside an unrelated command's preview indefinitely.
    ``catalog_notice`` is already cleared at the top of :meth:`App.activate`; the
    status line was not cleared anywhere.

    Mutation target: the ``self._clear_status_notice()`` call at the top of
    :meth:`App.move_selection`. Deleting it leaves the notice both set AND rendered
    after the move, and this REDs on both assertions.
    """

    app = _app()
    app.runner = _SpyRunner()

    _run_keys(app, KEY_ENTER, "c")
    assert app.status_notice == "copied to clipboard"

    _run_keys(app, KEY_DOWN)

    assert app.status_notice is None, "the notice survived a selection move"
    assert "status:" not in _rendered(app)


def test_the_status_notice_expires_on_the_next_activation() -> None:
    """OQ-2: ``activate()`` clears it too — the exact sequence the reviewer ran.

    Copy on ``launch``, ``back()``, move, activate: the stale notice must be gone
    by the time the new command's preview is drawn.

    Mutation target: the ``self._clear_status_notice()`` call in
    :meth:`App.activate`, beside the existing ``self.catalog_notice = None``.
    Deleting it (while keeping ``move_selection``'s clear) still REDs, because this
    test re-sets the notice AFTER the last move and only then activates.
    """

    app = _app()
    app.runner = _SpyRunner()

    _run_keys(app, KEY_DOWN)  # highlight `session`
    _run_keys(app, "c")  # notice set with no move afterwards
    assert app.status_notice == "copied to clipboard"

    _run_keys(app, KEY_ENTER)  # drill into `session`

    assert app.status_notice is None, "the notice survived an activation"
    assert "status:" not in _rendered(app)
