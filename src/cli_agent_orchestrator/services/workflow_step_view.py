"""Read-only views of a workflow step's terminal, for the run view's Terminal tab.

Two reads back ``GET /workflows/runs/{run_id}/steps/{step_id}/screen`` and
``.../transcript``:

* :func:`capture_screen`, while the step runs: a plain-text capture of the step's
  terminal through the backend's ``get_history``, so it works on tmux and herdr alike. It
  attaches no client, sends no keys and resizes nothing, so the view is watch-only by
  construction. (A tmux attach from a small PTY shrinks the agent's window, which is why
  the run view does not use the Agents tab's WebSocket.)
* :func:`read_transcript`, after the step's terminal ended: the tail of the
  ``<terminal_id>.scrollback`` file ``terminal_service`` writes at teardown, which
  ``cleanup_service`` deletes after ``RETENTION_DAYS``.

TRUST. Both take the terminal id from the step's journal row (:func:`recorded_terminal_id`),
never from the request, and each re-checks it against the ``TerminalId`` pattern inline,
in the function that uses it. Both redact secrets BEFORE they cut the text to its bound:
the screen captures more lines than it returns, the transcript reads a margin before the
bytes it returns. A secret straddling the edge of what is returned is therefore redacted
whole and never returned in part.

Every function here is synchronous (sqlite, tmux or herdr, and file I/O): callers on the
event loop run them through ``asyncio.to_thread``.
"""

from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Tuple

from cli_agent_orchestrator.backends import TerminalNotFoundError
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.constants import (
    TERMINAL_LOG_DIR,
    WORKFLOW_STEP_SCREEN_CAPTURE_LINES,
    WORKFLOW_STEP_SCREEN_MAX_BYTES,
    WORKFLOW_STEP_SCREEN_MAX_LINES,
    WORKFLOW_STEP_TRANSCRIPT_MARGIN_BYTES,
    WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES,
)
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.secret_gate import redact_secrets
from cli_agent_orchestrator.services.step_output_store import _validate_key_part

# The ``TerminalId`` pattern (``models/terminal.py``), as a plain regex. It is repeated
# INLINE in each function that uses a terminal id, rather than factored into a helper,
# because CodeQL's py/path-injection analysis only accepts a guard in the same function
# as the sink it protects (the ``profile_store`` precedent).
_TERMINAL_ID_RE = re.compile(r"^[a-f0-9]{8}$")

_INVALID_TERMINAL_DETAIL = "the step's recorded terminal id is not a valid terminal id"


class StepTerminalNotFound(LookupError):
    """Unknown run or step, or no usable terminal recorded for the step (404)."""


class StepTerminalEnded(Exception):
    """The step's terminal has ended, so its screen cannot be captured (410)."""


class TranscriptNotFound(LookupError):
    """No transcript file exists for the step's terminal (404).

    The step is still running, the run predates the run view, or ``cleanup_service``
    deleted the file after ``RETENTION_DAYS``.
    """


@dataclass(frozen=True)
class ScreenCapture:
    """The live view of a running step: plain text, redacted, bounded."""

    terminal_id: str
    text: str
    truncated: bool
    redacted: bool
    captured_at: str


@dataclass(frozen=True)
class Transcript:
    """The saved transcript of a finished step: plain text, redacted, bounded."""

    terminal_id: str
    text: str
    truncated: bool
    redacted: bool


def recorded_terminal_id(run_id: str, step_id: str) -> str:
    """Return the terminal id the journal recorded for a step.

    Raises:
        ValueError: ``run_id`` or ``step_id`` is not a valid key (the route maps it to 400).
        StepTerminalNotFound: the run or the step is unknown, or the step has no terminal
            recorded: it has not started, it ran before the run view recorded terminals,
            or its launch was refused.
    """
    _validate_key_part(run_id, "run_id")
    _validate_key_part(step_id, "step_id")
    if workflow_journal.get_run(run_id) is None:
        raise StepTerminalNotFound(f"unknown run '{run_id}'")
    row = workflow_journal.get_step(run_id, step_id)
    if row is None:
        raise StepTerminalNotFound(f"run '{run_id}' has no step '{step_id}'")
    if not row.terminal_id:
        raise StepTerminalNotFound(f"step '{step_id}' of run '{run_id}' has no terminal recorded")
    return row.terminal_id


