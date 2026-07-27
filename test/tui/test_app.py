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
import subprocess
from typing import List, Optional, Sequence
from unittest import mock

import requests
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.output import DummyOutput

from cli_agent_orchestrator.tui import main
from cli_agent_orchestrator.tui.app import (
    EXIT_CATALOG_FATAL,
    EXIT_OK,
    EXIT_SIGINT,
    App,
)
from cli_agent_orchestrator.tui.command_builder import CommandBuilder
from cli_agent_orchestrator.tui.command_catalog import (
    CatalogError,
    Command,
    CommandCatalog,
    CommandGroup,
    Param,
)
from cli_agent_orchestrator.tui.completion import ArgCompleter
from cli_agent_orchestrator.tui.navigation import NavigationModel
from cli_agent_orchestrator.tui.profiles_view import ProfilesBrowser
from cli_agent_orchestrator.tui.provider_preflight import ProviderPreflight
from cli_agent_orchestrator.tui.runner import CommandRunner
from cli_agent_orchestrator.tui.server_client import (
    ProfileSummary,
    ProviderStatus,
    ServerClient,
)
from cli_agent_orchestrator.tui.views import (
    SERVER_START_COMMAND,
    ScreenState,
    build_catalog_fatal_view,
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


# =========================================================================== #
# Composition-level tests (P1-1). These are the point of the re-entry: the     #
# shell shipped 15/15 green but NOTHING tested the composition. Every test     #
# below fails if the App stops wiring its collaborators together.              #
# =========================================================================== #


# -- catalog fixtures (faithful to what U2's parser produces) ---------------

_GROUPS = [
    CommandGroup("launch", "Launch a session"),
    CommandGroup("session", "Manage sessions"),
    CommandGroup("workflow", "Run and manage workflows"),
    CommandGroup("memory", "Store and recall memory"),
]

_SESSION_COMMANDS = [
    Command("status", "Session status", ["session", "status"]),
    Command("list", "List sessions", ["session", "list"]),
]

# ``session status`` exposes a boolean ``--json`` option and a required
# ``SESSION_NAME`` positional — enough to exercise option + positional ordering.
_SESSION_STATUS_PARAMS = [
    Param("--json", "option", required=False, takes_value=False, choices=None, help="JSON output"),
    Param("SESSION_NAME", "argument", required=True, takes_value=True, choices=None, help=""),
]

# ``launch`` is a LEAF top-level command carrying a ``--agents`` option.
_LAUNCH_PARAMS = [
    Param("--agents", "option", required=False, takes_value=True, choices=None, help="Profiles"),
]

_COMMANDS_BY_GROUP = {
    "session": _SESSION_COMMANDS,
    "launch": [],  # leaf top-level command (no subcommands)
    "workflow": [],
    "memory": [],
}

_PARAMS_BY_PATH = {
    ("session", "status"): _SESSION_STATUS_PARAMS,
    ("launch",): _LAUNCH_PARAMS,
}


def _fake_catalog() -> mock.MagicMock:
    """A catalog double returning the fixtures above (no ``cao`` shell-out)."""

    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.groups.return_value = _GROUPS
    catalog.commands.side_effect = lambda group: list(_COMMANDS_BY_GROUP.get(group, []))
    catalog.params.side_effect = lambda path: list(_PARAMS_BY_PATH.get(tuple(path), []))
    return catalog


class _SpyRunner(CommandRunner):
    """A CommandRunner that records what it was asked to run / copy (never shells out)."""

    def __init__(self) -> None:
        self.ran_argv: Optional[List[str]] = None
        self.copied: List[str] = []

    def run_in_app(self, argv: Sequence[str]) -> None:  # type: ignore[override]
        self.ran_argv = list(argv)

    def copy(self, text: str) -> None:  # type: ignore[override]
        self.copied.append(text)


def _select_group(app: App, name: str) -> None:
    """Move the nav highlight onto a named top-level group, then Enter (drill)."""

    index = [g.name for g in app.navigation.visible_groups()].index(name)
    app.navigation.move(index - app.navigation.selected_index)
    app.activate()


def _select_command(app: App, name: str) -> None:
    """Move the nav highlight onto a named command in the open group, then Enter."""

    index = [c.name for c in app.navigation.visible_commands()].index(name)
    app.navigation.move(index - app.navigation.selected_index)
    app.activate()


# -- (a) the App composes all eight collaborators ---------------------------


def test_app_composes_all_eight_collaborators() -> None:
    """The App wires all eight collaborators, sharing ONE builder and ONE client."""

    catalog = _fake_catalog()
    client = mock.MagicMock(spec=ServerClient)
    app = App(liveness_probe=lambda _url: True, catalog=catalog, client=client)

    # Injected doubles are used verbatim.
    assert app.catalog is catalog
    assert app.client is client

    # The internally-constructed collaborators are the real types.
    assert isinstance(app.builder, CommandBuilder)
    assert isinstance(app.navigation, NavigationModel)
    assert isinstance(app.completer, ArgCompleter)
    assert isinstance(app.runner, CommandRunner)
    assert isinstance(app.preflight, ProviderPreflight)
    assert isinstance(app.profiles_browser, ProfilesBrowser)

    # ONE builder instance is shared across the collaborators that hold one, so
    # navigation/preview/launch all mutate the same command state.
    assert app.navigation.builder is app.builder
    assert app.profiles_browser.builder is app.builder


# -- (b) THE composition test: select → arg → exact preview → copy → run ----


def test_build_preview_copy_run_are_byte_identical_through_the_app() -> None:
    """select → set arg → EXACT preview → copy that exact string → run that exact argv.

    This is the test that pins the whole P1-1 wiring: driving the App's own
    ``activate`` / ``set_arg`` / ``copy_current`` / ``run_current`` handlers must
    yield a preview byte-identical to the copied text and to the argv the runner
    is handed. If any seam regresses (empty preview, copy_text returns "", runner
    not fed the preview argv), this fails.
    """

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())
    spy = _SpyRunner()
    app.runner = spy

    # Drive the App path: drill into ``session``, open ``status``.
    _select_group(app, "session")
    assert app.navigation.level == "commands"
    _select_command(app, "status")
    assert app.navigation.active_command is not None
    assert app.navigation.active_command.path == ["session", "status"]

    # Set the flag and the positional through the App's edit-commit handler.
    app.set_arg("--json", "on")
    app.set_arg("SESSION_NAME", "my-session")

    # The exact preview, computed independently via a real CommandBuilder so the
    # expectation is not hand-fragile — then asserted byte-for-byte.
    reference = CommandBuilder()
    reference.select(["session", "status"], params=_SESSION_STATUS_PARAMS)
    reference.set_arg("--json", "on")
    reference.set_arg("SESSION_NAME", "my-session")
    expected = reference.preview_string()
    assert expected == "cao session status --json my-session"  # anchor
    assert app.preview_text() == expected  # byte-identical through the App

    expected_argv = list(app.builder.preview_argv())
    assert expected_argv == ["cao", "session", "status", "--json", "my-session"]

    # Copy places that EXACT string on the clipboard and records it.
    copied = app.copy_current()
    assert copied == expected
    assert app.last_copied == expected
    assert spy.copied == [expected]

    # Run hands the runner the SAME argv the preview shows (FR-3.1).
    app.run_current()
    assert spy.ran_argv == expected_argv


