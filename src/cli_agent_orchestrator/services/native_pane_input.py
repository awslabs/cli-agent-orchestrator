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

The writing side is deliberately split by effect. ``send_literal`` writes
payload text and never submits; ``send_enter`` submits and writes no
payload; ``send_key`` sends one named control key. A single
``send(text)`` would let a newline inside a payload do the submitting,
which is how a half-composed message becomes a sent one.

The guarantee this module makes is about *payload writes*, and only
those: a literal write never contains CR, LF, or a bracketed-paste
marker. ``tmux send-keys -l`` writes its argument as literal characters
with no key-name interpretation and no paste buffer, so nothing in a
message can be reinterpreted as a control action.

``send_key`` carries no such guarantee, and must not be read as if it
did. A tmux key name is *resolved to bytes by tmux*, and those bytes are
then interpreted by the provider: ``C-j`` emits LF, and ``Enter`` submits
by design. Whether a given key inserts a newline rather than sending is a
fact about one provider build, so **the caller owns that meaning** by
pinning the key to an exact version. This module only guarantees it sends
the key it was given, to the pane it was given, or raises.

Multi-line content is delivered on those terms: each line is typed as a
literal payload write, and the breaks between lines are named keys chosen
by the version-pinned caller.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence, Tuple

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.control_input_contract import (
    SUBMISSION_SUBMITTED,
    SUBMISSION_UNKNOWN,
    SUBMISSION_UNSUBMITTED,
)

# tmux takes the payload as a single argv element. Long task messages are
# split so no one call approaches an argument-length limit, which would
# otherwise fail late and unpredictably on exactly the largest messages.
_LITERAL_CHUNK_CHARS = 1024

_DEFAULT_TIMEOUT_SECONDS = 10.0

