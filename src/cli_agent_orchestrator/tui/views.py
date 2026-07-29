"""prompt_toolkit view construction for the ``cao tui`` thin shell.

U1 delivers the *frame*: the three-region main layout (header / body: nav |
build+preview / foot) and the server-unreachable screen (S-unreachable). Content
controls (nav list items, build fields, preview text, provider rows) are filled
by later units (U2/U3/U4/U6); U1 defines the containers each plugs into and
renders every status as TEXT (never colour alone — NFR-6, keyboard-only).

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only stdlib, ``prompt_toolkit``, ``requests``,
``cli_agent_orchestrator.constants`` and
``cli_agent_orchestrator.utils.path_validation`` may be imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import FilterOrBool, to_filter
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.widgets import Frame

from cli_agent_orchestrator.constants import API_BASE_URL

# A zero-arg text provider the App passes in so the frame renders live model
# state at draw time. ``views`` invokes these callables but imports no model —
# the thin-shell import boundary is unaffected (the frame stays a pure view).
TextProvider = Callable[[], str]

# The exact command a user runs to start the API server. ``cao-server`` is the
# console-script entry point declared in pyproject ([project.scripts]). Shown —
# and offered for copy — on the unreachable screen. RD-c=A: the TUI never owns
# the server process, so there is deliberately NO auto-start control.
SERVER_START_COMMAND = "cao-server"

# How many completions the floating menu shows at once. Bounded so a command with
# many flags cannot let the float cover the whole build/preview pane.
COMPLETIONS_MENU_MAX_HEIGHT = 8

# The hard budget the footer must fit. The key-map line lives in a
# ``Window(height=1)`` with ``wrap_lines=False``, so anything past the terminal
# width is silently TRUNCATED — the operator simply never learns those keys exist.
# 80 columns is the floor we support (FR-10.2).
KEY_MAP_MAX_WIDTH = 80

# Context-sensitive key maps (FR-10.1). One variant per screen/mode, so a screen
# never advertises a key that does nothing there. Every variant MUST stay within
# :data:`KEY_MAP_MAX_WIDTH`.
#
# The MAIN variant is additionally ABBREVIATED (FR-10.2, the recorded bounded
# extension). Context-sensitivity alone cannot help there: in navigating mode on
# the main screen every one of the 11 keys is valid, so nothing can be dropped —
# the readable label style extended to all 11 groups is 161 characters, and
# moderate shortening still lands at 86. Only the main variant is abbreviated; the
# other variants advertise few enough keys to keep the readable style.
KEY_MAP_MAIN = "arw:sel Tab:pane Ent:go Esc:up c:copy e:edit x:clr p:prof /:find r:retry q:quit"

# The overlay owns the keyboard while open: the navigation keys are suppressed by
# the ``editing`` filter, so advertising them would be a lie.
KEY_MAP_EDITING = "type a value  |  Enter: commit  |  Esc: cancel  |  Ctrl-C: quit"

# S-unreachable: the only meaningful actions are copying the start command,
# re-probing, and quitting.
KEY_MAP_UNREACHABLE = "[c] copy start command  |  [r] retry connection  |  [q] quit"

# S-catalog-fatal: `cao` is not runnable, so nothing can be navigated, built,
# copied or retried. Advertising anything else would be false — this is the exact
# defect the nit reported (the fatal screen offered keys that do nothing).
KEY_MAP_CATALOG_FATAL = "[q] quit  |  Ctrl-C: quit"

# S-profiles: a read-only browse list; Esc returns to main.
KEY_MAP_PROFILES = "arrows: select  |  Esc: back to commands  |  [q] quit"

# Every variant, keyed by the string a caller passes to :func:`key_map_hint`.
KEY_MAPS = {
    "main": KEY_MAP_MAIN,
    "editing": KEY_MAP_EDITING,
    "unreachable": KEY_MAP_UNREACHABLE,
    "catalog_fatal": KEY_MAP_CATALOG_FATAL,
    "profiles": KEY_MAP_PROFILES,
}


def key_map_hint(screen: str, *, editing: bool = False) -> str:
    """The key-map line for ``screen`` (FR-10.1), never wider than 80 columns.

    Args:
        screen: The active screen (``"main"``, ``"unreachable"``,
            ``"catalog_fatal"``, ``"profiles"``). An unknown screen falls back to
            the main variant rather than rendering nothing.
        editing: ``True`` while the input overlay holds the keyboard — the overlay
            variant then wins over the screen variant, because the navigation keys
            are suppressed by the ``editing`` key filter while it is open.

    Returns:
        The key-map string for that context, guaranteed ``<= 80`` characters.
    """

    if editing:
        return KEY_MAP_EDITING
    return KEY_MAPS.get(screen, KEY_MAP_MAIN)


# First-open guiding copy for the main body (S-main empty variant). Reinforces
# NFR-4 / TC-3: the CLI stays fully usable on its own.
MAIN_BODY_HINT = (
    "Select a command group on the left. Every action shows the exact "
    "`cao ...` command it will run; the CLI stays fully usable on its own."
)


@dataclass
class ScreenState:
    """U1-local view state (no domain/business state lives here).

    Attributes:
        reachable: Result of the W-1 liveness probe (``True`` == cao-server
            answered). Drives the ``main`` vs ``unreachable`` screen and the
            header reachability label.
        screen: Which screen is showing — ``"main"`` or ``"unreachable"``.
        focus: Which region currently holds focus (``"nav"`` | ``"build"`` |
            ...). U1 only tracks it; later units act on it.
    """

    reachable: bool = True
    screen: str = "main"
    focus: str = "nav"


def _reachability_label(reachable: bool) -> str:
    """Server reachability rendered as a TEXT label (NFR-6, never colour)."""

    return "server: REACHABLE" if reachable else "server: NOT REACHABLE"


def header_text(state: ScreenState) -> str:
    """Header line: title plus the server-reachability text label."""

    return f"cao tui   {_reachability_label(state.reachable)}   [{API_BASE_URL}]"


def unreachable_text() -> str:
    """Body copy for S-unreachable, including the exact start command.

    Command-building and copy still work while the server is down; only live
    reads are unavailable. Shows the ``cao-server`` start command with a copy
    affordance and the retry key. RD-c=A: no auto-start offer.
    """

    return "\n".join(
        [
            "cao-server is NOT REACHABLE.",
            "",
            "You can still build and copy commands; live status reads are",
            "unavailable until the server is up.",
            "",
            "Start the server with:",
            "",
            f"    {SERVER_START_COMMAND}",
            "",
            "[c] copy the start command    [r] retry connection    [q] quit",
        ]
    )


# Default text providers — the guiding copy shown before the App wires its live
# models. Each is a zero-arg callable so ``build_layout`` can render it lazily.
def _default_nav_text() -> str:
    """Nav-pane guiding copy shown until the App supplies the live list."""

    return "(command groups load here)"


def _default_build_text() -> str:
    """Build-pane guiding copy (the first-open hint)."""

    return MAIN_BODY_HINT


def _default_preview_text() -> str:
    """Preview-pane copy — empty until a command is selected (U3 fills it)."""

    return ""


def _default_preflight_text() -> str:
    """Footer pre-flight copy shown until the App supplies live provider rows."""

    return "providers: (pre-flight loads here)"


def build_layout(
    state: ScreenState,
    *,
    nav_text: Optional[TextProvider] = None,
    build_text: Optional[TextProvider] = None,
    preview_text: Optional[TextProvider] = None,
    preflight_text: Optional[TextProvider] = None,
    input_buffer: Optional[Buffer] = None,
    input_prompt: Optional[TextProvider] = None,
    input_visible: FilterOrBool = False,
) -> Layout:
    """Assemble the main three-region shell, wiring live text providers.

    Header (title + reachability text) over a body VSplit — a focusable
    command-group nav on the left, and on the right an HSplit of the build panel
    (top) over the always-visible read-only command preview (bottom) — over a
    footer (provider pre-flight line + global key map). Each content region's
    text is driven by an optional zero-arg provider callable; when omitted the
    guiding-copy default is used. The App passes lambdas that read its models;
    ``views`` only *invokes* them (importing no model), so the thin-shell import
    boundary is unaffected.

    Args:
        state: The U1 view state (reachability / screen).
        nav_text: Provider for the nav list (default: guiding copy).
        build_text: Provider for the build panel (default: :data:`MAIN_BODY_HINT`).
        preview_text: Provider for the preview pane (default: empty).
        preflight_text: Provider for the footer pre-flight line (default: guiding
            copy).
        input_buffer: The live :class:`~prompt_toolkit.buffer.Buffer` the ``[e]``
            edit / ``[/]`` search overlay reads. The App owns it (and attaches its
            completer); ``views`` only renders it. ``None`` omits the overlay row
            entirely, which keeps the pre-overlay call signature working.
        input_prompt: Provider for the overlay's prompt label (e.g. ``"search: "``).
        input_visible: Filter deciding whether the overlay row is shown — the App
            passes a condition over its own capture state, so ``views`` still
            imports no model.
    """

    nav_provider = nav_text or _default_nav_text
    build_provider = build_text or _default_build_text
    preview_provider = preview_text or _default_preview_text
    preflight_provider = preflight_text or _default_preflight_text

    header = Window(
        content=FormattedTextControl(text=lambda: header_text(state)),
        height=1,
        style="reverse",
    )

    # Left: focusable command-group navigation (live list from the navigator).
    nav = Frame(
        title="commands",
        body=Window(
            content=FormattedTextControl(text=lambda: nav_provider(), focusable=True),
            width=Dimension(min=18, preferred=24),
        ),
    )

    # Right-top: argument/build fields (live from the builder).
    build = Frame(
        title="build",
        body=Window(
            content=FormattedTextControl(text=lambda: build_provider(), focusable=True),
        ),
    )

    # Right-bottom: always-visible read-only command preview (the exact argv).
    preview = Frame(
        title="preview (exact `cao ...`)",
        body=Window(
            content=FormattedTextControl(text=lambda: preview_provider(), focusable=False),
            height=Dimension(min=3, preferred=5),
        ),
    )

    body = VSplit(
        [
            nav,
            HSplit([build, preview]),
        ]
    )

    # Footer: the live provider pre-flight line over the static global key map.
    preflight_line = Window(
        content=FormattedTextControl(text=lambda: preflight_provider()),
        height=1,
        style="reverse",
    )
    # The key map is context-sensitive (FR-10.1) and re-read at every repaint, so
    # opening the overlay swaps it to the overlay variant live. ``input_visible`` is
    # the App's own capture condition, so ``views`` still imports no model.
    keymap_filter = to_filter(input_visible)
    keymap_line = Window(
        content=FormattedTextControl(text=lambda: key_map_hint("main", editing=keymap_filter())),
        height=1,
        style="reverse",
    )
    foot = HSplit([preflight_line, keymap_line])

    # The ``[e]`` edit / ``[/]`` search capture row: a single line between the
    # body and the footer, shown only while a capture is active. Keeping it inline
    # (rather than swapping screens) means the operator watches the list narrow as
    # they type. The buffer belongs to the App — this is the LIVE input surface
    # that makes ``set_arg``/``apply_search`` reachable by keyboard, and whose
    # completer is the App's ``ArgCompleter``.
    rows = [header, body]
    if input_buffer is not None:
        prompt_provider = input_prompt or (lambda: "> ")
        rows.append(
            ConditionalContainer(
                content=VSplit(
                    [
                        Window(
                            content=FormattedTextControl(text=lambda: prompt_provider()),
                            height=1,
                            dont_extend_width=True,
                            style="reverse",
                        ),
                        Window(content=BufferControl(buffer=input_buffer), height=1),
                    ]
                ),
                filter=input_visible,
            )
        )
    rows.append(foot)

    # The root is a FloatContainer so the overlay's completions can be DRAWN
    # (FR-2.1). A ``CompletionsMenu`` is not a container the app can nest in an
    # HSplit — prompt_toolkit renders it only as a float, anchored to the cursor
    # position of the focused ``BufferControl``. Before this, the ``ArgCompleter``
    # attached to the overlay's buffer computed completions that had nowhere to
    # appear, so no completion could ever be seen or accepted.
    root = FloatContainer(
        content=HSplit(rows),
        floats=[
            Float(
                xcursor=True,
                ycursor=True,
                content=CompletionsMenu(max_height=COMPLETIONS_MENU_MAX_HEIGHT, scroll_offset=1),
            )
        ],
    )
    # Focus the nav frame's inner window first (keyboard-only entry point).
    return Layout(root, focused_element=nav.body)


def build_unreachable_view(
    state: ScreenState | None = None,
    *,
    notice_text: Optional[TextProvider] = None,
) -> Layout:
    """Assemble the S-unreachable screen (RD-c=A).

    A single alert region replaces the body: it explains that command-building
    and copy still work, shows the exact ``cao-server`` start command with a
    copy affordance ``[c]`` and a ``[r]`` retry key. No auto-start control.

    Args:
        state: The view state (drives the header reachability label).
        notice_text: Provider for the shared transient status line (OQ-2) — the
            ``[c]`` copy confirmation and its clipboard-unavailable fallback
            (FR-5.3). This screen NEEDS it: ``[c] copy start command`` is the first
            action :data:`KEY_MAP_UNREACHABLE` advertises, and before this the
            notice was spliced only into :func:`build_layout`'s build panel — so a
            copy here set a confirmation that no region could draw and the operator
            saw nothing. Read at every repaint, so it appears and clears live.
            Returning an empty string (or omitting the provider) renders no line.
    """

    state = state or ScreenState(reachable=False, screen="unreachable")
    notice_provider = notice_text or (lambda: "")

    header = Window(
        content=FormattedTextControl(text=lambda: header_text(state)),
        height=1,
        style="reverse",
    )

    def alert_body() -> str:
        notice = notice_provider()
        prefix = f"status: {notice}\n\n" if notice else ""
        return prefix + unreachable_text()

    alert = Frame(
        title="server unreachable",
        body=Window(
            content=FormattedTextControl(text=lambda: alert_body(), focusable=True),
        ),
    )

    foot = Window(
        content=FormattedTextControl(text=key_map_hint("unreachable")),
        height=1,
        style="reverse",
    )

    root = HSplit([header, alert, foot])
    return Layout(root, focused_element=alert.body)


def build_catalog_fatal_view(message: str) -> Layout:
    """Assemble the fatal "cao not found" screen (U1 business-logic-model L75-76).

    Shown when the catalog reports the ``cao`` executable itself is missing or
    not runnable (a ``CatalogError`` caused by ``FileNotFoundError``): the shell
    cannot introspect any command, so this is fatal, not the recoverable
    server-unreachable state. A single alert region explains that ``cao`` could
    not be run and offers ``[q]`` to quit; the App exits non-zero. This is
    distinct from :func:`build_unreachable_view` (the server is down but ``cao``
    and command-building still work).

    Args:
        message: The catalog error detail (e.g. ``cao`` executable not found).
    """

    body = "\n".join(
        [
            "cao could not be run.",
            "",
            message or "The `cao` command was not found on PATH.",
            "",
            "The TUI builds its command surface by running `cao ... --help`, so it",
            "cannot start without a runnable `cao`. Check that `cao` is installed",
            "and on your PATH, then relaunch.",
            "",
            "[q] quit",
        ]
    )

    header = Window(
        content=FormattedTextControl(text="cao tui   FATAL: cao not runnable"),
        height=1,
        style="reverse",
    )

    alert = Frame(
        title="cao not found",
        body=Window(content=FormattedTextControl(text=body, focusable=True)),
    )

    foot = Window(
        content=FormattedTextControl(text=key_map_hint("catalog_fatal")),
        height=1,
        style="reverse",
    )

    root = HSplit([header, alert, foot])
    return Layout(root, focused_element=alert.body)


def build_profiles_view(
    state: ScreenState,
    *,
    profiles_text: Optional[TextProvider] = None,
    detail_text: Optional[TextProvider] = None,
    notice_text: Optional[TextProvider] = None,
) -> Layout:
    """Assemble the S-profiles screen (FR-3.1) — the browse surface for Profiles.

    Follows the existing screen-swap pattern (``main`` / ``unreachable`` /
    ``catalog_fatal``): a header over a body VSplit — the selectable profile list
    on the left, the selected profile's detail on the right — over the
    profiles-specific key map. Esc returns to the main screen (the App owns the
    binding).

    Both regions are filled by zero-arg providers the App passes in (reading its
    ``ProfilesBrowser``), so ``views`` still imports no model and the thin-shell
    boundary is unaffected. A failed read is the browser's own *state* and comes
    through these providers as rendered notice text — never a traceback (FR-3.2).

    Args:
        state: The view state (drives the header reachability label).
        profiles_text: Provider for the list pane.
        detail_text: Provider for the detail pane.
        notice_text: Provider for the shared transient status line (OQ-2). Needed
            because ``[x]`` (clear argument) is filtered only on the input overlay,
            not on the screen, so it fires here and sets a notice; before this the
            notice was spliced only into :func:`build_layout`, so it was invisible.
            Rendered above the detail pane. Empty string / omitted renders no line.
    """

    list_provider = profiles_text or (lambda: "(profiles load here)")
    detail_provider = detail_text or (lambda: "")
    notice_provider = notice_text or (lambda: "")

    def detail_body() -> str:
        notice = notice_provider()
        prefix = f"status: {notice}\n\n" if notice else ""
        return prefix + detail_provider()

    header = Window(
        content=FormattedTextControl(text=lambda: header_text(state)),
        height=1,
        style="reverse",
    )

    listing = Frame(
        title="profiles",
        body=Window(
            content=FormattedTextControl(text=lambda: list_provider(), focusable=True),
            width=Dimension(min=18, preferred=28),
        ),
    )

    detail = Frame(
        title="profile detail",
        body=Window(
            content=FormattedTextControl(text=lambda: detail_body(), focusable=True),
        ),
    )

    foot = Window(
        content=FormattedTextControl(text=key_map_hint("profiles")),
        height=1,
        style="reverse",
    )

    root = HSplit([header, VSplit([listing, detail]), foot])
    return Layout(root, focused_element=listing.body)
