"""Command runner + clipboard for the ``cao tui`` thin shell (U3, the mutation seam).

:class:`CommandRunner` is the single point where a logic-bearing action leaves
the TUI. It executes the ``cao`` command **as a subprocess** (SC-1 / BR-2) and
returns the CLI's own output verbatim; it constructs **no HTTP request of any
kind** — mutation goes through the CLI, never a mutating REST call. The argv it
runs is exactly the list produced by
:meth:`~cli_agent_orchestrator.tui.command_builder.CommandBuilder.preview_argv`,
so what the user saw previewed is byte-identical to what runs (FR-3.1 / BR-1).

**O-2 resolution (streaming).** A ``cao`` command may be interactive or stream
output; piping that through the prompt_toolkit renderer would garble it. So the
interactive path :meth:`run_in_app` *suspends* the full-screen application via
prompt_toolkit's :func:`run_in_terminal` seam, runs ``cao`` with the process's
**real inherited stdio** (``subprocess.run(argv)`` — no capture, no pipe into
the renderer), then resumes the app. Copying the command and actually running it
are therefore behaviourally identical: the CLI owns the terminal both ways. When
no prompt_toolkit application is running (tests / non-interactive callers),
:meth:`run` runs the same argv in *captured* mode
(``capture_output=True, text=True``) and returns a :class:`RunResult`. No new
dependency is introduced — stdlib :mod:`subprocess` + the existing
``prompt_toolkit`` only.

Error policy (BR-3 / BR-7): a non-zero ``cao`` exit is a **normal**
:class:`RunResult` (verbatim stdout/stderr/code — U3 does not interpret CLI
errors, SC-2); only a *spawn* failure (``FileNotFoundError`` / ``OSError``)
raises :class:`RunnerError`. Copy (FR-3.2) uses the prompt_toolkit clipboard and
never raises.

**ADR-013 is REVERSED (recorded here, at the point it was cited).** ADR-013 chose
"no ``pyperclip`` / no new dependency", so :meth:`CommandRunner.copy` wrote into
whatever clipboard the ``Application`` happened to carry. Because the App passed
no ``clipboard=``, that was prompt_toolkit's default ``InMemoryClipboard`` — a
process-local buffer discarded on exit. The advertised ``[c] copy`` therefore
never reached the OS clipboard: the operator pressed it, saw nothing, and had
nothing to paste. A copy affordance that copies nowhere is worse than no
affordance, so the App now constructs its ``Application`` with a
``PyperclipClipboard`` and ``pyperclip`` is declared as a real dependency in
``pyproject.toml`` (it was previously present only transitively, via ``fastmcp``,
which is not a contract anything may rely on). ``pyperclip`` is reached *through*
prompt_toolkit's ``PyperclipClipboard`` — no module under ``tui/`` imports it
directly, so the thin-shell import boundary is unchanged.

Alternative rejected: shell out to ``pbcopy``/``xclip``/``wl-copy``. That keeps
the dependency count but adds a per-platform binary-detection matrix and a second
process spawn on a keystroke, for behaviour ``PyperclipClipboard`` already
provides. Consequence accepted: one more unconditional runtime dependency for
every install, disclosed in the PR body alongside ``prompt_toolkit``.

Import rule (thin shell, enforced by ``test/tui/test_thin_shell_boundary.py``):
only the standard library, ``prompt_toolkit`` and the ``tui`` package's own
modules may be imported here. No ``requests`` (there is no HTTP here at all), no
``cli``/``services``/``clients``/``backends``/``providers``/``models`` layer.

Design references: business-logic-model W-4/W-5, business-rules BR-2/BR-3/BR-7,
domain-entities (RunResult / RunnerError), code-generation-plan Step 2.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Sequence

from prompt_toolkit.application import get_app_or_none, run_in_terminal

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Local domain model (in tui/, NOT cli_agent_orchestrator.models).              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RunResult:
    """Immutable capture of one ``cao`` subprocess run (domain-entities).

    ``stdout`` / ``stderr`` are the CLI's output verbatim; ``exit_code`` is its
    process return code. Rendered as-is (BR-3): a non-zero ``exit_code`` is a
    normal result, not an error condition U3 interprets.
    """

    stdout: str
    stderr: str
    exit_code: int


class RunnerError(Exception):
    """Raised when the ``cao`` subprocess cannot be *spawned* (BR-7).

    Carries the failing argv and the underlying OS error so the App (U1) can
    surface a clear "cao not runnable" state. NOT raised for a non-zero exit —
    that is a normal :class:`RunResult` (BR-3).
    """

    def __init__(self, argv: Sequence[str], os_error: BaseException) -> None:
        self.argv: List[str] = list(argv)
        self.os_error = os_error
        super().__init__(f"failed to launch `{' '.join(self.argv)}`: {os_error}")


class CommandRunner:
    """Execute a ``cao`` argv as a subprocess and copy the preview (W-4/W-5).

    Stateless; one instance can run many commands. The argv passed to
    :meth:`run` / :meth:`run_in_app` is used verbatim — it is expected to be a
    :meth:`CommandBuilder.preview_argv` list so the run is byte-identical to the
    preview (BR-1). Nothing here constructs an HTTP request (BR-2).
    """

    # -- W-4: captured (headless / non-interactive) run --------------------- #

    def run(self, argv: Sequence[str]) -> RunResult:
        """Run ``argv`` as a subprocess in *captured* mode → :class:`RunResult` (W-4).

        Used by tests and any non-interactive caller (no running prompt_toolkit
        app). Output is captured, not streamed; the interactive/streaming path
        is :meth:`run_in_app`. A non-zero exit is returned verbatim (BR-3); a
        spawn failure raises :class:`RunnerError` (BR-7).

        Args:
            argv: The full command argv (``argv[0]`` is the executable, e.g.
                ``"cao"``). Passed verbatim to :func:`subprocess.run` — never
                mutated, never re-quoted.

        Returns:
            A :class:`RunResult` with the CLI's verbatim stdout/stderr/exit code.

        Raises:
            RunnerError: If the subprocess cannot be launched
                (``FileNotFoundError`` / ``OSError``).
        """

        argv_list = list(argv)
        try:
            proc = subprocess.run(
                argv_list,
                capture_output=True,
                text=True,
            )
        except (FileNotFoundError, OSError) as exc:
            # BR-7: spawn failure is surfaced, never swallowed.
            raise RunnerError(argv_list, exc) from exc

        # BR-3: non-zero exit is a normal result rendered verbatim (SC-2).
        return RunResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            exit_code=proc.returncode,
        )

    # -- W-4: interactive run (O-2 suspend + inherit terminal) -------------- #

    def run_in_app(self, argv: Sequence[str]) -> None:
        """Run ``argv`` with the real terminal, suspending the TUI around it (O-2).

        The prompt_toolkit application is suspended via :func:`run_in_terminal`
        (which hides the full-screen UI and drops back to the normal terminal),
        ``cao`` runs with the process's inherited stdio so its interactive /
        streaming output goes straight to the real terminal (NOT piped through
        the renderer), then the app resumes. This makes running a command and
        copying-then-running it behaviourally identical — the CLI owns the
        terminal in both cases.

        Falls back to a plain inherited-stdio :func:`subprocess.run` when no
        prompt_toolkit application is running (so the same entry point works
        outside a live TUI). A spawn failure raises :class:`RunnerError`; a
        non-zero exit is not an error here (the CLI already showed its output on
        the inherited terminal).

        Args:
            argv: The full command argv (byte-identical to the preview, BR-1).

        Raises:
            RunnerError: If the subprocess cannot be launched.
        """

        argv_list = list(argv)

        app = get_app_or_none()
        if app is None:
            # No live TUI to suspend — run directly on the current terminal.
            # A synchronous caller can catch the spawn failure, so raise (BR-7).
            self._spawn_inherited(argv_list)
            return

        # A live full-screen app is running. run_in_terminal schedules the
        # suspend -> run -> resume on the event loop and returns immediately; a
        # key-binding handler cannot synchronously block on the result, so we do
        # NOT await it (that is the correct prompt_toolkit idiom — the scheduled
        # callable blocks the loop while cao owns the bare terminal, then the UI
        # repaints). Because the exception cannot propagate back to the handler,
        # a spawn failure is surfaced via the logger from inside the scheduled
        # callable rather than silently swallowed (BR-7).
        def _suspended_run() -> None:
            try:
                self._spawn_inherited(argv_list)
            except RunnerError as exc:
                logger.error("%s", exc)

        run_in_terminal(_suspended_run)

    @staticmethod
    def _spawn_inherited(argv_list: List[str]) -> None:
        """Run ``argv_list`` with the process's inherited stdio (no capture).

        The ``cao`` CLI streams / prompts directly on the real terminal (O-2 —
        output is never piped through the renderer). A spawn failure raises
        :class:`RunnerError` (BR-7); a non-zero exit is not an error here (the
        CLI has already shown its output on the inherited terminal).
        """

        try:
            subprocess.run(argv_list)
        except (FileNotFoundError, OSError) as exc:
            raise RunnerError(argv_list, exc) from exc

    # -- W-5: copy (FR-3.2) ------------------------------------------------- #

    def copy(self, text: str) -> bool:
        """Place ``text`` on the clipboard — never raises (W-5 / FR-5.2).

        Uses the running prompt_toolkit application's clipboard when available. The
        App now constructs the ``Application`` with a ``PyperclipClipboard``, so this
        reaches the real OS clipboard rather than the process-local
        ``InMemoryClipboard`` whose contents were discarded on exit (FR-5.1). This
        **reverses ADR-013** ("no ``pyperclip`` / no new dependency"); see the
        module docstring for the rationale.

        Fallback policy (the recorded FR-5.2 ⇄ FR-11.1 resolution). Whether the
        text may be printed depends on whether a full-screen application owns the
        terminal:

        * **Live-app path** (``get_app_or_none()`` is not ``None``) — NOTHING is
          written to ``stdout``/``stderr``. A ``print()`` here lands directly on top
          of the interface the UI is drawing, which is exactly the defect class
          FR-11.1 forbids. The caller reports the outcome through the UI's own notice
          line using this method's return value.
        * **Non-live path** (no running application) — no terminal has been taken
          over, so the stdout fallback survives and still hands the user the text.

        Never logs ``text`` itself: a copied command can carry a path or a value the
        operator would not expect in a log (NFR-9).

        Args:
            text: The preview string to copy (from
                :meth:`CommandBuilder.preview_string`).

        Returns:
            ``True`` when the text reached a clipboard; ``False`` when it did not
            (no clipboard, or the clipboard raised), so the caller can render the
            fallback notice. Never raises either way.
        """

        app = get_app_or_none()
        clipboard = getattr(app, "clipboard", None) if app is not None else None
        if clipboard is not None:
            try:
                clipboard.set_text(text)
                return True
            except Exception:  # noqa: BLE001 - clipboard is best-effort
                logger.warning("clipboard set_text failed")

        if app is not None:
            # A live full-screen app owns the terminal — the caller surfaces this
            # through the UI notice path instead (FR-5.2 / FR-11.1).
            return False

        # No app owns the terminal: print so the user can copy it manually.
        print(text, file=sys.stdout)
        return False


__all__: List[str] = ["CommandRunner", "RunResult", "RunnerError"]
