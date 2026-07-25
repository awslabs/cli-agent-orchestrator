"""Literal input into one exact provider pane, and a live read of its turn state.

A native TUI has no control socket. The only way to give it a task is to
type into the pane it runs in, which makes *which pane* and *what exactly
was typed* the two facts everything else rests on.

Both are addressed the same way: by the immutable tmux pane id (``%N``)
rather than a session/window name. A name is a label that can be reused,
renamed, or resolved to a different pane after a window closes -- and
tmux answers a request for a missing window by acting on some *other*
pane with exit status 0, so a name-targeted write can land in a stranger's
terminal and report success. The pane id is minted once per pane and is
never reused while the server lives, so a write targeted at one either
reaches that pane or fails.

The writing side is deliberately split by effect. ``send_literal`` types
and never submits; ``send_enter`` submits and types nothing;
``send_soft_newline`` opens a new line inside the composer and submits
nothing. A single ``send(text)`` would let a newline inside a payload do
the submitting, which is how a half-composed message becomes a sent one.
``tmux send-keys -l`` writes the argument as literal characters -- no
key-name interpretation, and no paste buffer, so no bracketed-paste
sentinel can be introduced by this path at all.

Multi-line content is therefore delivered as content, not as newlines:
each line is typed literally and the line breaks between them are
composer keystrokes. The bytes ``\\r`` and ``\\n`` never reach the pane
from here in any mode.
"""

from __future__ import annotations

import subprocess
from typing import Optional, Sequence

from cli_agent_orchestrator.models.terminal import TerminalStatus

# tmux takes the payload as a single argv element. Long task messages are
# split so no one call approaches an argument-length limit, which would
# otherwise fail late and unpredictably on exactly the largest messages.
_LITERAL_CHUNK_CHARS = 1024

_DEFAULT_TIMEOUT_SECONDS = 10.0

# Refused rather than stripped. Every one of these either terminates the
# literal write early or submits it: ESC (7-bit and 8-bit CSI
# introducers) starts an escape sequence the pane will interpret, and CR
# or LF submits whatever is in the composer. A caller holding text with
# any of them built it through a path this module exists to replace, and
# silently editing the message a human asked to send would be worse than
# refusing it.
_ILLEGAL_LITERAL_CHARS = ("\x1b", "\x9b", "\r", "\n")


class NativePaneInputError(RuntimeError):
    """Base class for every failure this module raises."""


class NativePaneInputInvalid(NativePaneInputError):
    """The request could not be attempted; nothing was written."""


class NativePaneInputUnavailable(NativePaneInputError):
    """The pane could not be reached; nothing is known to have been written."""


class PartialLiteralWrite(NativePaneInputError):
    """Some chunks landed and a later one did not.

    Carries the exact boundary rather than a bare failure, because the
    two cases are not the same: nothing written licenses a retry, and
    something written does not. A caller that cannot tell them apart has
    to treat every transport failure as ambiguous, which is how a
    duplicate task gets sent.
    """

    def __init__(self, detail: str, *, chunks_sent: int, chunks_total: int) -> None:
        super().__init__(
            f"{detail} (wrote {chunks_sent} of {chunks_total} literal chunks; "
            f"the composer may hold a partial message and no Enter was sent)"
        )
        self.chunks_sent = chunks_sent
        self.chunks_total = chunks_total


def _tmux_binary() -> str:
    from cli_agent_orchestrator.clients.tmux import tmux_binary

    return tmux_binary()