# -- (c) App-level launch flow (the issue's primary workflow) ---------------


def test_app_launch_leaf_flow_builds_launch_with_agents() -> None:
    """Reaching ``launch`` as a leaf and setting ``--agents`` builds the exact cmd.

    ``cao launch`` is a leaf top-level command — previously unreachable (P1-2).
    Through the App: select the ``launch`` row, Enter opens it (no group to
    drill), set ``--agents``, and the preview is ``cao launch --agents <p>``.
    """

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())

    _select_group(app, "launch")  # a leaf: Enter opens it directly
    assert app.navigation.active_command is not None
    assert app.navigation.active_command.path == ["launch"]

    app.set_arg("--agents", "my-profile")

    assert app.preview_text() == "cao launch --agents my-profile"
    assert app.builder.preview_argv() == ["cao", "launch", "--agents", "my-profile"]


# -- (d) profiles surface + provider pre-flight reachable from the App -------


def test_profiles_and_preflight_are_reachable_from_the_app() -> None:
    """The App exposes the profiles browser and provider pre-flight, both live.

    Both read through the ONE shared client; a mocked client returning known
    profiles/providers must surface verbatim through the App's collaborators and
    its footer pre-flight text (rendered as TEXT yes/no — NFR-6).
    """

    client = mock.MagicMock(spec=ServerClient)
    client.profiles.return_value = [
        ProfileSummary(name="architect"),
        ProfileSummary(name="developer"),
    ]
    client.providers.return_value = [
        ProviderStatus(name="claude_code", binary="claude", installed=True),
        ProviderStatus(name="kiro_cli", binary="kiro", installed=False),
    ]

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog(), client=client)

    # Profiles surface.
    assert app.profiles_browser.load() == client.profiles.return_value
    assert app.profiles_browser.names() == ["architect", "developer"]

    # Provider pre-flight rows.
    rows = app.preflight.rows()
    assert [(r.name, r.installed_text) for r in rows] == [
        ("claude_code", "yes"),
        ("kiro_cli", "no"),
    ]

    # And the App renders them as the footer pre-flight TEXT line.
    footer = app._preflight_text()
    assert "claude_code: yes" in footer
    assert "kiro_cli: no" in footer


