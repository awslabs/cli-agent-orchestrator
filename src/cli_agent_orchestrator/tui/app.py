"""``cao tui`` application shell — the composition root.

The prompt_toolkit full-screen ``Application`` that composes the thin-shell
front door: it builds the root ``Layout`` (see :mod:`.views`), installs the
global :class:`~prompt_toolkit.key_binding.KeyBindings`, wires the eight
collaborator objects (catalog, builder, navigation, completer, runner, the
read-only server client, provider pre-flight, and the profiles browser) into one
working flow, and, at startup, runs a real liveness probe that selects the main
screen or the server-unreachable screen.

``main()`` is the ``cao tui`` entry callable (RD-b=A). U5 wires it into
``cli/main.py`` as the ``cao tui`` subcommand.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only stdlib, ``prompt_toolkit``, ``requests``,
``cli_agent_orchestrator.constants``, ``cli_agent_orchestrator.utils.path_validation``
and the ``tui`` package's own modules may be imported here. The liveness probe
goes through :class:`~cli_agent_orchestrator.tui.server_client.ServerClient`
(U4) over ``requests`` — never a direct services/clients import.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.clipboard import Clipboard
from prompt_toolkit.clipboard.pyperclip import PyperclipClipboard
from prompt_toolkit.filters import Condition
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output.base import Output

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.tui import views
from cli_agent_orchestrator.tui.command_builder import CommandBuilder
from cli_agent_orchestrator.tui.command_catalog import CatalogError, CommandCatalog
from cli_agent_orchestrator.tui.completion import ArgCompleter
from cli_agent_orchestrator.tui.navigation import (
    LEVEL_GROUPS,
    NavigationModel,
)
from cli_agent_orchestrator.tui.path_input import PathInputError
from cli_agent_orchestrator.tui.profiles_view import ProfilesBrowser
from cli_agent_orchestrator.tui.provider_preflight import ProviderPreflight
from cli_agent_orchestrator.tui.runner import CommandRunner
from cli_agent_orchestrator.tui.server_client import (
    ServerAuthRequired,
    ServerClient,
    ServerClientError,
    ServerUnavailable,
)
from cli_agent_orchestrator.tui.views import ScreenState

# Exit codes returned by ``run()`` / ``main()``.
EXIT_OK = 0
EXIT_SIGINT = 130
# The ``cao`` binary is missing / not runnable — the catalog cannot introspect
# any command, so the TUI cannot function. It shows the fatal "cao not found"
# screen and exits non-zero (U1 business-logic-model: CatalogError → fatal).
EXIT_CATALOG_FATAL = 1

# Type of the injectable liveness probe: given the API base URL, return whether
# cao-server is reachable.
LivenessProbe = Callable[[str], bool]

# The logger tree every ``tui`` module logs under (each uses ``__name__``, so all
# of them are descendants of this). Named explicitly rather than derived from
# ``__name__`` so it stays correct if this module moves.
_TUI_LOGGER_NAME = "cli_agent_orchestrator.tui"


def probe_server_reachable(base_url: str = API_BASE_URL) -> bool:
    """Startup liveness probe (W-1.2): a real bounded ``GET /health``.

    Performs a single, bounded ``ServerClient(base_url).health()`` call over
    ``requests`` (which stays within the thin-shell import allow-list). The
    server answering a well-formed ``/health`` payload means reachable; any
    :class:`~cli_agent_orchestrator.tui.server_client.ServerUnavailable`
    (connection refused, timeout, non-2xx) means unreachable. Callers may inject
    their own probe via :class:`App` (the seam is preserved for tests).

    Args:
        base_url: The cao-server base URL to probe. Defaults to the
            constants-derived ``API_BASE_URL``.

    Returns:
        ``True`` when cao-server answered ``/health``; ``False`` when it was
        unreachable. Any other unexpected failure is caught one level up by
        :meth:`App._run_probe` and also treated as unreachable — the shell never
        crashes on a probe failure (S-unreachable is a normal, recoverable
        state, not a fatal error).
    """

    try:
        ServerClient(base_url).health()
        return True
    except ServerUnavailable:
        return False


class App:
    """The ``cao tui`` prompt_toolkit application shell + composition root.

    Owns the frame (layout + key map + screen selection) *and* the wiring of the
    eight collaborators into one build -> preview -> copy/run flow. Domain
    behaviour lives in the collaborators; the App routes keys to small,
    testable handler methods that delegate to them.
    """

    def __init__(
        self,
        liveness_probe: Optional[LivenessProbe] = None,
        *,
        catalog: Optional[CommandCatalog] = None,
        client: Optional[ServerClient] = None,
        clipboard: Optional[Clipboard] = None,
    ) -> None:
        """Compose the collaborators, run the startup probe, and build the app.

        Args:
            liveness_probe: Optional override for the startup/retry probe. When
                omitted, the module-level :func:`probe_server_reachable` (a real
                ``GET /health``) is used. Existing tests inject their own probe;
                that seam is preserved unchanged.
            catalog: Optional command catalog (U2). Defaults to a real
                :class:`CommandCatalog`. Injectable so tests drive a double
                without shelling out to ``cao``.
            client: Optional read-only server client (U4). Defaults to a real
                :class:`ServerClient`. A single instance is shared across the
                pre-flight and profiles collaborators so live reads share one
                seam.
            clipboard: Optional prompt_toolkit clipboard. Defaults to a
                :class:`~prompt_toolkit.clipboard.pyperclip.PyperclipClipboard`
                so ``[c]`` reaches the real OS clipboard (FR-5.1). Injectable so a
                test can pass a double (including one that raises) without
                touching the machine's clipboard.
        """

        self._probe: LivenessProbe = liveness_probe or probe_server_reachable
        # FR-5.1: the real OS clipboard, not prompt_toolkit's process-local default.
        self._clipboard: Clipboard = clipboard if clipboard is not None else PyperclipClipboard()

        # -- compose the eight collaborators (all intra-tui) ----------------- #
        # ONE builder + ONE client are shared so preview/run/launch see the
        # same state and reads share one HTTP seam.
        self.catalog: CommandCatalog = catalog if catalog is not None else CommandCatalog()
        self.builder: CommandBuilder = CommandBuilder(self.catalog)
        self.navigation: NavigationModel = NavigationModel(self.catalog, self.builder)
        self.completer: ArgCompleter = ArgCompleter(self.catalog, self._current_command_path)
        self.runner: CommandRunner = CommandRunner()
        self.client: ServerClient = client if client is not None else ServerClient()
        self.preflight: ProviderPreflight = ProviderPreflight(self.client)
        self.profiles_browser: ProfilesBrowser = ProfilesBrowser(self.client, builder=self.builder)

        self.state = ScreenState()
        # Text most recently placed on the clipboard by ``[c]`` — surfaced for
        # tests and for later status feedback.
        self.last_copied: Optional[str] = None
        # Inline error for the last argument edit (rendered in the build panel);
        # ``None`` when the last edit succeeded (or none has happened).
        self.arg_error: Optional[str] = None
        # Transient catalog notice (rendered on S-main): set when a ``cao <name>
        # --help`` read fails recoverably (timeout / non-zero exit) on a
        # selection keystroke. ``None`` clears it. A *fatal* catalog failure
        # (the ``cao`` binary is missing / not runnable) is NOT a notice — it
        # swaps to the S-catalog-fatal screen (:attr:`fatal_message`) instead.
        self.catalog_notice: Optional[str] = None
        # Fatal "cao not found" message (S-catalog-fatal); ``None`` unless the
        # catalog reported the ``cao`` executable itself is missing/not runnable.
        self.fatal_message: Optional[str] = None
        # Whether the S-catalog-fatal layout has been swapped in by the
        # render-time guard (so it swaps once, then exits on the next frame).
        self._fatal_screen_applied = False
        # Transient one-line status notice rendered in the build panel (OQ-2: one
        # shared line). This is the *in-UI* channel for feedback that must never
        # be written to the terminal the full-screen UI owns — the ``[c]`` copy
        # confirmation and its clipboard-unavailable fallback (FR-5.3), and any
        # other transient acknowledgement. ``None`` == nothing to show.
        self.status_notice: Optional[str] = None
        # The param ``[e]`` will target next, as an index into
        # ``builder.params``. ``None`` means "no cycle started yet", so the first
        # press lands on the first unset param (the pre-existing behaviour) and
        # subsequent presses advance through EVERY param, wrapping (FR-4.1).
        self._edit_index: Optional[int] = None
        # The command path the edit cycle belongs to. Opening a different command
        # must restart the cycle rather than inherit a stale index.
        self._edit_cycle_path: Optional[List[str]] = None

        # -- input overlay (P1-3) ------------------------------------------- #
        # Which capture is active: ``None`` (closed), ``"edit"``, or ``"search"``.
        # ``[e]``/``[/]`` open it; Enter commits, Esc cancels.
        self.input_mode: Optional[str] = None
        # The param an ``"edit"`` capture will commit into on Enter.
        self.input_param: Optional[str] = None
        # The live buffer the overlay reads. ``ArgCompleter`` is attached HERE —
        # this is what makes the completer built above a live UI object rather
        # than an unreachable one. ``on_text_changed`` gives search its
        # incremental narrowing as characters arrive.
        self.input_buffer: Buffer = Buffer(
            completer=self.completer,
            complete_while_typing=True,
            multiline=False,
            on_text_changed=self._on_input_changed,
        )

        # Run the startup liveness probe (W-1) to pick the initial screen.
        self.state.reachable = self._run_probe()
        self.state.screen = "main" if self.state.reachable else "unreachable"

        self.key_bindings = self.build_keybindings()
        self.application: Application = self._build_application()

    # -- collaborator glue --------------------------------------------------

    def _current_command_path(self) -> List[str]:
        """The focused command's argv path, for the completer to re-read on focus.

        Returns the path of the command most recently opened into the builder
        (empty when none is open yet), so :class:`ArgCompleter` offers the right
        command's flags/choices as the selection changes.
        """

        command = self.navigation.active_command
        return list(command.path) if command is not None else []

    # -- probe / screen selection ------------------------------------------

    def _run_probe(self) -> bool:
        """Invoke the liveness probe, treating any failure as unreachable.

        Errors from the probe (network failures from the real HTTP call) must
        never crash the shell — an unreachable server is a normal, recoverable
        state (S-unreachable), not a fatal error.
        """

        try:
            return bool(self._probe(API_BASE_URL))
        except Exception:
            return False

    def _select_layout(self) -> object:
        """Return the layout for the current screen (main vs unreachable).

        For the main screen the App's live text providers are passed to
        :func:`views.build_layout`; ``views`` invokes them at render time and
        imports no models itself (thin-shell boundary preserved).
        """

        if self.state.screen == "catalog_fatal":
            return views.build_catalog_fatal_view(self.fatal_message or "")
        if self.state.screen == "profiles":
            return views.build_profiles_view(
                self.state,
                profiles_text=self._profiles_text,
                detail_text=self._profile_detail_text,
                notice_text=self._status_notice_text,
            )
        if self.state.screen == "unreachable":
            return views.build_unreachable_view(self.state, notice_text=self._status_notice_text)
        return views.build_layout(
            self.state,
            nav_text=self._nav_text,
            build_text=self._build_text,
            preview_text=self.preview_text,
            preflight_text=self._preflight_text,
            input_buffer=self.input_buffer,
            input_prompt=self.input_prompt,
            input_visible=Condition(lambda: self.input_active),
        )

    def _apply_screen(self) -> None:
        """Re-render the active screen onto the running application's layout."""

        self.application.layout = self._select_layout()  # type: ignore[assignment]
        self.application.invalidate()

    def retry(self) -> bool:
        """Re-probe the server and swap to the matching screen (the ``[r]`` key).

        Returns:
            The refreshed reachability result.
        """

        self.state.reachable = self._run_probe()
        self.state.screen = "main" if self.state.reachable else "unreachable"
        return self.state.reachable

    # -- handler methods (behaviour; key bindings are thin adapters) --------

    def activate(self) -> None:
        """Enter: drill a group, open a command, or run an already-open command.

        At the *groups* level: route the highlighted top-level entry through
        :meth:`NavigationModel.select_top_level` — which drills a real group or
        opens a leaf command (P1-2). If the highlighted top-level entry is a leaf
        that is *already* open, Enter runs it.

        At the *commands* level: open the highlighted command into the builder,
        or — if it is already the open command — run it (the run path, W-2 /
        FR-3.1). Running goes through :meth:`run_current` so the previewed argv
        is byte-identical to what executes.

        Both the group-drill (``select_top_level`` → ``commands()``) and the
        command-open (``open_command`` → ``builder.select`` → ``params()``)
        paths shell out to ``cao <name> --help`` and can raise
        :class:`~cli_agent_orchestrator.tui.command_catalog.CatalogError`. That
        is caught here and classified (:meth:`_handle_catalog_error`) so a slow
        or missing ``cao`` never propagates out of the key handler and crashes
        the event loop (construction guardrail; the U1 fatal-vs-recoverable
        split).
        """

        if self.state.screen != "main":
            return
        nav = self.navigation
        # Any prior transient notice is cleared the moment a fresh selection is
        # attempted — a stale "timed out" note must not linger over a good read.
        self.catalog_notice = None
        # The shared status line is transient for the same reason (OQ-2): a copy
        # confirmation must not sit beside an unrelated command's preview.
        self._clear_status_notice()

        try:
            if nav.level == LEVEL_GROUPS:
                groups = nav.visible_groups()
                if not groups:
                    return
                index = min(nav.selected_index, len(groups) - 1)
                name = groups[index].name
                active = nav.active_command
                if active is not None and active.path == [name]:
                    # A top-level leaf already opened → Enter runs it.
                    self.run_current()
                else:
                    nav.select_top_level(name)
                return

            commands = nav.visible_commands()
            if not commands:
                return
            index = min(nav.selected_index, len(commands) - 1)
            command = commands[index]
            active = nav.active_command
            if active is not None and active.path == command.path:
                # Already opened → Enter runs it (the run path).
                self.run_current()
            else:
                nav.open_command(command)
        except CatalogError as exc:
            self._handle_catalog_error(exc)

    def move_selection(self, delta: int) -> None:
        """Move the highlighted row by ``delta``, routing catalog failures (FR-1.1).

        This is the MUST-FIX. :meth:`NavigationModel.move` clamps against the
        visible list, and computing that list reads the catalog — which shells out
        to ``cao --help`` and can raise
        :class:`~cli_agent_orchestrator.tui.command_catalog.CatalogError`. The arrow
        key handlers called ``move()`` **unguarded**, so one transient catalog
        failure turned the next arrow keypress into a raw traceback painted over the
        interface followed by prompt_toolkit's unrecoverable "Press ENTER to
        continue…", and the event loop died.

        Routing through :meth:`_handle_catalog_error` — the same classifier every
        other catalog call site already uses — inherits the fatal-vs-transient split
        and the clean non-zero exit for free. That matters specifically because
        :meth:`_catalog_fatal_guard` is registered on ``after_render`` and the arrow
        handlers raised *before* any render callback ran, so the only route to a
        clean ``EXIT_CATALOG_FATAL`` never fired on this path.

        On the profiles screen the arrows move that list instead; it is served from
        already-loaded summaries and touches no catalog.

        Args:
            delta: Rows to move (``+1`` down, ``-1`` up).
        """

        # A selection move is the other place a stale transient notice must expire
        # (OQ-2), matching what :meth:`activate` does for ``catalog_notice``.
        self._clear_status_notice()
        if self.state.screen == "profiles":
            self.move_profile_selection(delta)
            return
        try:
            self.navigation.move(delta)
        except CatalogError as exc:
            self._handle_catalog_error(exc)

    def _exit_if_catalog_fatal(self, event: KeyPressEvent) -> None:
        """Exit the run loop non-zero when the current screen turned catalog-fatal.

        Mirrors what the Enter binding does. The layout swap itself is left to the
        caller's :meth:`_apply_screen` (and to :meth:`_catalog_fatal_guard`), so the
        operator sees the fatal screen's guidance rather than a bare exit.
        """

        if self.state.screen != "catalog_fatal":
            return
        self._apply_screen()
        event.app.exit(result=EXIT_CATALOG_FATAL)

    def _handle_catalog_error(self, exc: CatalogError) -> None:
        """Classify a selection-path :class:`CatalogError` — fatal vs transient (Q1=A).

        The catalog raises ``CatalogError`` for three causes (see
        ``CommandCatalog._help_text``): a missing/non-runnable ``cao`` executable
        (``__cause__`` is :class:`FileNotFoundError`), a ``--help`` timeout
        (``__cause__`` is :class:`subprocess.TimeoutExpired`), and a non-zero
        exit (no ``__cause__``, ``stderr`` carried). Only the first is *fatal* —
        the ``cao`` binary is gone, so the whole TUI cannot introspect anything;
        it swaps to the S-catalog-fatal screen and the run loop will exit
        non-zero. The other two are *transient* (this one group/command could
        not be read now): they set a non-fatal inline notice and stay on S-main
        so the operator can pick another group or retry.

        Args:
            exc: The catalog error raised on the selection keystroke path.
        """

        if isinstance(exc.__cause__, FileNotFoundError):
            # Fatal: the cao binary itself is missing / not runnable.
            self.fatal_message = str(exc)
            self.state.screen = "catalog_fatal"
        else:
            # Transient (timeout / non-zero exit): surface a notice, stay on main.
            self.catalog_notice = str(exc)

    def set_arg(self, param_name: str, value: str) -> Optional[str]:
        """Commit an argument edit (the ``[e]`` edit), surfacing errors inline.

        Delegates to :meth:`CommandBuilder.set_arg`; a rejected path arg raises
        :class:`~cli_agent_orchestrator.tui.path_input.PathInputError`, which is
        caught and stored in :attr:`arg_error` (rendered next to the field)
        rather than crashing the shell (FR-8.1 / construction guardrail).

        Args:
            param_name: The param/flag to set (e.g. ``--working-directory``).
            value: The raw user-entered value.

        Returns:
            The stored value on success, or ``None`` when the value was rejected
            (with :attr:`arg_error` set to the validator's message).
        """

        try:
            stored = self.builder.set_arg(param_name, value)
            self.arg_error = None
            return stored
        except PathInputError as exc:
            self.arg_error = str(exc)
            return None

    def _reset_edit_cycle_if_command_changed(self) -> None:
        """Restart the ``[e]`` cycle when a different command has been opened.

        The cycle index is an offset into *this* command's params; carrying it over
        to a different command would target an unrelated (or out-of-range) param.
        """

        path = self._current_command_path()
        if path != self._edit_cycle_path:
            self._edit_cycle_path = path
            self._edit_index = None

    def edit_target(self) -> Optional[str]:
        """The param name ``[e]`` currently targets, or ``None`` when there is none.

        Exposed so the clear key (``[x]``) and the overlay prompt agree with the
        cycle on a single source of truth.
        """

        self._reset_edit_cycle_if_command_changed()
        params = self.builder.params
        if not params:
            return None
        if self._edit_index is None:
            return None
        return params[self._edit_index % len(params)].name

    def begin_edit(self) -> None:
        """Open the argument-edit overlay, CYCLING the target (the ``[e]`` key, FR-4.1).

        Clears any stale field error, advances the edit target, and opens the live
        input row. The typed value is committed to :meth:`set_arg` on Enter and
        discarded on Esc (:meth:`cancel_input`).

        Targeting (FR-4.1): the FIRST press lands on the first *unset* param (or
        param 1 when all are set) — the previous behaviour, preserved so the common
        "fill in what is missing" flow is unchanged. Every subsequent press advances
        one param and **wraps** at the end, so the cycle reaches EVERY param, set or
        unset. Before this, the target was recomputed from scratch on each press and
        so was pinned to the first unset param: an operator could not correct a value
        they had already entered. The overlay prompt (:meth:`input_prompt`) names the
        cycled target, because it reads :attr:`input_param`.
        """

        self.arg_error = None
        self._reset_edit_cycle_if_command_changed()
        params = self.builder.params
        if not params:
            # Nothing to edit (no command open, or a command with no arguments).
            return

        if self._edit_index is None:
            unset = [
                index
                for index, param in enumerate(params)
                if self.builder.state.args.get(param.name) is None
            ]
            self._edit_index = unset[0] if unset else 0
        else:
            # Wrap: the cycle must reach every param, then return to the first.
            self._edit_index = (self._edit_index + 1) % len(params)

        self.input_param = params[self._edit_index].name
        self.input_mode = "edit"
        self.input_buffer.reset()

    def clear_current_arg(self) -> Optional[str]:
        """Clear the currently-targeted param's value (the ``[x]`` key, FR-4.2).

        Routes through :meth:`CommandBuilder.clear_arg` — which had zero call sites
        anywhere in ``src/`` before this, so a mis-entered argument could not be
        removed from the build at all, only overwritten. The cleared param renders as
        ``(unset)`` in the build panel and disappears from the preview/copy string,
        because both are pure functions of the builder state.

        The target is the one ``[e]`` is cycled to; when no cycle has started yet it
        is the first param, so ``[x]`` is useful without pressing ``[e]`` first.

        Returns:
            The name of the param cleared, or ``None`` when there was nothing to
            clear (no command open, or a command with no params).
        """

        self.arg_error = None
        target = self.edit_target()
        if target is None:
            params = self.builder.params
            if not params:
                return None
            target = params[0].name
        self.builder.clear_arg(target)
        self.status_notice = f"cleared {target}"
        return target

    def _focus_input(self, event: KeyPressEvent) -> None:
        """Move keyboard focus to the overlay's buffer once it is open.

        The overlay row only exists in the layout while it is visible, so focus is
        moved AFTER :meth:`_apply_screen` has rebuilt the layout. A focus failure
        must never crash the shell — if the control cannot be focused the overlay
        is closed again so the operator is not stranded in a mode with no input.
        """

        if not self.input_active:
            return
        try:
            event.app.layout.focus(self.input_buffer)
        except Exception:
            self._close_input()
            self._apply_screen()

    def commit_input(self) -> None:
        """Commit the overlay's typed text (Enter) and close it.

        In ``"edit"`` mode the text goes through :meth:`set_arg` (so a rejected
        path lands in :attr:`arg_error` rather than raising); in ``"search"`` mode
        it goes through :meth:`apply_search`. Either way the overlay closes and
        focus returns to the nav pane.
        """

        text = self.input_buffer.text
        mode, param = self.input_mode, self.input_param
        self._close_input()
        if mode == "edit" and param is not None:
            self.set_arg(param, text)
        elif mode == "search":
            self.apply_search(text)

    def cancel_input(self) -> None:
        """Dismiss the overlay without applying (Esc while it holds focus).

        A cancelled *search* also restores the unfiltered list, since
        :meth:`begin_search` cleared the filter on open and search narrows
        incrementally as characters arrive.
        """

        mode = self.input_mode
        self._close_input()
        if mode == "search":
            self.apply_search("")

    def _close_input(self) -> None:
        """Reset the overlay to its closed state (shared by commit and cancel)."""

        self.input_mode = None
        self.input_param = None
        self.input_buffer.reset()

    def _on_input_changed(self, _buffer: Buffer) -> None:
        """Narrow the list as search characters arrive (incremental filtering).

        Only ``"search"`` mode filters while typing; an ``"edit"`` value must not
        touch navigation state until it is committed.
        """

        if self.input_mode == "search":
            self.navigation.set_filter(self.input_buffer.text)

    @property
    def input_active(self) -> bool:
        """Whether the input overlay is open (drives its visibility + key filters)."""

        return self.input_mode is not None

    def input_prompt(self) -> str:
        """The overlay's prompt label, rendered as TEXT (NFR-6)."""

        if self.input_mode == "search":
            return "search: "
        if self.input_mode == "edit":
            return f"{self.input_param}: "
        return ""

    def apply_search(self, text: str) -> None:
        """Apply a ``[/]`` search string to the navigator's client-side filter.

        Pure delegation to :meth:`NavigationModel.set_filter` (a substring match
        over already-fetched catalog names — never a network round-trip).
        """

        self.navigation.set_filter(text)

    def begin_search(self) -> None:
        """Open the search overlay (the ``[/]`` key).

        Clears any active filter so the full list is shown as the operator begins
        a fresh search, then opens the live input row. Typing narrows the list
        incrementally (:meth:`_on_input_changed`); Enter commits the substring and
        Esc restores the unfiltered list.
        """

        self.apply_search("")
        self.input_param = None
        self.input_mode = "search"
        self.input_buffer.reset()

    def preview_text(self) -> str:
        """The exact ``cao ...`` command preview (empty until one is selected).

        Sourced from :meth:`CommandBuilder.preview_string` — the single source of
        truth the run path also uses, so the preview is byte-identical to what
        runs (FR-3.1).
        """

        return self.builder.preview_string()

    def run_current(self) -> None:
        """Run the built command in-app with the previewed argv (FR-3.1 / FR-4.1).

        Executes ``runner.run_in_app(builder.preview_argv())`` — the *same* argv
        the preview shows — so copy-then-run and run are behaviourally identical
        (the ``cao`` CLI is the mutation seam; no mutating HTTP).
        """

        self.runner.run_in_app(self.builder.preview_argv())

    # -- clipboard ----------------------------------------------------------

    def copy_text(self) -> str:
        """Return the text ``[c]`` copies for the current screen.

        On S-unreachable this is the exact ``cao-server`` start command
        (RD-c=A). On S-main it is the command preview string
        (:meth:`CommandBuilder.preview_string`) — byte-identical to what runs
        (FR-3.1 / FR-3.2).
        """

        if self.state.screen == "unreachable":
            return views.SERVER_START_COMMAND
        return self.builder.preview_string()

    def copy_current(self) -> str:
        """Copy the current screen's text and confirm it in-UI (the ``[c]`` behaviour).

        Places :meth:`copy_text` on the clipboard via :meth:`CommandRunner.copy`
        (which never raises) and records it in :attr:`last_copied`, byte-identical to
        the preview so the copied command is exactly what the preview shows and what
        a run would execute (FR-3.1 / FR-3.2).

        Sets :attr:`status_notice` on BOTH outcomes (FR-5.3) — there was previously
        no feedback at all, so a successful copy and a silently-failed one looked
        identical. On the live-app path the fallback notice is the *only* channel:
        ``copy`` writes nothing to stdout there, because that stream is the terminal
        the UI has taken over (FR-5.2 ⇄ FR-11.1).

        Returns:
            The text placed on the clipboard.
        """

        text = self.copy_text()
        copied = self.runner.copy(text)
        self.last_copied = text
        if copied:
            self.status_notice = "copied to clipboard"
        else:
            self.status_notice = (
                "clipboard unavailable — the command is shown in the preview pane; "
                "select it with your terminal to copy"
            )
        return text

    # -- live text providers (invoked by views at render time) --------------

    def _nav_text(self) -> str:
        """Render the visible command/group list with the selection marked.

        A ``>`` marks the highlighted row (keyboard-only, TEXT marker — never
        colour alone, NFR-6). An empty list (e.g. a filter that matches nothing,
        or a leaf group) shows guiding copy, not an error.

        This provider is invoked by ``views`` at every repaint, and the read it
        performs (``visible_names`` -> ``visible_groups`` -> ``catalog.groups``)
        shells out to ``cao --help`` on the FIRST PAINT — before any keystroke
        reaches the guarded :meth:`activate` path. So the same
        :class:`CatalogError` triage must apply here, or a missing ``cao`` binary
        crashes the draw loop with a raw traceback instead of showing the
        S-catalog-fatal screen (P1-3).
        """

        # A transient catalog notice (a group/command whose `--help` timed out or
        # exited non-zero) is shown above the list as TEXT — the operator can
        # pick another entry or retry; it is not an error state (FR-9.1-style).
        notice_lines = [f"notice: {self.catalog_notice}", ""] if self.catalog_notice else []

        try:
            names = self.navigation.visible_names()
        except CatalogError as exc:
            # Classify exactly as the selection path does: a missing `cao` is
            # fatal (swap to S-catalog-fatal, and the run loop exits non-zero via
            # _catalog_fatal_guard); a timeout / non-zero exit is transient.
            self._handle_catalog_error(exc)
            if self.state.screen == "catalog_fatal":
                # The in-flight frame still needs text; the screen swap and the
                # non-zero exit are driven by the render-time guard.
                return "(cao could not be run)"
            return "\n".join([f"notice: {self.catalog_notice}", "", "(catalog unavailable)"])
        if not names:
            if self.navigation.filter_text:
                return "\n".join(notice_lines + ["(no matches — press [/] to change the filter)"])
            return "\n".join(notice_lines + ["(no commands here)"])
        selected = self.navigation.selected_index
        lines = list(notice_lines)
        for index, name in enumerate(names):
            marker = "> " if index == selected else "  "
            lines.append(f"{marker}{name}")
        return "\n".join(lines)

    def _status_notice_text(self) -> str:
        """The shared transient status line (OQ-2), or ``""`` when there is none.

        The single provider every screen that can *set* a notice renders it through:
        S-main splices it into :meth:`_build_text`, S-unreachable and S-profiles take
        it as ``notice_text=``. Before this it existed only inside ``_build_text``,
        which ``views.build_layout`` alone consumes — so ``[c]`` on S-unreachable
        (that screen's first advertised action) and ``[x]`` on S-profiles both set a
        confirmation no region could draw (FR-5.3).
        """

        return self.status_notice or ""

    def _clear_status_notice(self) -> None:
        """Expire the transient status line (OQ-2: *transient*, not sticky).

        Called from the same places :attr:`catalog_notice` is cleared — a fresh
        selection attempt (:meth:`activate`) and a selection move
        (:meth:`move_selection`). Without this the notice persisted across arbitrary
        later navigation, so a stale ``status: clipboard unavailable …`` sat beside an
        unrelated command's preview forever.
        """

        self.status_notice = None

    def _build_text(self) -> str:
        """Render the build panel: the open command, its params, and any error.

        Until a command is opened this shows the first-open guiding copy. Once a
        command is open it lists each parameter with its entered value (or
        ``(unset)``), any inline arg error (:attr:`arg_error`), and the builder's
        advisory soft warnings — none of which ever block the run/copy path.
        """

        # The shared in-UI status line (OQ-2). It is the ONLY channel for
        # feedback like the copy confirmation: the full-screen UI owns the
        # terminal, so a print()/stderr write would land on top of the interface.
        notice_lines = [f"status: {self.status_notice}", ""] if self.status_notice else []

        command = self.navigation.active_command
        if command is None:
            return "\n".join(notice_lines + [views.MAIN_BODY_HINT])

        lines = notice_lines + [f"command: {' '.join(['cao', *command.path])}", ""]
        params = self.builder.params
        if params:
            lines.append("params:")
            for param in params:
                value = self.builder.state.args.get(param.name)
                shown = value if value is not None else "(unset)"
                required = " [required]" if param.required else ""
                lines.append(f"  {param.name}{required}: {shown}")
        else:
            lines.append("(no arguments — press Enter to run)")

        if self.arg_error:
            lines += ["", f"error: {self.arg_error}"]

        warnings = self.builder.soft_warnings()
        if warnings:
            lines.append("")
            lines += [f"note: {warning}" for warning in warnings]

        return "\n".join(lines)

    def _preflight_text(self) -> str:
        """Render the provider pre-flight footer line as TEXT (NFR-6).

        Sourced solely from ``GET /agents/providers`` via
        :class:`ProviderPreflight`. A server-down or malformed read degrades to a
        text note (never a crash — FR-9.1); command building and copy keep
        working while the server is unreachable.
        """

        try:
            rows = self.preflight.rows()
        except ServerAuthRequired:
            # Narrower than ServerUnavailable (its superclass), so this branch MUST
            # come first: the server IS up, it just wants credentials. Reporting
            # "not reachable" here sent operators after the wrong problem (FR-7.1).
            # Message distinction only — the TUI plumbs no token (OOS-2).
            return (
                "providers: cao-server requires authentication (this TUI cannot authenticate yet)"
            )
        except ServerUnavailable:
            return "providers: server not reachable (reads resume when it is up)"
        except ServerClientError:
            return "providers: unexpected response from cao-server"
        if not rows:
            return "providers: none reported"
        return "providers  " + "   ".join(f"{row.name}: {row.installed_text}" for row in rows)

    # -- profiles screen (FR-3) ---------------------------------------------

    def open_profiles(self) -> None:
        """Swap to the S-profiles screen and load the list (the ``[p]`` key, FR-3.1).

        Makes :class:`ProfilesBrowser` — constructed in the composition root but
        previously referenced nowhere else in ``src/`` — actually reachable from the
        running UI. The read goes through the browser, which already turns a
        server-down or malformed-payload read into its own ``unavailable`` *state*
        rather than an exception (FR-3.2), so no read failure can escape here.
        Because :class:`ServerAuthRequired` subclasses :class:`ServerUnavailable`, a
        401 degrades through that same path instead of rendering a traceback on a
        screen this method makes reachable for the first time (FR-7.2).
        """

        # Only from the main screen: profiles are a live read, so offering them on
        # S-unreachable would guarantee the unavailable notice, and the
        # S-unreachable key map deliberately advertises only keys that work there
        # (FR-10.1). S-catalog-fatal is a terminal state.
        if self.state.screen != "main":
            return
        self.profiles_browser.load()
        self.state.screen = "profiles"

    def close_profiles(self) -> None:
        """Return from S-profiles to the screen the operator came from (Esc).

        Mirrors the existing Esc-goes-back model. Which screen to return to is
        decided by the last probe result, exactly as :meth:`retry` does, so an
        unreachable server does not land the operator on a main screen whose live
        reads cannot work.
        """

        if self.state.screen != "profiles":
            return
        self.state.screen = "main" if self.state.reachable else "unreachable"

    def move_profile_selection(self, delta: int) -> None:
        """Move the S-profiles highlight and refresh the detail pane."""

        self.profiles_browser.move(delta)

    def _profiles_text(self) -> str:
        """Render the profile list with the selection marked (TEXT only, NFR-6).

        A ``>`` marks the highlighted row, matching the nav pane's marker. When a
        read failed the browser is in its ``unavailable`` state and
        :meth:`ProfilesBrowser.list_text` already returns the degradation notice —
        so a server-down, an auth-required 401, or a malformed payload all render
        as a notice here, never a traceback (FR-3.2).
        """

        browser = self.profiles_browser
        if browser.unavailable or browser.is_empty():
            return browser.list_text()
        selected = browser.selected_index
        return "\n".join(
            f"{'> ' if index == selected else '  '}{name}"
            for index, name in enumerate(browser.names())
        )

    def _profile_detail_text(self) -> str:
        """Render the highlighted profile's summary detail without a second read.

        Deliberately sourced from the ALREADY-LOADED list summary rather than
        issuing ``GET /agents/profiles/{name}`` — this provider runs on every
        repaint, and a per-repaint HTTP read is the exact defect FR-6 forbids.
        Falls back to the browser's own copy when the list is unavailable or empty.
        """

        browser = self.profiles_browser
        if browser.unavailable or browser.is_empty():
            return browser.list_text()
        summary = browser.selected_profile()
        if summary is None:
            return "Select a profile to preview its detail."
        capabilities = ", ".join(summary.capabilities) if summary.capabilities else "(none listed)"
        return "\n".join(
            [
                f"profile:     {summary.name}",
                "",
                f"source:      {summary.source or '(unknown)'}",
                f"role:        {summary.role or '(none)'}",
                f"tools:       {capabilities}",
                f"description: {summary.description or '(none)'}",
                f"loadable:    {'yes' if summary.loadable else 'no'}",
            ]
        )

    # -- key bindings -------------------------------------------------------

    def build_keybindings(self) -> KeyBindings:
        """Install the global key map (W-2).

        Bound: Up/Down move the *selection* through :meth:`move_selection`, Esc goes
        *back* (leaving the profiles screen, else up one navigation level), Tab/S-Tab
        (and Left/Right) move pane focus, Enter activates the focused row (drill /
        open / run), ``[c]`` copy, ``[e]`` edit (cycling the target), ``[x]`` clear
        the targeted argument, ``[p]`` open Profiles, ``[/]`` search, ``[r]`` retry,
        ``[q]`` quit, plus Ctrl-C for a clean SIGINT exit. ``[s]`` is deliberately
        NOT bound (RD-e=A: no status pane / no status key).

        ``[p]`` and ``[x]`` are the two keys added here; both were verified free
        against the full bound set, and both are registered under ``navigating``
        ONLY — so while the input overlay holds the keyboard they are ordinary typed
        characters, not commands.

        The selection keys are the P1 fix: they were previously bound to
        prompt_toolkit's ``focus_next``/``focus_previous``, which move the *focus
        ring* between panes and never touch the navigation model — so
        ``move()``/``back()`` had zero production call sites and the highlighted
        row could not be changed by the keyboard at all.

        Every binding is a thin adapter that calls a handler method then
        re-renders. Bindings are scoped by filter so the input overlay owns the
        keys it needs while open (typed characters, Enter to commit, Esc to
        cancel) and the navigation keys do not fire underneath it.
        """

        kb = KeyBindings()

        # The overlay owns the keyboard while it is open; navigation keys are
        # suppressed so a typed value cannot move the selection underneath it.
        editing = Condition(lambda: self.input_active)
        navigating = Condition(lambda: not self.input_active)

        # Pane focus movement (keyboard-only — NFR-6). Tab/S-Tab and the
        # horizontal arrows stay on the focus ring; the VERTICAL arrows now drive
        # the selection instead (see below).
        kb.add("tab", filter=navigating)(focus_next)
        kb.add("s-tab", filter=navigating)(focus_previous)
        kb.add("right", filter=navigating)(focus_next)
        kb.add("left", filter=navigating)(focus_previous)

        @kb.add("down", filter=navigating)
        def _next_row(event: KeyPressEvent) -> None:
            """Move the selection one row down (the P1 key -> model wiring)."""

            self.move_selection(1)
            self._exit_if_catalog_fatal(event)
            self._apply_screen()

        @kb.add("up", filter=navigating)
        def _prev_row(event: KeyPressEvent) -> None:
            """Move the selection one row up (clamped at the top of the list)."""

            self.move_selection(-1)
            self._exit_if_catalog_fatal(event)
            self._apply_screen()

        @kb.add("escape", filter=navigating, eager=True)
        def _back(event: KeyPressEvent) -> None:
            """Go back: leave the profiles screen, or up one navigation level.

            ``eager=True`` so a bare Esc acts immediately instead of waiting to
            see whether it is the start of an escape SEQUENCE (arrow keys arrive
            as ``\\x1b[A``/``\\x1b[B``). Without it, prompt_toolkit holds the bare
            Esc back for its input timeout and ``back()`` feels broken.
            """

            if self.state.screen == "profiles":
                self.close_profiles()
            else:
                self.navigation.back()
            self._apply_screen()

        @kb.add("enter", filter=editing)
        def _commit_input(event: KeyPressEvent) -> None:
            """Commit the overlay's typed text (set_arg / apply_search) and close."""

            self.commit_input()
            self._apply_screen()

        @kb.add("escape", filter=editing, eager=True)
        def _cancel_input(event: KeyPressEvent) -> None:
            """Dismiss the overlay without applying the typed text."""

            self.cancel_input()
            self._apply_screen()

        @kb.add("c-c", filter=editing)
        def _sigint_editing(event: KeyPressEvent) -> None:
            """Ctrl-C still exits cleanly from inside the overlay."""

            event.app.exit(result=EXIT_SIGINT)

        @kb.add("enter", filter=navigating)
        def _activate(event: KeyPressEvent) -> None:
            """Activate the focused row: drill a group, open a command, or run.

            A fatal catalog failure discovered while activating (the ``cao``
            binary is gone) swaps to the S-catalog-fatal screen and exits the
            run loop non-zero — the shell cannot function without ``cao``.
            """

            self.activate()
            if self.state.screen == "catalog_fatal":
                self._apply_screen()
                event.app.exit(result=EXIT_CATALOG_FATAL)
                return
            self._apply_screen()

        @kb.add("c", filter=navigating)
        def _copy(event: KeyPressEvent) -> None:
            """Copy the current preview / start command to the clipboard (FR-3.2).

            Re-renders so the FR-5.3 confirmation (or the clipboard-unavailable
            fallback notice) is actually visible. Without the repaint the notice sits
            in model state until some other key happens to trigger a frame — and on
            the live-app path the in-UI notice is the ONLY channel, because nothing
            may be written to the terminal the UI owns.
            """

            self.copy_current()
            self._apply_screen()

        @kb.add("e", filter=navigating)
        def _edit(event: KeyPressEvent) -> None:
            """Open the argument-edit overlay (commit via set_arg)."""

            self.begin_edit()
            self._apply_screen()
            self._focus_input(event)

        @kb.add("/", filter=navigating)
        def _search(event: KeyPressEvent) -> None:
            """Open the search overlay over the visible group/command names."""

            self.begin_search()
            self._apply_screen()
            self._focus_input(event)

        @kb.add("x", filter=navigating)
        def _clear_arg(event: KeyPressEvent) -> None:
            """Clear the currently-targeted argument (FR-4.2).

            ``[x]`` was verified unbound before this. Registered under
            ``navigating`` only, so a literal ``x`` typed into the overlay is text.
            """

            self.clear_current_arg()
            self._apply_screen()

        @kb.add("p", filter=navigating)
        def _profiles(event: KeyPressEvent) -> None:
            """Open the Profiles screen (FR-3.1).

            ``[p]`` was verified unbound before this. Registered under ``navigating``
            only, so a literal ``p`` typed into the overlay is text.
            """

            self.open_profiles()
            self._apply_screen()

        @kb.add("r", filter=navigating)
        def _retry(event: KeyPressEvent) -> None:
            """Re-probe the server and swap screens (S-unreachable → S-main)."""

            self.retry()
            self._apply_screen()

        @kb.add("q", filter=navigating)
        def _quit(event: KeyPressEvent) -> None:
            """Quit the TUI with a normal exit code."""

            event.app.exit(result=EXIT_OK)

        @kb.add("c-c")
        def _sigint(event: KeyPressEvent) -> None:
            """Ctrl-C: clean exit with the conventional SIGINT code."""

            event.app.exit(result=EXIT_SIGINT)

        return kb

    # -- application / run loop --------------------------------------------

    def _build_application(
        self,
        *,
        input: Optional[Input] = None,
        output: Optional[Output] = None,
    ) -> Application:
        """Construct the full-screen prompt_toolkit Application.

        ``input``/``output`` are injectable so a headless test harness (pipe
        input + ``DummyOutput``) can drive the loop without a real terminal.
        """

        application: Application = Application(
            layout=self._select_layout(),  # type: ignore[arg-type]
            key_bindings=self.key_bindings,
            full_screen=True,
            mouse_support=False,  # keyboard-only (NFR-6)
            # FR-5.1: without this, prompt_toolkit defaults to an
            # ``InMemoryClipboard`` — a process-local buffer discarded on exit, so
            # the advertised ``[c] copy`` reached nothing the operator could paste.
            # ``pyperclip`` is reached only THROUGH this prompt_toolkit class; no
            # module under ``tui/`` imports it, so the thin-shell boundary holds.
            clipboard=self._clipboard,
            input=input,
            output=output,
        )
        # A fatal catalog failure found at PAINT time (not on a keystroke) has no
        # key handler to swap the screen and exit for it, so without this hook the
        # app would sit on the main layout forever. The hook renders the fatal
        # screen and then exits non-zero (see _catalog_fatal_guard).
        application.after_render += self._catalog_fatal_guard
        return application

    def _catalog_fatal_guard(self, _sender: object) -> None:
        """Swap to the S-catalog-fatal screen, then exit the run loop non-zero.

        Registered on ``Application.after_render``. The frame that DISCOVERED the
        failure was already laid out for S-main (the layout was chosen before
        ``_nav_text`` ran), so this swaps in :func:`views.build_catalog_fatal_view`
        first — otherwise the operator would only ever see the terse
        ``"(cao could not be run)"`` in the nav pane and never the fatal screen's
        actual guidance. Mirrors what the Enter-key fatal path does.

        Idempotent on both halves: the screen check keeps it inert on every normal
        frame, ``_screen_applied_for_fatal`` stops the swap from re-entering on the
        frame it schedules, and exiting an already-exiting application is a no-op.
        """

        if self.state.screen != "catalog_fatal":
            return
        if not self._fatal_screen_applied:
            # Swap the layout to the fatal view. Done BEFORE exiting (and in the
            # same callback) so the exit cannot be pre-empted by a keystroke that
            # is already queued — waiting for a second frame would let a pending
            # `[q]` quit with EXIT_OK and lose the fatal code.
            self._fatal_screen_applied = True
            self._apply_screen()
        application = self.application
        if application.is_running and not application.is_done:
            application.exit(result=EXIT_CATALOG_FATAL)

    @contextmanager
    def _quiet_tui_logging(self) -> Iterator[None]:
        """Suppress ``logging.lastResort`` for the TUI tree while the UI runs (FR-11.2).

        The writer this guards against is **not** a configured handler.
        ``setup_logging()`` is called only from ``api/main.py`` — never from
        ``cli/main.py`` and never from here — so under a live ``cao tui`` the
        ``cli_agent_orchestrator.tui`` logger tree has *no* handler at all, and
        Python falls back to :data:`logging.lastResort`: a ``_StderrHandler`` at
        level WARNING that writes the record straight to ``stderr``. That stream is
        the terminal the full-screen UI has taken over, so a ``cao`` spawn failure
        painted its log line on top of the interface.

        Attaching a :class:`logging.NullHandler` gives the tree a handler, which is
        precisely the condition under which ``lastResort`` is skipped. The log
        **record is still emitted** — this suppresses only the stream write. That
        distinction matters: ``test_runner.py`` asserts via ``caplog`` that a spawn
        failure IS logged at ERROR, and that assertion is retained deliberately
        (emitting the record is not the defect; the record reaching the owned
        terminal's stream is).

        The handler is removed on exit — including on an exception — so importing the
        TUI never permanently mutes the package's logging for a host process.
        """

        tui_logger = logging.getLogger(_TUI_LOGGER_NAME)
        handler = logging.NullHandler()
        tui_logger.addHandler(handler)
        try:
            yield
        finally:
            tui_logger.removeHandler(handler)

    def run(
        self,
        *,
        input: Optional[Input] = None,
        output: Optional[Output] = None,
    ) -> int:
        """Enter the event loop until ``[q]`` / Ctrl-C and return an exit code.

        The loop runs inside :meth:`_quiet_tui_logging`, so nothing the TUI logs can
        reach the terminal the UI owns while it owns it (FR-11.1/FR-11.2).

        Args:
            input: Optional prompt_toolkit input (for headless tests).
            output: Optional prompt_toolkit output (for headless tests).

        Returns:
            ``0`` on a normal quit, ``130`` on Ctrl-C, ``0`` for any other
            non-integer exit result.
        """

        if input is not None or output is not None:
            self.application = self._build_application(input=input, output=output)

        with self._quiet_tui_logging():
            result = self.application.run()
        return result if isinstance(result, int) else EXIT_OK


def main() -> int:
    """``cao tui`` entry callable.

    Constructs the :class:`App` (which composes the collaborators, runs the
    startup liveness probe, and picks the initial screen) and enters its run
    loop. U5 wires this into ``cli/main.py`` as the ``cao tui`` subcommand
    (RD-b=A).

    Returns:
        The process exit code.
    """

    return App().run()