# Refused rather than stripped. Each of these stops being payload the
# moment it arrives: ESC (7-bit and 8-bit CSI introducers) starts an
# escape sequence the pane will interpret, and CR or LF is a control byte
# the provider acts on -- submitting on some builds, breaking the line on
# others. Which one hardly matters here: the point is that a byte inside
# a message must never get to decide, and only the caller's pinned key
# names may carry that meaning. A caller holding text with any of them
# built it through a path this module exists to replace, and silently
# editing the message a human asked to send would be worse than refusing
# it.
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
        """Send one named key -- a key event, never payload text.

        Used for the keys that shape the composer rather than fill it,
        such as the newline that breaks a line without sending.

        **This does not promise the key is harmless.** tmux resolves the
        name to bytes and the provider interprets them: ``C-j`` emits LF,
        and ``Enter`` submits. Passing a submitting key here submits.
        What this guarantees is narrower and is the part payload text
        depends on -- the argument is a key *name*, so message content
        can never reach the pane through this method and be reinterpreted
        as a control action.

        Which key inserts a newline instead of sending is a per-provider,
        per-version fact that has to be proven against the installed
        build, so there is deliberately no default: the version-pinned
        caller owns that meaning. A default here would be this module
        guessing on behalf of every provider, and the failure mode of a
        wrong guess is a message submitted in pieces.
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


def observe_claude_turn_state(
    pane_id: str,
    *,
    terminal_id: str,
    session_name: str,
    window_name: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    screen: Optional[Sequence[str]] = None,
) -> TerminalStatus:
    """Read whether the Claude TUI in ``pane_id`` is mid-turn, right now.

    Delegates to the Claude provider's own detector for the same reason
    the Kimi observation delegates to Kimi's: there must be exactly one
    description of what a Claude screen means, and a second matcher here
    would drift from it silently.

    A separate function rather than a provider parameter, because the two
    detectors answer different questions about different renderings and
    the choice of which to use is made by the caller's provider binding,
    not at runtime from a string. That binding is checked before any
    write, so picking the detector from it keeps the observation and the
    delivery talking about the same provider.

    Raises rather than returning ``UNKNOWN`` when the pane cannot be read
    at all. "The pane says nothing" and "we could not look" must stay
    distinguishable: only the first is an observation.
    """
    from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

    rows = list(screen) if screen is not None else capture_pane_screen(pane_id, timeout=timeout)
    provider = ClaudeCodeProvider(
        terminal_id=terminal_id,
        session_name=session_name,
        window_name=window_name,
    )
    return provider.get_status_from_screen(rows)


# --- Provider-pinned submission barrier (cond-0026) ---------------------------
#
# Transport acceptance is not submission.  A composer that is still catching
# up with an input burst can swallow the Enter that follows it, leaving the
# control text resting unsubmitted while every tmux write reported success.
# The providers with a native control adapter (Kimi, Claude) already cross
# this boundary inside their own proven composer plans.  For a provider
# without one, the barrier below is the only thing standing between "tmux
# acked the bytes" and "the provider visibly took the control".
#
# The strategy is compose-visible settle: the text write and its one Enter
# are serialized through an observation of the composer itself.  The Enter
# is sent only after the control text is seen resting in the composer, and
# submission is claimed only when the composer is then seen to give the
# text up.  Exactly one Enter is ever sent.  If the text never becomes
# visible, no Enter is sent at all; if the composer never gives the text
# up, the control is ambiguous and is never re-driven — a second, blind
# Enter is how one requested submission becomes two.


@dataclass(frozen=True)
class SubmissionBarrier:
    """How one provider's composer is walked across the submit boundary.

    ``compose_settle_seconds`` bounds the wait for the typed text to become
    compose-visible.  ``post_enter_seconds`` bounds the wait for the
    composer to give the text up after the single Enter.  Both are bounded
    polls, not fixed sleeps: a healthy composer answers in one or two
    polls, and the full bound is only ever paid by a composer that is not
    answering.  ``composer_tail_rows`` is the region, counted up from the
    bottom of the screen, that holds the composer box and its status line
    for this provider's pinned builds.
    """

    compose_settle_seconds: float
    post_enter_seconds: float
    poll_interval_seconds: float
    composer_tail_rows: int


# The observation matches on the *tail* of the control text, not the whole
# string.  A long control wraps inside the composer box, and the box's top
# rows can scroll above the observed region; the wrap's last line always
# carries the text's own ending, so the ending is the one fragment that is
# present exactly while the composer holds the control.  48 normalized
# characters spans at most two composer rows on any sane pane width.
_OBSERVATION_SUFFIX_CHARS = 48

# One capture may not consume the whole write deadline while the composer
# is being polled.  A capture that cannot answer inside this bound is a
# failed observation, not a slow one.
_OBSERVATION_CAPTURE_TIMEOUT_SECONDS = 2.0

#: The provider-pinned barrier table, beside the adapters' steer-chord
#: pins.  Only Codex has an entry: it is the provider whose composer was
#: proven to swallow a back-to-back Enter (cond-0026), and it has no
#: native control adapter to cross the boundary on its own.  Kimi and
#: Claude deliberately have no entry — their adapter plans already carry
#: the proven submit settle, and adding a second barrier there would
#: change behaviour that has no contrary evidence.  A provider absent
#: from the table gets today's behaviour: text and Enter back to back,
#: with no observation claimed.
_SUBMISSION_BARRIERS = {
    # Codex (ratatui composer, pinned 0.145.x): the observed region is
    # the bottom four rows — the status line, the composer box's bottom
    # border, and its (possibly wrapped) input rows.  Four is the
    # boundary that keeps the composer's own contents in and everything
    # else out: a submitted control's transcript echo lands directly
    # above the box, outside the region, while the match on the text's
    # own ending keeps a wrapped long control visible through its last
    # input row.  Both failure directions of a misread region land on
    # ``ambiguous`` rather than on a wrong verdict: a region too small
    # fails the settle, a region too large reads the echo as the
    # composer, and neither sends a second Enter.  The settle bound is far
    # beyond the measured paste-burst windows (~120 ms class) and well
    # inside the 20 s control write deadline; the post-Enter bound covers
    # a slow first repaint without ever approaching that deadline.
    "codex": SubmissionBarrier(
        compose_settle_seconds=3.0,
        post_enter_seconds=5.0,
        poll_interval_seconds=0.1,
        composer_tail_rows=4,
    ),
}


def submission_barrier_for(provider: Optional[str]) -> Optional[SubmissionBarrier]:
    """The pinned submission barrier for ``provider``, or None.

    None means "no barrier is proven for this provider" and the caller
    keeps the current behaviour.  It never means "guess one": a barrier
    run against a composer whose layout was never read would fabricate
    the very observation this boundary exists to make honest.
    """
    if provider is None:
        return None
    return _SUBMISSION_BARRIERS.get(provider)


# Composer chrome: the box-drawing verticals and prompt glyphs a composer
# draws *around* the text it holds.  They sit between the fragments of a
# wrapped line, so matching without dropping them would split every wrap
# at its border.  They are stripped from the captured rows and from the
# control text alike, so a text that genuinely contains one still matches
# its own rendering.
_COMPOSER_CHROME_CHARS = "│┃>›"


def _normalised(text: str) -> str:
    """``text`` with whitespace and composer chrome removed.

    Whitespace is the one thing the composer may reflow (wrapping,
    indentation, box padding) without changing what the operator typed,
    and chrome is what it draws around the text rather than as part of
    it, so the comparison is made on the characters that cannot move.
    """
    return "".join(
        char for char in text if not char.isspace() and char not in _COMPOSER_CHROME_CHARS
    )


def composed_text_visible(
    rows: Sequence[str],
    text: str,
    *,
    composer_tail_rows: int,
) -> bool:
    """Whether the control text is visibly resting in the composer region.

    The region is the bottom ``composer_tail_rows`` rows of the captured
    screen.  The match is on the normalised tail of the text (see
    :data:`_OBSERVATION_SUFFIX_CHARS`), so a wrapped composer line still
    counts, and a transcript echo of an already-submitted copy — which
    renders above the composer box — does not.
    """
    if not rows:
        return False
    needle = _normalised(text)[-_OBSERVATION_SUFFIX_CHARS:]
    if not needle:
        return False
    haystack = _normalised("".join(rows[-composer_tail_rows:]))
    return needle in haystack


def submission_evidence_ref(pane_id: str, rows: Sequence[str]) -> str:
    """A durable pointer to the capture a submission verdict rests on.

    The reference names the pane, the moment, and the digest of the exact
    rows that decided the observation, so a later reader can re-capture or
    locate the same evidence in the pane's logs.  The screen itself is not
    journaled: it can carry conversation the operator considers private,
    and the digest is the proof that does not.
    """
    digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]
    moment = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return f"capture-pane:{pane_id}:{moment}:sha256:{digest}"


def _capture_rows(
    pane_id: str,
    screen: Optional[Callable[[], Sequence[str]]],
    *,
    timeout: float,
) -> Sequence[str]:
    if screen is not None:
        return screen()
    return capture_pane_screen(pane_id, timeout=timeout)


def await_compose_visible(
    pane_id: str,
    text: str,
    *,
    barrier: SubmissionBarrier,
    deadline_monotonic: Optional[float] = None,
    screen: Optional[Callable[[], Sequence[str]]] = None,
) -> bool:
    """Poll until the control text is compose-visible, or the settle expires.

    Called after the text write and before the one Enter.  A False return
    withholds the Enter: the barrier's whole guarantee is that the Enter
    fires only at a composer proven to be holding this control's text.
    Capture failures are retried within the bound rather than fatal — one
    transient tmux hiccup must not strand a healthy write — but a settle
    that expires without a positive sighting is a failed barrier, and the
    caller records it as the ambiguity it is.
    """
    settle_end = time.monotonic() + barrier.compose_settle_seconds
    if deadline_monotonic is not None:
        settle_end = min(settle_end, deadline_monotonic)
    while True:
        try:
            rows = _capture_rows(
                pane_id,
                screen,
                timeout=min(
                    _OBSERVATION_CAPTURE_TIMEOUT_SECONDS,
                    max(0.2, settle_end - time.monotonic()),
                ),
            )
        except NativePaneInputUnavailable:
            rows = ()
        if composed_text_visible(rows, text, composer_tail_rows=barrier.composer_tail_rows):
            return True
        remaining = settle_end - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(barrier.poll_interval_seconds, remaining))


def observe_submission(
    pane_id: str,
    text: str,
    *,
    barrier: SubmissionBarrier,
    deadline_monotonic: Optional[float] = None,
    screen: Optional[Callable[[], Sequence[str]]] = None,
) -> Tuple[str, Optional[str]]:
    """Classify what the composer did with the control after the one Enter.

    Returns ``(submission_observed, evidence_ref)``:

    - ``submitted`` — the text that was compose-visible before the Enter
      is gone from the composer region.  This is the provider-visible
      submission evidence: the composer gave the control up.  It is not,
      and never upgrades to, provider completion.
    - ``unsubmitted`` — the text persisted in the composer region through
      the whole post-Enter window: the positive observation that the
      Enter did not take.
    - ``unknown`` — no classification could be made: every capture failed,
      or the overall write deadline cut the window short.  Never read as
      "probably submitted".
    """
    observe_end = time.monotonic() + barrier.post_enter_seconds
    if deadline_monotonic is not None:
        observe_end = min(observe_end, deadline_monotonic)
    last_rows: Optional[Sequence[str]] = None
    while True:
        try:
            rows = _capture_rows(
                pane_id,
                screen,
                timeout=min(
                    _OBSERVATION_CAPTURE_TIMEOUT_SECONDS,
                    max(0.2, observe_end - time.monotonic()),
                ),
            )
        except NativePaneInputUnavailable:
            rows = None
        if rows is not None:
            last_rows = rows
            if not composed_text_visible(rows, text, composer_tail_rows=barrier.composer_tail_rows):
                return (SUBMISSION_SUBMITTED, submission_evidence_ref(pane_id, rows))
        remaining = observe_end - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(barrier.poll_interval_seconds, remaining))
    # The window closed without the composer ever giving the text up.
    # ``unsubmitted`` is a positive observation, so it requires two things:
    # a successful final capture still holding the text, and a window that
    # ran its full bound rather than being cut by the write deadline — a
    # shortened window proves nothing about what the composer did next.
    deadline_cut = deadline_monotonic is not None and time.monotonic() >= deadline_monotonic
    if last_rows is not None and not deadline_cut:
        return (SUBMISSION_UNSUBMITTED, submission_evidence_ref(pane_id, last_rows))
    return (SUBMISSION_UNKNOWN, None)