def test_preflight_text_degrades_when_server_unreachable() -> None:
    """FR-9.1: a server-down pre-flight read degrades to a text note, never a crash."""

    from cli_agent_orchestrator.tui.server_client import ServerUnavailable

    client = mock.MagicMock(spec=ServerClient)
    client.providers.side_effect = ServerUnavailable("cao-server down")

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog(), client=client)

    text = app._preflight_text()
    assert "not reachable" in text.lower()


# -- (e) the FAILING real probe — the test that would have caught the defect --


def test_default_probe_starts_unreachable_when_health_get_raises() -> None:
    """Production default probe: a failing ``GET /health`` starts on S-unreachable.

    Exercises the REAL ``probe_server_reachable`` (no injected probe) with
    ``requests.get`` monkeypatched to raise a connection error. The shipped stub
    returned ``True`` unconditionally, so the shell always opened on the main
    screen even with no server — this test would have caught that defect.
    """

    with mock.patch(
        "cli_agent_orchestrator.tui.server_client.requests.get",
        side_effect=requests.exceptions.ConnectionError("connection refused"),
    ):
        app = App(catalog=_fake_catalog())

    assert app.state.reachable is False
    assert app.state.screen == "unreachable"


def test_default_probe_starts_main_when_health_get_succeeds() -> None:
    """Production default probe: a healthy ``GET /health`` starts on S-main.

    The reachable counterpart to the failing-probe test — proves the real probe
    honours a successful response (not a stub that ignores the server).
    """

    healthy = mock.MagicMock()
    healthy.status_code = 200
    healthy.raise_for_status.return_value = None
    healthy.json.return_value = {"status": "ok", "service": "cao-server"}

    with mock.patch(
        "cli_agent_orchestrator.tui.server_client.requests.get",
        return_value=healthy,
    ):
        app = App(catalog=_fake_catalog())

    assert app.state.reachable is True
    assert app.state.screen == "main"


# -- handler behaviour: search, edit errors, run-on-reopen, completer path --


def test_apply_search_routes_to_navigation_filter() -> None:
    """The ``[/]`` handler routes the search string into the client-side filter.

    Filtering is a pure client-side substring match; applying "work" through the
    App must narrow the visible group list to ``workflow`` with no catalog
    re-fetch beyond the cached groups.
    """

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())

    app.apply_search("work")
    assert app.navigation.visible_names() == ["workflow"]

    app.begin_search()  # resets the filter to empty
    assert app.navigation.filter_text == ""
    assert app.navigation.visible_names() == ["launch", "session", "workflow", "memory"]