def _run(argv: Sequence[str], *, timeout: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NativePaneInputUnavailable(f"tmux did not answer within {timeout}s: {exc}") from exc
    except OSError as exc:
        raise NativePaneInputUnavailable(f"tmux could not be executed: {exc}") from exc


def assert_literal_writable(text: str, *, field: str = "text") -> str:
    """Return ``text`` when it can be typed as one literal line."""
    if not isinstance(text, str) or not text:
        raise NativePaneInputInvalid(f"{field} must be a non-empty string; got {text!r}")
    for forbidden in _ILLEGAL_LITERAL_CHARS:
        if forbidden in text:
            raise NativePaneInputInvalid(
                f"{field} contains {forbidden!r}, which cannot be typed literally into a "
                f"provider composer; it would terminate the write or submit it early"
            )
    return text


def _chunks(text: str) -> list[str]:
    return [
        text[start : start + _LITERAL_CHUNK_CHARS]
        for start in range(0, len(text), _LITERAL_CHUNK_CHARS)
    ]


class TmuxPaneInput:
    """Write one literal line into one exact pane, then submit it.

    Satisfies the two-method transport the native control adapter
    requires. Neither method returns anything: there is no value tmux
    could return that would constitute provider acceptance, and offering
    one would invite a caller to read a successful write as a taken
    instruction.
    """

    def __init__(self, pane_id: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        if not isinstance(pane_id, str) or not pane_id.startswith("%"):
            raise NativePaneInputInvalid(
                f"pane_id must be an immutable tmux pane id like '%3'; got {pane_id!r}. "
                f"A session/window name is not identity: tmux resolves a missing window "
                f"to another pane and exits 0, so a name-targeted write can land elsewhere"
            )
        self._pane_id = pane_id
        self._timeout = timeout

    @property
    def pane_id(self) -> str:
        return self._pane_id

    def send_literal(self, text: str) -> None:
        """Type ``text`` into the pane exactly, submitting nothing."""
        payload = assert_literal_writable(text)
        chunks = _chunks(payload)
        binary = _tmux_binary()
        for index, chunk in enumerate(chunks):
            result = _run(
                [binary, "send-keys", "-t", self._pane_id, "-l", "--", chunk],
                timeout=self._timeout,
            )
            if result.returncode != 0:
                detail = (result.stderr or "").strip() or f"tmux exited {result.returncode}"
                if index == 0:
                    raise NativePaneInputUnavailable(
                        f"the first literal chunk was refused by tmux, so nothing was "
                        f"written to {self._pane_id}: {detail}"
                    )
                raise PartialLiteralWrite(
                    f"tmux refused a literal chunk for {self._pane_id}: {detail}",
                    chunks_sent=index,
                    chunks_total=len(chunks),
                )

    def send_enter(self) -> None:
        """Send the submitting key on its own, as a key name and not text."""
        result = _run(
            [_tmux_binary(), "send-keys", "-t", self._pane_id, "Enter"],
            timeout=self._timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"tmux exited {result.returncode}"
            raise NativePaneInputUnavailable(
                f"the submitting Enter was refused by tmux for {self._pane_id}: {detail}"
            )

    def send_key(self, keystroke: str) -> None:
        """Send one named, non-submitting key -- never text.

        Used for the keys that shape the composer rather than fill it:
        the newline that breaks a line without sending, and any key a
        provider pin needs before submitting. The argument is a tmux key
        *name*, so this cannot emit a literal ``\\n`` -- which would
        submit -- no matter what is passed.

        Deliberately without a default. Which key inserts a newline
        instead of sending is a per-provider, per-version fact that has
        to be proven against the installed build; a default here would be
        this module guessing on behalf of every provider, and the failure
        mode of a wrong guess is a message submitted in pieces.
        """
        if not isinstance(keystroke, str) or not keystroke.strip():
            raise NativePaneInputInvalid(
                f"keystroke must be a non-empty tmux key name; got {keystroke!r}"
            )
        for forbidden in _ILLEGAL_LITERAL_CHARS:
            if forbidden in keystroke:
                # A key *name* carrying CR/LF/ESC is a caller that built a
                # raw byte sequence and called it a keystroke.
                raise NativePaneInputInvalid(
                    f"keystroke {keystroke!r} contains {forbidden!r}; it must be a tmux key "
                    f"name such as 'C-j', not raw bytes"
                )
        result = _run(
            [_tmux_binary(), "send-keys", "-t", self._pane_id, keystroke],
            timeout=self._timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or f"tmux exited {result.returncode}"
            raise NativePaneInputUnavailable(
                f"the composer keystroke {keystroke!r} was refused by tmux for "
                f"{self._pane_id}: {detail}"
            )


def capture_pane_screen(pane_id: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    """The pane's visible rows, escape-free, or a raised error.

    Captured without ``-e`` so the rows are already the rendered
    viewport rather than a raw stream: the providers' status detectors
    read composited text, and handing them escape sequences would make a
    spinner match or miss for reasons that have nothing to do with the
    provider's actual state.
    """
    result = _run(
        [_tmux_binary(), "capture-pane", "-p", "-t", pane_id],
        timeout=timeout,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip() or f"tmux exited {result.returncode}"
        raise NativePaneInputUnavailable(f"could not capture pane {pane_id}: {detail}")
    return (result.stdout or "").splitlines()


def observe_kimi_turn_state(
    pane_id: str,
    *,
    terminal_id: str,
    session_name: str,
    window_name: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    screen: Optional[Sequence[str]] = None,
) -> TerminalStatus:
    """Read whether the Kimi TUI in ``pane_id`` is mid-turn, right now.

    Delegates to the Kimi provider's own detector rather than matching
    on anything here, so there is exactly one description of what a Kimi
    screen means. That detector's boot gate matters most for admission:
    Kimi paints its status bar *before* it can accept input, and a
    message delivered into that window is absorbed by the boot screen
    with no error anywhere -- the failure this observation exists to
    prevent. The boot state reads as ``PROCESSING``, so a caller that
    requires ``IDLE`` waits for a real prompt instead of typing into a
    screen that will swallow it.

    Raises rather than returning ``UNKNOWN`` when the pane cannot be
    read at all. "The pane says nothing" and "we could not look" must
    stay distinguishable: only the first is an observation.
    """
    from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider

    rows = list(screen) if screen is not None else capture_pane_screen(pane_id, timeout=timeout)
    # Built with the pane's real identifiers rather than placeholders, even
    # though the detector reads only the rows it is given: a provider that
    # later consults its own terminal would otherwise start answering about
    # a different one, and the bug would look like a flaky idle gate.
    provider = KimiCliProvider(
        terminal_id=terminal_id,
        session_name=session_name,
        window_name=window_name,
    )
    return provider.get_status_from_screen(rows)
