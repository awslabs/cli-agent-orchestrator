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

from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Frame

from cli_agent_orchestrator.constants import API_BASE_URL

# The exact command a user runs to start the API server. ``cao-server`` is the
# console-script entry point declared in pyproject ([project.scripts]). Shown —
# and offered for copy — on the unreachable screen. RD-c=A: the TUI never owns
# the server process, so there is deliberately NO auto-start control.
SERVER_START_COMMAND = "cao-server"

# Global key map, shown in the footer. ``[s]`` is intentionally absent
# (RD-e=A: no status pane / no status key).
KEY_MAP_HINT = (
    "arrows/Tab: focus  |  Enter: activate  |  [c] copy  |  [e] edit  " "|  [/] search  |  [q] quit"
)

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


def build_layout(state: ScreenState) -> Layout:
    """Assemble the main three-region shell as empty focusable containers.

    Header (title + reachability text) over a body VSplit — a focusable
    command-group nav on the left, and on the right an HSplit of the build panel
    (top) over the always-visible read-only command preview (bottom) — over a
    footer (provider pre-flight placeholder + global key map). Content is filled
    by later units; U1 frames the containers and wires focus.
    """

    header = Window(
        content=FormattedTextControl(text=lambda: header_text(state)),
        height=1,
        style="reverse",
    )

    # Left: focusable command-group navigation (populated by U2).
    nav = Frame(
        title="commands",
        body=Window(
            content=FormattedTextControl(text="(command groups load here)", focusable=True),
            width=Dimension(min=18, preferred=24),
        ),
    )

    # Right-top: argument/build fields (populated by U2/U3).
    build = Frame(
        title="build",
        body=Window(
            content=FormattedTextControl(text=MAIN_BODY_HINT, focusable=True),
        ),
    )

    # Right-bottom: always-visible read-only command preview (U3 fills the text).
    preview = Frame(
        title="preview (exact `cao ...`)",
        body=Window(
            content=FormattedTextControl(text="", focusable=False),
            height=Dimension(min=3, preferred=5),
        ),
    )

    body = VSplit(
        [
            nav,
            HSplit([build, preview]),
        ]
    )

    foot = Window(
        content=FormattedTextControl(text=KEY_MAP_HINT),
        height=1,
        style="reverse",
    )

    root = HSplit([header, body, foot])
    # Focus the nav frame's inner window first (keyboard-only entry point).
    return Layout(root, focused_element=nav.body)


def build_unreachable_view(state: ScreenState | None = None) -> Layout:
    """Assemble the S-unreachable screen (RD-c=A).

    A single alert region replaces the body: it explains that command-building
    and copy still work, shows the exact ``cao-server`` start command with a
    copy affordance ``[c]`` and a ``[r]`` retry key. No auto-start control.
    """

    state = state or ScreenState(reachable=False, screen="unreachable")

    header = Window(
        content=FormattedTextControl(text=lambda: header_text(state)),
        height=1,
        style="reverse",
    )

    alert = Frame(
        title="server unreachable",
        body=Window(
            content=FormattedTextControl(text=unreachable_text(), focusable=True),
        ),
    )

    foot = Window(
        content=FormattedTextControl(text=KEY_MAP_HINT),
        height=1,
        style="reverse",
    )

    root = HSplit([header, alert, foot])
    return Layout(root, focused_element=alert.body)