def test_set_arg_surfaces_path_error_inline_without_crashing() -> None:
    """A rejected path arg is caught and stored as an inline error (FR-8.1).

    Driving ``set_arg`` for a directory-style flag with a bogus path must not
    raise into the shell: the handler returns ``None`` and records the
    validator's message in ``arg_error`` (rendered next to the field).
    """

    # A command whose only param is a directory-style flag routed through U5.
    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.params.side_effect = lambda path: [
        Param(
            "--working-directory",
            "option",
            required=False,
            takes_value=True,
            choices=None,
            help="",
        )
    ]
    app = App(liveness_probe=lambda _url: True, catalog=catalog)
    app.builder.select(["session", "status"])

    stored = app.set_arg("--working-directory", "/no/such/dir/really-not-here-42")

    assert stored is None
    assert app.arg_error is not None
    assert app.arg_error != ""
    # The rejected value was NOT recorded on the builder.
    assert "--working-directory" not in app.builder.state.args

    # A subsequent valid edit clears the inline error.
    app.set_arg("--working-directory", ".")
    assert app.arg_error is None


def test_enter_on_already_open_command_runs_it() -> None:
    """At command level, Enter on the already-open command triggers the run path.

    First Enter opens the command; a second Enter on the same highlighted row
    runs it with the exact preview argv (FR-3.1) — not a second open.
    """

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())
    spy = _SpyRunner()
    app.runner = spy

    _select_group(app, "session")
    _select_command(app, "status")  # first Enter opens
    assert app.navigation.active_command is not None
    assert spy.ran_argv is None

    app.activate()  # second Enter on the same row → run
    assert spy.ran_argv == app.builder.preview_argv()
    assert spy.ran_argv == ["cao", "session", "status"]


def test_enter_on_already_open_top_level_leaf_runs_it() -> None:
    """At group level, Enter on an already-open leaf top-level command runs it."""

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())
    spy = _SpyRunner()
    app.runner = spy

    _select_group(app, "launch")  # leaf: first Enter opens it
    assert app.navigation.active_command is not None
    assert app.navigation.active_command.path == ["launch"]
    assert spy.ran_argv is None

    app.activate()  # second Enter on the launch row → run
    assert spy.ran_argv == ["cao", "launch"]


def test_completer_path_tracks_the_open_command() -> None:
    """The completer's path callable re-reads the currently open command path.

    The App wires ``ArgCompleter`` to ``_current_command_path`` so completion
    follows the focused command. Before any selection it is empty; after opening
    ``session status`` it is that path.
    """

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())

    assert app._current_command_path() == []

    _select_group(app, "session")
    _select_command(app, "status")
    assert app._current_command_path() == ["session", "status"]


# -- live text providers (views invoke these at render time) ----------------


def test_nav_text_marks_the_selected_row() -> None:
    """The nav-text provider renders the visible list with the selection marked."""

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())

    text = app._nav_text()
    lines = text.splitlines()
    assert lines[0] == "> launch"  # index 0 selected by default
    assert "  session" in lines

    app.navigation.move(1)
    assert app._nav_text().splitlines()[1] == "> session"


def test_nav_text_empty_filter_shows_guiding_copy() -> None:
    """A filter that matches nothing shows guiding copy, not a crash/blank."""

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())
    app.apply_search("zzz-nope")

    assert app._nav_text() == "(no matches — press [/] to change the filter)"


def test_build_text_lists_open_command_params_and_error() -> None:
    """The build-text provider renders the open command, its params, and errors."""

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())

    # Before any selection: the first-open guiding copy.
    from cli_agent_orchestrator.tui.views import MAIN_BODY_HINT

    assert app._build_text() == MAIN_BODY_HINT

    _select_group(app, "session")
    _select_command(app, "status")
    app.set_arg("SESSION_NAME", "my-session")

    text = app._build_text()
    assert "command: cao session status" in text
    assert "--json" in text
    assert "SESSION_NAME [required]: my-session" in text


def test_preview_text_bare_before_selection_then_exact_after() -> None:
    """The preview provider is the bare executable before selection, exact after.

    A fresh :class:`CommandBuilder` renders only ``argv[0]`` (``"cao"``) until a
    command path is selected — there is no half-formed command to run. After
    selecting ``launch`` and setting ``--agents`` it is the exact command
    (FR-3.1). (The plan's "empty until selected" wording is the *no-command*
    state; U3's builder represents that as the bare executable, asserted here as
    the real behaviour rather than a hand-massaged expectation.)
    """

    app = App(liveness_probe=lambda _url: True, catalog=_fake_catalog())
    assert app.preview_text() == "cao"

    _select_group(app, "launch")
    app.set_arg("--agents", "architect")
    assert app.preview_text() == "cao launch --agents architect"