def capture_screen(terminal_id: str) -> ScreenCapture:
    """Capture the step's live screen: the visible pane plus scrollback above it.

    Takes the last ``WORKFLOW_STEP_SCREEN_CAPTURE_LINES`` lines of history (tmux counts
    them above the visible pane), redacts the whole capture, drops the empty rows below
    the last output, then returns the last ``WORKFLOW_STEP_SCREEN_MAX_LINES`` lines, at
    most ``WORKFLOW_STEP_SCREEN_MAX_BYTES``. ``truncated`` is True when lines or bytes
    were dropped. Read-only: no client attaches and no key is sent.

    Raises:
        StepTerminalNotFound: ``terminal_id`` is not a valid terminal id.
        StepTerminalEnded: the terminal's registry row is gone, or its pane is.
    """
    # Inline guard: see _TERMINAL_ID_RE. The id comes from the journal, never the request.
    if not _TERMINAL_ID_RE.fullmatch(terminal_id):
        raise StepTerminalNotFound(_INVALID_TERMINAL_DETAIL)
    metadata = get_terminal_metadata(terminal_id)
    if metadata is None:
        raise StepTerminalEnded(f"terminal {terminal_id} has ended; read the step's transcript")
    try:
        raw = get_backend().get_history(
            metadata["tmux_session"],
            metadata["tmux_window"],
            tail_lines=WORKFLOW_STEP_SCREEN_CAPTURE_LINES,
            strip_escapes=True,
        )
    except (ValueError, TerminalNotFoundError) as e:
        # The registry row outlived the pane (teardown in progress, or a deferred cleanup
        # kept the row for a retry): there is no screen left to capture.
        raise StepTerminalEnded(
            f"terminal {terminal_id} has ended; read the step's transcript"
        ) from e
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Redact the whole capture first, then cut.
    text, fired = redact_secrets(raw)
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()  # the empty rows of the visible pane below the last output
    lines_dropped = len(lines) > WORKFLOW_STEP_SCREEN_MAX_LINES
    kept, bytes_dropped = _keep_tail(
        "\n".join(lines[-WORKFLOW_STEP_SCREEN_MAX_LINES:]), WORKFLOW_STEP_SCREEN_MAX_BYTES
    )
    return ScreenCapture(
        terminal_id=terminal_id,
        text=kept,
        truncated=lines_dropped or bytes_dropped,
        redacted=bool(fired),
        captured_at=captured_at,
    )


def read_transcript(terminal_id: str) -> Transcript:
    """Return the tail of the step's saved transcript, ``<terminal_id>.scrollback``.

    Reads the last ``WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES`` plus a
    ``WORKFLOW_STEP_TRANSCRIPT_MARGIN_BYTES`` margin, drops the partial first line when
    the read starts inside the file, redacts, then keeps the last
    ``WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES`` starting at a line boundary. ``truncated`` is
    True when the file holds more than the returned text.

    The file is opened without following a symlink and must be a regular file, the same
    posture as the run-artifact reads in ``api/main.py``.

    Raises:
        StepTerminalNotFound: ``terminal_id`` is not a valid terminal id.
        TranscriptNotFound: no regular transcript file exists for the terminal.
        OSError: any other read failure (surfaced, never reported as an empty transcript).
    """
    # Inline guard (CodeQL py/path-injection): the id is joined into a path below, so the
    # check must sit in this function. The id comes from the journal, never the request.
    if not _TERMINAL_ID_RE.fullmatch(terminal_id):
        raise StepTerminalNotFound(_INVALID_TERMINAL_DETAIL)
    path = os.path.join(str(TERMINAL_LOG_DIR), f"{terminal_id}.scrollback")
    window = WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES + WORKFLOW_STEP_TRANSCRIPT_MARGIN_BYTES
    missing = TranscriptNotFound(f"no transcript was saved for terminal {terminal_id}")
    try:
        # O_NONBLOCK keeps a FIFO planted at the path from blocking the open; it has no
        # effect on a regular file.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except FileNotFoundError as e:
        raise missing from e
    except OSError as e:
        if e.errno == errno.ELOOP:  # a symlink: refused, never followed
            raise missing from e
        raise
    # Check the descriptor before wrapping it: os.fdopen refuses a directory with
    # IsADirectoryError, which would surface as a 500 instead of a 404.
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise missing
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as f:
        start = max(0, info.st_size - window)
        f.seek(start)
        data = f.read(window)

    if start > 0:
        # The read began inside a line. Drop that partial line BEFORE redaction, so a
        # credential cut by the read's own start is never returned in part.
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline != -1 else b""
    text, fired = redact_secrets(data.decode("utf-8", errors="replace"))
    kept, cut = _keep_tail(text, WORKFLOW_STEP_TRANSCRIPT_MAX_BYTES)
    return Transcript(
        terminal_id=terminal_id,
        text=kept,
        truncated=start > 0 or cut,
        redacted=bool(fired),
    )


def _keep_tail(text: str, max_bytes: int) -> Tuple[str, bool]:
    """Keep at most ``max_bytes`` of UTF-8 from the end of ``text``, from a line start.

    Returns ``(kept, cut)``. Called only on text that is already redacted, so a cut can
    split a ``[REDACTED:...]`` marker or ordinary text, never a credential. When no
    newline falls inside the kept bytes, the cut stays mid-line rather than dropping
    everything; a partial UTF-8 sequence at the start is dropped.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    tail = encoded[len(encoded) - max_bytes :]
    newline = tail.find(b"\n")
    if newline != -1:
        tail = tail[newline + 1 :]
    return tail.decode("utf-8", errors="ignore"), True
