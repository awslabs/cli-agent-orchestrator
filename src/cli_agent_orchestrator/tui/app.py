"""``cao tui`` application shell.

The prompt_toolkit full-screen ``Application`` that composes the thin-shell
front door: it builds the root ``Layout`` (see :mod:`.views`), installs the
global :class:`~prompt_toolkit.key_binding.KeyBindings`, and, at startup, runs a
liveness probe that selects the main screen or the server-unreachable screen.

``main()`` is the ``cao tui`` entry callable (RD-b=A). U1 provides it but does
*not* wire it into ``cli/main.py`` — that registration is U5.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only stdlib, ``prompt_toolkit``, ``requests``,
``cli_agent_orchestrator.constants`` and
``cli_agent_orchestrator.utils.path_validation`` may be imported here. The
liveness probe below is a deliberate seam that returns ``True``; U4 replaces it
with a real HTTP ``health()`` call over ``requests`` (still thin-shell legal).
"""

from __future__ import annotations

from typing import Callable, Optional

from prompt_toolkit.application import Application
from prompt_toolkit.input.base import Input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.key_binding.key_processor import KeyPressEvent
from prompt_toolkit.output.base import Output

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.tui import views
from cli_agent_orchestrator.tui.views import ScreenState

# Exit codes returned by ``run()`` / ``main()``.
EXIT_OK = 0
EXIT_SIGINT = 130

# Type of the injectable liveness probe: given the API base URL, return whether
# cao-server is reachable.
LivenessProbe = Callable[[str], bool]


def probe_server_reachable(base_url: str = API_BASE_URL) -> bool:
    """Startup liveness-probe seam (U1 stub — always reachable).

    This is the single deliberate stub in U1. It lets the skeleton run
    standalone without a live cao-server. U4 replaces the body with a real HTTP
    ``health()`` GET against ``base_url`` over ``requests`` (which stays within
    the thin-shell import allow-list). Callers may also inject their own probe
    via :class:`App`.

    Args:
        base_url: The cao-server base URL to probe. Defaults to the
            constants-derived ``API_BASE_URL``.

    Returns:
        ``True`` when the server is considered reachable. The U1 stub always
        returns ``True`` so the main screen renders during development.
    """

    return True


class App:
    """The ``cao tui`` prompt_toolkit application shell.

    U1 owns the frame (layout + key map + screen selection). Domain behaviour
    (catalog, builder, preview, runner, pre-flight) is added by later units and
    routed through the key map U1 installs.
    """

    def __init__(self, liveness_probe: Optional[LivenessProbe] = None) -> None:
        """Build view state, key bindings, and the prompt_toolkit Application.

        Args:
            liveness_probe: Optional override for the startup/retry probe. When
                omitted, the module-level :func:`probe_server_reachable` stub is
                used (U4 supplies the real one).
        """

        self._probe: LivenessProbe = liveness_probe or probe_server_reachable
        self.state = ScreenState()
        # Text most recently placed on the clipboard by ``[c]`` — surfaced for
        # tests and for later status feedback.
        self.last_copied: Optional[str] = None

        # Run the startup liveness probe (W-1) to pick the initial screen.
        self.state.reachable = self._run_probe()
        self.state.screen = "main" if self.state.reachable else "unreachable"

        self.key_bindings = self.build_keybindings()
        self.application: Application = self._build_application()

    # -- probe / screen selection ------------------------------------------

    def _run_probe(self) -> bool:
        """Invoke the liveness probe, treating any failure as unreachable.

        Errors from the probe (network failures once U4 wires the real HTTP
        call) must never crash the shell — an unreachable server is a normal,
        recoverable state (S-unreachable), not a fatal error.
        """

        try:
            return bool(self._probe(API_BASE_URL))
        except Exception:
            return False

    def _select_layout(self) -> object:
        """Return the layout for the current screen (main vs unreachable)."""

        if self.state.screen == "unreachable":
            return views.build_unreachable_view(self.state)
        return views.build_layout(self.state)

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

    # -- clipboard ----------------------------------------------------------

    def copy_text(self) -> str:
        """Return the text ``[c]`` copies for the current screen.

        On S-unreachable this is the exact ``cao-server`` start command
        (RD-c=A). On S-main it is the command preview string — empty in the U1
        skeleton until U3 fills the preview.
        """

        if self.state.screen == "unreachable":
            return views.SERVER_START_COMMAND
        return ""

    # -- key bindings -------------------------------------------------------

    def build_keybindings(self) -> KeyBindings:
        """Install the global key map (W-2).

        Bound: arrows/Tab move focus, Enter activates the focused control,
        ``[c]`` copy, ``[e]`` edit, ``[q]`` quit, ``[/]`` search, ``[r]`` retry,
        plus Ctrl-C for a clean SIGINT exit. ``[s]`` is deliberately NOT bound
        (RD-e=A: no status pane / no status key).
        """

        kb = KeyBindings()

        # Focus movement (keyboard-only — NFR-6).
        kb.add("tab")(focus_next)
        kb.add("s-tab")(focus_previous)
        kb.add("down")(focus_next)
        kb.add("right")(focus_next)
        kb.add("up")(focus_previous)
        kb.add("left")(focus_previous)

        @kb.add("enter")
        def _activate(event: KeyPressEvent) -> None:
            """Activate the focused control. Behaviour lands in U2/U3."""

        @kb.add("c")
        def _copy(event: KeyPressEvent) -> None:
            """Copy the current preview / start command to the clipboard."""

            text = self.copy_text()
            self.last_copied = text
            event.app.clipboard.set_text(text)

        @kb.add("e")
        def _edit(event: KeyPressEvent) -> None:
            """Edit the focused argument field. Behaviour lands in U2/U3."""

        @kb.add("/")
        def _search(event: KeyPressEvent) -> None:
            """Search command groups. Behaviour lands in U2."""

        @kb.add("r")
        def _retry(event: KeyPressEvent) -> None:
            """Re-probe the server and swap screens (S-unreachable → S-main)."""

            self.retry()
            self._apply_screen()

        @kb.add("q")
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

        return Application(
            layout=self._select_layout(),  # type: ignore[arg-type]
            key_bindings=self.key_bindings,
            full_screen=True,
            mouse_support=False,  # keyboard-only (NFR-6)
            input=input,
            output=output,
        )

    def run(
        self,
        *,
        input: Optional[Input] = None,
        output: Optional[Output] = None,
    ) -> int:
        """Enter the event loop until ``[q]`` / Ctrl-C and return an exit code.

        Args:
            input: Optional prompt_toolkit input (for headless tests).
            output: Optional prompt_toolkit output (for headless tests).

        Returns:
            ``0`` on a normal quit, ``130`` on Ctrl-C, ``0`` for any other
            non-integer exit result.
        """

        if input is not None or output is not None:
            self.application = self._build_application(input=input, output=output)

        result = self.application.run()
        return result if isinstance(result, int) else EXIT_OK


def main() -> int:
    """``cao tui`` entry callable.

    Constructs the :class:`App` (which runs the startup liveness probe and picks
    the initial screen) and enters its run loop. Wiring this into
    ``cli/main.py`` as the ``cao tui`` subcommand is U5 (RD-b=A); U1 only
    provides the callable.

    Returns:
        The process exit code.
    """

    return App().run()