# =========================================================================== #
# CatalogError robustness on the Enter keystroke path (Q1=A / Q2=A).           #
# activate() drills a group (select_top_level → commands()) or opens a command #
# (open_command → builder.select → params()); both shell out to               #
# `cao <name> --help` and can raise CatalogError. An unhandled CatalogError    #
# previously propagated out of the key handler and KILLED the event loop —     #
# these tests pin the graceful handling and its fatal-vs-transient split.      #
# =========================================================================== #


def _timeout_catalog_error(argv: List[str]) -> CatalogError:
    """A CatalogError whose cause is a --help timeout (a TRANSIENT failure)."""

    exc = CatalogError(argv, message=f"`{' '.join(argv)}` timed out after 10.0s")
    exc.__cause__ = subprocess.TimeoutExpired(cmd=" ".join(argv), timeout=10.0)
    return exc


def _missing_binary_catalog_error(argv: List[str]) -> CatalogError:
    """A CatalogError whose cause is a missing `cao` binary (a FATAL failure)."""

    exc = CatalogError(argv, message="`cao` executable not found")
    exc.__cause__ = FileNotFoundError("cao")
    return exc


def _nonzero_exit_catalog_error(argv: List[str]) -> CatalogError:
    """A CatalogError from a NON-ZERO exit (no ``__cause__``) — a TRANSIENT failure.

    The catalog's third raise site (``CommandCatalog._help_text``) constructs a
    bare ``CatalogError(argv, stderr=...)`` with no chained cause. This is the
    variant with ``__cause__ is None``, which must classify as transient (not
    fatal) — exercised explicitly so the ``else`` branch is pinned on its own,
    not only via the timeout case.
    """

    return CatalogError(argv, stderr="Error: no such command 'bogus'\n")


def _catalog_raising_on_commands(exc: CatalogError) -> mock.MagicMock:
    """A catalog whose groups() works but commands() raises (groups-level path)."""

    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.groups.return_value = [CommandGroup("workflow", "")]

    def _raise(_group: str) -> List[Command]:
        raise exc

    catalog.commands.side_effect = _raise
    return catalog


def test_transient_catalog_error_on_group_select_stays_on_main_with_notice() -> None:
    """A --help TIMEOUT while drilling a group → inline notice, stay on S-main (Q1=A).

    Driven through the composed Enter path (``activate`` at the groups level).
    Before the fix this raised out of the key handler and killed the loop; now
    it is a recoverable notice and the shell keeps running.
    """

    catalog = _catalog_raising_on_commands(_timeout_catalog_error(["cao", "workflow", "--help"]))
    app = App(liveness_probe=lambda _url: True, catalog=catalog)

    # Enter on the highlighted 'workflow' group — commands() raises a timeout.
    app.activate()

    assert app.state.screen == "main", "a transient catalog timeout must NOT leave S-main"
    assert app.fatal_message is None, "a timeout is not the fatal missing-binary case"
    assert app.catalog_notice is not None, "the operator must see a notice"
    assert "timed out" in app.catalog_notice
    # The notice is surfaced in the rendered nav text (TEXT, not colour — NFR-6).
    assert "notice:" in app._nav_text()


def test_nonzero_exit_catalog_error_is_transient_not_fatal() -> None:
    """A NON-ZERO-exit CatalogError (no ``__cause__``) → notice, stay on S-main.

    The third catalog raise cause (a ``cao <group> --help`` that ran but exited
    non-zero) carries no ``__cause__``. It must classify as transient like the
    timeout case — NOT fall through to the fatal missing-binary screen. This
    pins the ``else`` branch on the ``__cause__ is None`` variant directly, so an
    inverted or over-narrow classification is caught even if the timeout test
    were removed.
    """

    catalog = _catalog_raising_on_commands(
        _nonzero_exit_catalog_error(["cao", "workflow", "--help"])
    )
    app = App(liveness_probe=lambda _url: True, catalog=catalog)

    app.activate()

    assert app.state.screen == "main", "a non-zero exit is recoverable, not fatal"
    assert app.fatal_message is None, "no __cause__ must NOT be read as missing-binary"
    assert app.catalog_notice is not None


def test_transient_catalog_error_notice_clears_on_next_good_selection() -> None:
    """A stale timeout notice is cleared the moment a fresh selection succeeds."""

    # First: a catalog that times out; then swap in a healthy one and re-select.
    catalog = _catalog_raising_on_commands(_timeout_catalog_error(["cao", "workflow", "--help"]))
    app = App(liveness_probe=lambda _url: True, catalog=catalog)
    app.activate()
    assert app.catalog_notice is not None

    # Replace the catalog's commands() with a healthy read and re-select.
    app.navigation._catalog.commands.side_effect = lambda group: []  # type: ignore[attr-defined]
    app.activate()
    assert app.catalog_notice is None, "a successful selection clears the stale notice"


def test_fatal_catalog_error_on_group_select_swaps_to_fatal_screen() -> None:
    """A MISSING `cao` binary while drilling a group → fatal screen (Q1=A).

    The catalog reports the executable itself is gone (CatalogError caused by
    FileNotFoundError): the whole TUI cannot introspect anything, so this is
    fatal — distinct from the transient notice — and the App records the fatal
    message for the S-catalog-fatal screen.
    """

    catalog = _catalog_raising_on_commands(
        _missing_binary_catalog_error(["cao", "workflow", "--help"])
    )
    app = App(liveness_probe=lambda _url: True, catalog=catalog)

    app.activate()

    assert app.state.screen == "catalog_fatal", "a missing cao binary is fatal"
    assert app.fatal_message is not None
    assert app.catalog_notice is None, "the fatal path is not a transient notice"


def test_fatal_catalog_error_on_command_open_swaps_to_fatal_screen() -> None:
    """A MISSING `cao` binary while opening a command (params() path) is also fatal.

    Covers the commands-level branch: drilling ``session`` succeeds, but opening
    ``status`` calls ``builder.select`` → ``params()``, which raises. The same
    fatal classification must apply on this second shell-out path, not only the
    group-drill path.
    """

    catalog = mock.MagicMock(spec=CommandCatalog)
    catalog.groups.return_value = _GROUPS
    catalog.commands.side_effect = lambda group: list(_COMMANDS_BY_GROUP.get(group, []))

    def _params_raise(_path: Sequence[str]) -> List[Param]:
        raise _missing_binary_catalog_error(["cao", "session", "status", "--help"])

    catalog.params.side_effect = _params_raise
    app = App(liveness_probe=lambda _url: True, catalog=catalog)

    _select_group(app, "session")  # drill OK (commands() does not raise)
    assert app.state.screen == "main"
    _select_command(app, "status")  # open → params() raises FileNotFoundError-caused error

    assert app.state.screen == "catalog_fatal"
    assert app.fatal_message is not None


def test_enter_binding_exits_nonzero_on_fatal_catalog_error() -> None:
    """The Enter key binding exits the loop with EXIT_CATALOG_FATAL when cao is missing.

    End-to-end through the real key binding and a headless prompt_toolkit loop:
    pressing Enter on a group whose read fails with a missing-binary CatalogError
    swaps to the fatal screen AND exits non-zero (U1 design: fatal → exit
    non-zero), rather than hanging or crashing.
    """

    catalog = _catalog_raising_on_commands(
        _missing_binary_catalog_error(["cao", "workflow", "--help"])
    )
    app = App(liveness_probe=lambda _url: True, catalog=catalog)

    with create_pipe_input() as pipe:
        pipe.send_text("\r")  # Enter
        code = app.run(input=pipe, output=DummyOutput())

    assert app.state.screen == "catalog_fatal"
    assert code == EXIT_CATALOG_FATAL


def test_build_catalog_fatal_view_renders_message_and_quit() -> None:
    """The fatal view is a Layout showing the cao-not-found copy and [q] quit."""

    layout = build_catalog_fatal_view("`cao` executable not found")
    assert isinstance(layout, Layout)
