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
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional, Sequence, Tuple

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


def capture_pane_screen_styled(
    pane_id: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> list[str]:
    """The pane's visible rows with SGR styling preserved (``-e``).

    The one consumer is the declared-command composer-emptiness proof for
    Claude (§4.1): Claude's composer draws a *placeholder* in an empty
    input box, and the placeholder is distinguishable from operator
    prefill only by its dim styling — the rendered text cells are
    identical otherwise.  The escape-free capture above cannot make that
    distinction, so this sibling exists.  It changes nothing for the
    status detectors, which keep reading the composited text.
    """
    result = _run(
        [_tmux_binary(), "capture-pane", "-e", "-p", "-t", pane_id],
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


def observe_codex_turn_state(
    pane_id: str,
    *,
    terminal_id: str,
    session_name: str,
    window_name: str,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    screen: Optional[Sequence[str]] = None,
) -> TerminalStatus:
    """Read the bound Codex TUI's current turn state from its rendered screen."""
    from cli_agent_orchestrator.providers.codex import CodexProvider

    rows = list(screen) if screen is not None else capture_pane_screen(pane_id, timeout=timeout)
    provider = CodexProvider(
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

#: Provider-pinned composer observation.  A barrier is needed wherever a
#: caller must distinguish "tmux accepted Enter" from "the provider consumed
#: the composer as a submitted turn."  Codex and Kimi both expose stable,
#: pinned composer regions that make that observation possible.  Claude keeps
#: its existing fused write because no equivalent region has been pinned.
#: A provider absent from the table gets that legacy behavior with no
#: submission observation claimed.
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
    # Kimi Code (pinned 0.29-0.30): the bottom five rows contain the three-row
    # prompt box followed by its model and context status rows.  A submitted
    # turn moves the echo above this region, while an Enter retained by the
    # composer leaves the control's unique suffix inside it.  The same bounded
    # timings as Codex cover the prompt_toolkit repaint without ever sending a
    # second Enter.
    "kimi_cli": SubmissionBarrier(
        compose_settle_seconds=3.0,
        post_enter_seconds=5.0,
        poll_interval_seconds=0.1,
        composer_tail_rows=5,
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


# --- Read-only composer observation (cond-0324) -----------------------------
#
# A conductor that has sent a control needs to know whether the exact text it
# expects is still resting in the provider's composer, without sending another
# byte.  This is a read-only observation of the pinned composer region, bound
# to the exact pane identity under the pane-input lease.  It reuses the same
# layout pins that make the submission barrier honest, but is build-exact:
# an unpinned provider/build advertises no observation capability at all.

_RULE_KIMI_COMPOSER_BOX = "kimi-composer-box"
_RULE_CLAUDE_PROMPT_BOX = "claude-prompt-box"
_RULE_CODEX_PROMPT_FOOTER = "codex-prompt-footer"


@dataclass(frozen=True)
class ComposerObservationPin:
    """How one provider build's composer is observed read-only.

    ``rule`` names the region-determination strategy (shared with the
    composer-emptiness pins).  ``composer_tail_rows`` is the bottom tail
    used for a quick positive sighting via :func:`composed_text_visible`.
    ``evidence`` records what build the pin was read from, so review can
    check it without re-walking the tree.
    """

    provider: str
    rule: str
    composer_tail_rows: int
    evidence: str


_CODEX_OBSERVATION_EVIDENCE = (
    "live-verified on the installed Codex CLI 0.146.0 (cond-0324): the last "
    "'›' prompt row and any wrapped rows before the following blank separator "
    "are the composer; an empty composer renders only a dim-styled rotating "
    "placeholder.  Observation reuses the same pinned region as the §4.1 "
    "composer-emptiness pin."
)

_KIMI_OBSERVATION_EVIDENCE = (
    "live-verified on the installed Kimi Code 0.29.2 (cond-0324): the composer "
    "is an untitled rounded box — '╭─╮', content rows framed by '│', a '> ' "
    "prompt, '╰─╯' — and an empty composer renders no placeholder.  Observation "
    "reuses the same pinned region as the §4.1 composer-emptiness pin."
)

_KIMI_0330_OBSERVATION_EVIDENCE = (
    "live-verified on the installed Kimi Code 0.33.0 (cond-0332, terminal "
    "1ca9d289): the rendered composer keeps the same untitled rounded box as "
    "0.29.2 — '╭─╮', content rows framed by '│', a '> ' prompt, '╰─╯' — in "
    "the bottom five rows of the native TUI pane.  The installed bundle "
    "(sha256 0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2) "
    "attests the build.  Observation reuses the same pinned region as the §4.1 "
    "composer-emptiness pin."
)


#: The per-provider+build composer observation pins.  A build appears here
#: only when its composer layout was read; an unpinned build refuses the
#: observation route rather than guessing at a region.
_COMPOSER_OBSERVATION_PINS: dict[str, dict[str, ComposerObservationPin]] = {
    "codex": {
        "0.146.0": ComposerObservationPin(
            provider="codex",
            rule=_RULE_CODEX_PROMPT_FOOTER,
            composer_tail_rows=4,
            evidence=_CODEX_OBSERVATION_EVIDENCE,
        ),
    },
    "kimi_cli": {
        "0.29.2": ComposerObservationPin(
            provider="kimi_cli",
            rule=_RULE_KIMI_COMPOSER_BOX,
            composer_tail_rows=5,
            evidence=_KIMI_OBSERVATION_EVIDENCE,
        ),
        "0.33.0": ComposerObservationPin(
            provider="kimi_cli",
            rule=_RULE_KIMI_COMPOSER_BOX,
            composer_tail_rows=5,
            evidence=_KIMI_0330_OBSERVATION_EVIDENCE,
        ),
    },
}


def composer_observation_pin_for(
    provider: Optional[str], provider_version: Optional[str]
) -> Optional[ComposerObservationPin]:
    """The pinned read-only observation for this exact build, or None.

    None means "no observation is proven for this provider/build" — the
    caller refuses the route rather than guessing at a composer region.
    The version is normalized the same way the other per-build tables do,
    so all build-exact decisions agree on which build a request names.
    """
    if not provider or not provider_version:
        return None
    from cli_agent_orchestrator.services import provider_contracts

    return _COMPOSER_OBSERVATION_PINS.get(provider, {}).get(
        provider_contracts.normalized_version(provider_version)
    )


# Composer chrome is removed only at the pinned frame boundaries below.  Do
# not remove arbitrary prompt-looking characters from the payload: a valid
# control may contain them, and doing so would make two different payloads
# hash alike.
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


# --- Declared-command composer-emptiness proof (§4.1) -----------------------
#
# A declared command-class control must never concatenate with queued
# composer prefill: the r5 evidence is a provider command appended to a
# prefill that survived Escape and submitted as one ordinary prompt.  The
# guard therefore proves the composer *empty* — under the pane-input
# lease, before the first command byte — through a per-provider+build
# pinned determination, with the same evidence discipline as the
# submission-barrier table above.  Three answers, never a guess:
#
# - ``True``  — the composer is proven empty; the command may be written.
# - ``False`` — the composer is proven to hold content.
# - ``None``  — emptiness could not be proven (unparseable or unreadable
#   region, failed capture).  "Could not look" is not "empty".
#
# Both negative answers surface as the typed zero-byte ``composer-nonempty``
# refusal; a provider/build with no pin is ``provider-unsupported`` instead.
# The pins are live-verified per build in §10.3.  Blind clearing is
# prohibited: nothing here ever sends a keystroke.


@dataclass(frozen=True)
class ComposerEmptinessPin:
    """How one provider build's composer is proven empty.

    ``rule`` names the region determination (each build family draws its
    composer differently); ``styled`` whether the proof needs the
    escape-preserving capture (a placeholder is only distinguishable from
    prefill by its dim styling).  ``evidence`` records what the pin was
    read from, so review can check it without re-walking the tree.
    """

    provider: str
    rule: str
    styled: bool
    evidence: str


_KIMI_EMPTY_EVIDENCE = (
    "composer layout live-verified on the installed Kimi Code 0.29.2 "
    "(§10.3, 2026-07-28, evidence lane-a-10.3 cases 07/08): the composer is "
    "an untitled rounded box — '╭─╮', content rows framed by '│', a '> ' "
    "prompt, '╰─╯' — and an empty composer renders no placeholder (only the "
    "inverse-cursor cell after the prompt), so any content row is prefill; "
    "the installed bundle (sha256 2ee6e2f1…) contains no '── input ──' "
    "rendering — an earlier pin read from older in-tree fixtures was "
    "corrected by this live evidence, never approximated"
)

_CLAUDE_EMPTY_EVIDENCE = (
    "composer layout read from the Claude Code 2.1.220 composer (in-tree "
    "fixtures, test_claude_code_unit.py) and live-verified on the installed "
    "build (§10.3, 2026-07-28, evidence lane-a-10.3 cases 11/12): the prompt "
    "box is two '─' rules framing a '❯' prompt row; an empty composer shows "
    "only a dim-styled placeholder (SGR 2; the cursor cell inverse), so "
    "placeholder cells are told from prefill by styling, never by matching "
    "the rotating placeholder text; prefill was observed to survive one "
    "Escape on this build (the r5 evidence generalizes)"
)

_CODEX_EMPTY_EVIDENCE = (
    "composer layout live-verified on the installed Codex CLI 0.146.0 "
    "(§10.3, 2026-07-30, cond-0222 native-TUI canaries): the last '›' "
    "prompt row and any wrapped rows before the following blank separator "
    "are the composer; an empty composer renders only a dim-styled rotating "
    "placeholder (SGR 2), while typed /compact renders in normal video. "
    "The prompt glyph itself is bold and excluded, and footer/suggestion "
    "rows below the blank separator are excluded, so neither can prove "
    "content"
)


#: The per-provider+build emptiness pins, keyed by normalized version.
#: A build appears here only when its composer layout was read; an
#: unpinned build refuses command-class controls ``provider-unsupported``
#: rather than guessing at a region.  kimi 0.29.0/0.29.1 are deliberately
#: absent: the only live-verified Kimi composer is 0.29.2's, and pinning
#: their region from anything less would be the approximation this table
#: exists to prevent (§4.1 — they refuse honestly until live-verified).
_COMPOSER_EMPTINESS_PINS: dict[str, dict[str, ComposerEmptinessPin]] = {
    "kimi_cli": {
        "0.29.2": ComposerEmptinessPin(
            provider="kimi_cli",
            rule=_RULE_KIMI_COMPOSER_BOX,
            styled=False,
            evidence=_KIMI_EMPTY_EVIDENCE,
        ),
    },
    "claude_code": {
        "2.1.220": ComposerEmptinessPin(
            provider="claude_code",
            rule=_RULE_CLAUDE_PROMPT_BOX,
            styled=True,
            evidence=_CLAUDE_EMPTY_EVIDENCE,
        ),
    },
    "codex": {
        "0.146.0": ComposerEmptinessPin(
            provider="codex",
            rule=_RULE_CODEX_PROMPT_FOOTER,
            styled=True,
            evidence=_CODEX_EMPTY_EVIDENCE,
        ),
    },
}


def composer_emptiness_pin_for(
    provider: Optional[str], provider_version: Optional[str]
) -> Optional[ComposerEmptinessPin]:
    """The pinned emptiness determination for this exact build, or None.

    None means "no determination is proven for this provider/build" — the
    caller refuses ``provider-unsupported`` rather than guessing.  The
    version is normalized the same way the adapters' composer pins
    normalize theirs, so all per-build tables agree about which build a
    request names.
    """
    if not provider or not provider_version:
        return None
    from cli_agent_orchestrator.services import provider_contracts

    return _COMPOSER_EMPTINESS_PINS.get(provider, {}).get(
        provider_contracts.normalized_version(provider_version)
    )


# The Kimi Code composer's own frame (live-verified 0.29.2): an untitled
# rounded box — a '╭─╮' top rule, content rows framed by '│', a '> '
# prompt, and a '╰─╯' bottom rule.  The composer is always the LAST such
# box on screen; the status bar below it is not boxed.  Menu and dialog
# boxes share the frame, so the prompt glyph is what makes the box a
# composer: without it the region proves nothing.
_KIMI_COMPOSER_BOX_TOP = re.compile(r"^\s*╭─+╮\s*$")
_KIMI_COMPOSER_BOX_BOTTOM = re.compile(r"^\s*╰─+╯\s*$")
_KIMI_COMPOSER_BOX_CONTENT = re.compile(r"^\s*│(.*)│\s*$")


def _kimi_composer_box_rows(rows: Sequence[str]) -> Optional[List[str]]:
    """The inner text rows of the last rounded composer box, or None."""
    bottom = None
    for index in range(len(rows) - 1, -1, -1):
        if _KIMI_COMPOSER_BOX_BOTTOM.match(rows[index]):
            bottom = index
            break
    if bottom is None:
        return None
    top = None
    for index in range(bottom - 1, -1, -1):
        if _KIMI_COMPOSER_BOX_TOP.match(rows[index]):
            top = index
            break
    if top is None:
        return None
    content: List[str] = []
    for row in rows[top + 1 : bottom]:
        match = _KIMI_COMPOSER_BOX_CONTENT.match(row)
        if match is None:
            return None
        content.append(match.group(1))
    return content


def _kimi_composer_empty(rows: Sequence[str]) -> Optional[bool]:
    """Empty iff the composer's content after the '>' prompt is blank.

    The pinned build renders no placeholder in an empty composer (the
    live capture shows only the cursor cell after the prompt), so any
    non-blank content row is prefill.  The first content row must carry
    the '>' prompt glyph — a rounded box without it is a menu or dialog,
    and an unidentified box is unproven, never "probably empty".
    """
    content = _kimi_composer_box_rows(rows)
    if not content:
        return None
    first = content[0].lstrip()
    if not first.startswith(">"):
        return None
    for index, row in enumerate(content):
        text = row.lstrip()[1:] if index == 0 else row
        if text.strip():
            return False
    return True


# The Claude composer's frame: two horizontal rules framing the '❯'
# prompt row.  The rules also appear between transcript entries, so the
# region is the *last* rule pair on screen.
_CLAUDE_BOX_RULE = re.compile(r"^\s*─{3,}\s*$")
_CLAUDE_PROMPT_GLYPH = "❯"


def _parse_styled_rows(rows: Sequence[str]) -> Optional[List[List[Tuple[str, bool, bool]]]]:
    """Each row as ``(char, dim, inverse)`` cells, or None when unparseable.

    A minimal SGR tracker over the escape-preserving capture: ``dim``
    follows SGR 2/22 and ``inverse`` SGR 7/27; color and other parameters
    are tracked past without effect, OSC sequences are skipped, and state
    carries across rows the way the renderer's does.  Anything that is
    not a well-formed escape sequence makes the whole capture unproven —
    a guessed styling state could read prefill as placeholder, which is
    the one direction this proof must never err in.
    """
    dim = False
    inverse = False
    parsed_rows: List[List[Tuple[str, bool, bool]]] = []
    for row in rows:
        cells: List[Tuple[str, bool, bool]] = []
        index = 0
        while index < len(row):
            char = row[index]
            if char == "\x1b":
                if row.startswith("\x1b[", index):
                    end = index + 2
                    while end < len(row) and not ("\x40" <= row[end] <= "\x7e"):
                        end += 1
                    if end >= len(row):
                        return None
                    final = row[end]
                    if final == "m":
                        params = row[index + 2 : end].split(";")
                        for param in params:
                            if param in ("", "0"):
                                dim = False
                                inverse = False
                            elif param == "2":
                                dim = True
                            elif param == "22":
                                dim = False
                            elif param == "7":
                                inverse = True
                            elif param == "27":
                                inverse = False
                            # Color and other parameters leave dim/inverse
                            # untouched; they do not distinguish prefill.
                    index = end + 1
                    continue
                if row.startswith("\x1b]", index):
                    # OSC: runs to BEL or ST.  Content (titles, hyperlinks)
                    # is not composer cells.
                    bel = row.find("\x07", index + 2)
                    st = row.find("\x1b\\", index + 2)
                    ends = [pos for pos in (bel, st) if pos != -1]
                    if not ends:
                        return None
                    terminator = min(ends)
                    index = terminator + (2 if terminator == st else 1)
                    continue
                # Any other escape form: the styling state is unknowable.
                return None
            cells.append((char, dim, inverse))
            index += 1
        parsed_rows.append(cells)
    return parsed_rows


def _claude_composer_empty(styled_rows: Sequence[str]) -> Optional[bool]:
    """Empty iff the prompt box holds only dim/inverse-styled content.

    An empty Claude composer shows a *placeholder*: dim-styled suggestion
    text whose first cell sits under the inverse-video cursor.  Operator
    prefill renders in normal video.  The proof therefore never pattern-
    matches the placeholder text (the pool rotates and is unknowable); it
    reads styling: any normally-styled, non-whitespace cell after the
    prompt glyph is content, and whitespace carries no content either way.
    """
    parsed = _parse_styled_rows(styled_rows)
    if parsed is None:
        return None
    plain = ["".join(char for char, _, _ in row) for row in parsed]
    rule_indices = [i for i, text in enumerate(plain) if _CLAUDE_BOX_RULE.match(text)]
    if len(rule_indices) < 2:
        return None
    content = parsed[rule_indices[-2] + 1 : rule_indices[-1]]
    if not content:
        # No prompt row between the rules: this is not a prompt box.
        return None
    first = content[0]
    prompt_at = next(
        (i for i, (char, _, _) in enumerate(first) if char == _CLAUDE_PROMPT_GLYPH), None
    )
    if prompt_at is None:
        return None
    cells = [cell for cell in first[prompt_at + 1 :]] + [
        cell for row in content[1:] for cell in row
    ]
    for char, dim, inverse in cells:
        if char.isspace():
            continue
        if not (dim or inverse):
            return False
    return True


def _codex_composer_empty(styled_rows: Sequence[str]) -> Optional[bool]:
    """Empty iff the last Codex prompt region contains only placeholder cells.

    Codex draws transcript prompts and the live composer with the same ``›``
    glyph.  The live composer is the last such row and ends at the first
    blank row below it; the footer or slash-command suggestion begins after
    that separator.  Within the region, the live-verified build distinguishes
    its rotating placeholder from prefill by styling, not text: placeholder
    cells are dim, while typed cells are normal video.
    """
    parsed = _parse_styled_rows(styled_rows)
    if parsed is None:
        return None
    plain = ["".join(char for char, _, _ in row) for row in parsed]
    prompt_row = next(
        (index for index in range(len(plain) - 1, -1, -1) if "›" in plain[index]),
        None,
    )
    if prompt_row is None:
        return None
    separator = next(
        (index for index in range(prompt_row + 1, len(plain)) if not plain[index].strip()),
        None,
    )
    if separator is None:
        return None
    prompt_at = next(
        (index for index, (char, _, _) in enumerate(parsed[prompt_row]) if char == "›"),
        None,
    )
    if prompt_at is None:
        return None
    cells = parsed[prompt_row][prompt_at + 1 :] + [
        cell for row in parsed[prompt_row + 1 : separator] for cell in row
    ]
    for char, dim, inverse in cells:
        if char.isspace():
            continue
        if not (dim or inverse):
            return False
    return True


def _kimi_composer_text(
    rows: Sequence[str], *, expected_text_bytes: Optional[int] = None
) -> Optional[str]:
    """Exact text in a single, unambiguous Kimi composer row.

    Kimi right-pads the content row to the box width.  Without an expected
    byte count, only the historical one-padding-cell shape is unambiguous.
    A digest-bound observation already carries the original UTF-8 byte count;
    use that count to separate the payload from any amount of box padding,
    then let the caller's digest comparison prove the content itself.
    """
    content = _kimi_composer_box_rows(rows)
    if content is None or len(content) != 1:
        return None
    row = content[0]
    prompt_at = row.find(">")
    if prompt_at < 0:
        return None
    payload = row[prompt_at + 1 :]
    if payload.startswith(" "):
        payload = payload[1:]
    if expected_text_bytes is not None:
        encoded = payload.encode("utf-8")
        if len(encoded) < expected_text_bytes:
            return None
        candidate_bytes = encoded[:expected_text_bytes]
        padding = encoded[expected_text_bytes:]
        if padding.strip(b" "):
            return None
        try:
            candidate = candidate_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return None
        # A shorter value plus frame padding renders identically to an
        # expected value ending in spaces.  The digest cannot resolve that
        # display ambiguity, so keep the historical fail-closed posture.
        return None if candidate.endswith(" ") else candidate
    # The pinned box contributes one right-padding cell.  A second trailing
    # cell would be indistinguishable from user-supplied trailing whitespace,
    # so reject that ambiguous case instead of hashing a guess.
    if payload.endswith(" "):
        payload = payload[:-1]
    if payload != payload.strip():
        return None
    return payload


def _claude_composer_text(rows: Sequence[str]) -> Optional[str]:
    """Exact text in a single, unambiguous Claude composer row."""
    rule_indices = [i for i, text in enumerate(rows) if _CLAUDE_BOX_RULE.match(text)]
    if len(rule_indices) < 2:
        return None
    content = rows[rule_indices[-2] + 1 : rule_indices[-1]]
    if len(content) != 1:
        return None
    first = content[0]
    prompt_at = first.find(_CLAUDE_PROMPT_GLYPH)
    if prompt_at == -1:
        return None
    payload = first[prompt_at + 1 :]
    if payload.startswith(" "):
        payload = payload[1:]
    return payload if payload == payload.strip() else None


def _codex_composer_text(rows: Sequence[str]) -> Optional[str]:
    """Exact text in a single, unambiguous Codex prompt row."""
    prompt_row = next(
        (index for index in range(len(rows) - 1, -1, -1) if "›" in rows[index]),
        None,
    )
    if prompt_row is None:
        return None
    separator = next(
        (index for index in range(prompt_row + 1, len(rows)) if not rows[index].strip()),
        None,
    )
    if separator is None:
        return None
    content = rows[prompt_row:separator]
    if len(content) != 1:
        return None
    first = content[0]
    prompt_at = first.find("›")
    if prompt_at == -1:
        return None
    payload = first[prompt_at + 1 :]
    if payload.startswith(" "):
        payload = payload[1:]
    return payload if payload == payload.strip() else None


def extract_composer_text(
    rows: Sequence[str],
    pin: ComposerObservationPin,
    *,
    expected_text_bytes: Optional[int] = None,
) -> Optional[str]:
    """The exact text held in the pinned composer region, or ``None``.

    Only an unwrapped, one-row payload with unambiguous frame padding is
    returned.  Kimi observations may use the caller's already-validated byte
    count to distinguish payload from its variable-width box padding; the
    caller still verifies the raw UTF-8 digest.  Wrapped or otherwise
    whitespace-ambiguous captures fail closed.
    """
    if pin.rule == _RULE_KIMI_COMPOSER_BOX:
        return _kimi_composer_text(rows, expected_text_bytes=expected_text_bytes)
    if pin.rule == _RULE_CLAUDE_PROMPT_BOX:
        return _claude_composer_text(rows)
    if pin.rule == _RULE_CODEX_PROMPT_FOOTER:
        return _codex_composer_text(rows)
    return None


def observe_composer_empty(
    pane_id: str,
    pin: ComposerEmptinessPin,
    *,
    deadline_monotonic: Optional[float] = None,
    screen: Optional[Callable[[], Sequence[str]]] = None,
) -> Optional[bool]:
    """Whether the composer in ``pane_id`` is proven empty, under the pin.

    One capture, one determination, no keystrokes — the guard observes
    and refuses, it never "prepares" the composer.  A failed capture or an
    unparseable region is ``None`` (unproven), which the caller treats
    exactly like proven-non-empty: a typed zero-byte refusal.
    """
    timeout = _OBSERVATION_CAPTURE_TIMEOUT_SECONDS
    if deadline_monotonic is not None:
        timeout = max(0.2, min(timeout, deadline_monotonic - time.monotonic()))
    if screen is not None:
        rows: Sequence[str] = screen()
    elif pin.styled:
        rows = capture_pane_screen_styled(pane_id, timeout=timeout)
    else:
        rows = capture_pane_screen(pane_id, timeout=timeout)
    if pin.rule == _RULE_KIMI_COMPOSER_BOX:
        return _kimi_composer_empty(rows)
    if pin.rule == _RULE_CLAUDE_PROMPT_BOX:
        return _claude_composer_empty(rows)
    if pin.rule == _RULE_CODEX_PROMPT_FOOTER:
        return _codex_composer_empty(rows)
    # A rule this build does not know proves nothing.
    return None


# --- Declared-command execution observation (§4.1, r11 two-close rule) -------
#
# Transport acceptance is not command execution: a declared command is
# closed ``accepted`` only when the provider/build-pinned execution signal
# is observed on the pane within a bounded post-write window, with the
# evidence reference journaled.  Otherwise the close is ``ambiguous`` with
# the deployed ``submission-unproven`` reason — terminal for automation,
# never a retry licence, never a forced completion.  The signals below are
# the ones live-proven on the pinned builds (§10.3, evidence lane-a-10.3);
# a provider/build without one refuses declared commands
# ``provider-unsupported`` pre-write — both pins are required, never one.

_RULE_KIMI_COMPACTION_SIGNAL = "kimi-compaction-signal"
_RULE_CLAUDE_COMMAND_ECHO_RESPONSE = "claude-command-echo-response"
_RULE_CODEX_COMPACTION_SIGNAL = "codex-compaction-signal"


@dataclass(frozen=True)
class CommandExecutionPin:
    """How one provider build's declared-command execution is observed.

    ``rule`` names the signal matcher; ``signal`` is the short human name
    for it (used in refusal/ambiguous detail strings); ``styled`` whether
    the observation needs the escape-preserving capture (the composer
    end-check shares the emptiness pin's need).  ``evidence`` records what
    the pin was read from, per the same discipline as the emptiness pins.
    """

    provider: str
    rule: str
    signal: str
    styled: bool
    evidence: str


_KIMI_EXECUTION_EVIDENCE = (
    "live-proven on the installed Kimi Code 0.29.2 (§10.3, 2026-07-28, "
    "evidence lane-a-10.3 case-08): a declared /compact drives the "
    "compaction notice ' ● Compacting context...' in the transcript; the "
    "occurrence count over a pre-write baseline distinguishes this "
    "command's notice from any earlier compaction's"
)

_CLAUDE_EXECUTION_EVIDENCE = (
    "live-proven on the installed Claude Code 2.1.220 (§10.3, 2026-07-28, "
    "evidence lane-a-10.3 case-12): a declared command's own UI is the "
    "'❯ <command>' echo row followed by a '⎿' response row (an ordinary "
    "prompt echo draws no '⎿'); the pair count over a pre-write baseline "
    "distinguishes this command's pair from any earlier one"
)

_CODEX_EXECUTION_EVIDENCE = (
    "live-proven on the installed Codex CLI 0.146.0 (§10.3, 2026-07-30, "
    "cond-0222 native-TUI canary): an idle declared /compact starts a real "
    "Codex task and terminates with the transcript notice '• Context "
    "compacted'; the occurrence count over a pre-write baseline "
    "distinguishes this command's completion from any earlier compaction"
)


#: The per-provider+build execution pins.  Same scope as the emptiness
#: pins — a build appears only with live evidence; an unpinned build
#: refuses declared commands ``provider-unsupported`` pre-write.
_COMMAND_EXECUTION_PINS: dict[str, dict[str, CommandExecutionPin]] = {
    "kimi_cli": {
        "0.29.2": CommandExecutionPin(
            provider="kimi_cli",
            rule=_RULE_KIMI_COMPACTION_SIGNAL,
            signal="the compaction notice/UI",
            styled=False,
            evidence=_KIMI_EXECUTION_EVIDENCE,
        ),
    },
    "claude_code": {
        "2.1.220": CommandExecutionPin(
            provider="claude_code",
            rule=_RULE_CLAUDE_COMMAND_ECHO_RESPONSE,
            signal="the command echo plus its response region",
            styled=True,
            evidence=_CLAUDE_EXECUTION_EVIDENCE,
        ),
    },
    "codex": {
        "0.146.0": CommandExecutionPin(
            provider="codex",
            rule=_RULE_CODEX_COMPACTION_SIGNAL,
            signal="the context-compacted notice",
            styled=True,
            evidence=_CODEX_EXECUTION_EVIDENCE,
        ),
    },
}


def command_execution_pin_for(
    provider: Optional[str], provider_version: Optional[str]
) -> Optional[CommandExecutionPin]:
    """The pinned execution observation for this exact build, or None.

    None means "no execution signal is proven for this provider/build" —
    the caller refuses declared commands ``provider-unsupported`` pre-write
    rather than closing on transport facts alone.
    """
    if not provider or not provider_version:
        return None
    from cli_agent_orchestrator.services import provider_contracts

    return _COMMAND_EXECUTION_PINS.get(provider, {}).get(
        provider_contracts.normalized_version(provider_version)
    )


# The live-proven compaction notice on kimi 0.29.2.
_KIMI_COMPACTION_NOTICE = "Compacting context"
_CLAUDE_RESPONSE_GLYPH = "⎿"
_CODEX_COMPACTION_NOTICE = "Context compacted"


def _execution_signal_count(
    pin: CommandExecutionPin, rows: Sequence[str], command_text: str
) -> int:
    """Occurrences of the pinned execution signal in one capture.

    Counted rather than matched once, and compared against a pre-write
    baseline by the caller, so a stale signal from an earlier command in
    the same session can never be read as this command's execution.
    """
    if pin.rule == _RULE_KIMI_COMPACTION_SIGNAL:
        return sum(1 for row in rows if _KIMI_COMPACTION_NOTICE in row)
    if pin.rule == _RULE_CLAUDE_COMMAND_ECHO_RESPONSE:
        parsed = _parse_styled_rows(rows)
        if parsed is None:
            return 0
        plain = ["".join(char for char, _, _ in row) for row in parsed]
        pairs = 0
        for index, text in enumerate(plain):
            if text.strip() == f"❯ {command_text}" and any(
                later.lstrip().startswith(_CLAUDE_RESPONSE_GLYPH) for later in plain[index + 1 :]
            ):
                pairs += 1
        return pairs
    if pin.rule == _RULE_CODEX_COMPACTION_SIGNAL:
        parsed = _parse_styled_rows(rows)
        if parsed is None:
            return 0
        plain = ["".join(char for char, _, _ in row) for row in parsed]
        return sum(1 for row in plain if _CODEX_COMPACTION_NOTICE in row)
    return 0


# The observation polls the pane at this interval until the signal appears
# or the remaining write deadline closes the window.
_EXECUTION_POLL_SECONDS = 0.25


def capture_execution_rows(
    pane_id: str,
    pin: CommandExecutionPin,
    *,
    deadline_monotonic: Optional[float] = None,
) -> List[str]:
    """One escape-form capture for the execution observation, per the pin.

    Styled when the pin needs styling (the composer end-check shares the
    emptiness determination's need), plain otherwise; the timeout is
    bounded by the remaining write deadline so the observation always fits
    inside it.
    """
    timeout = _OBSERVATION_CAPTURE_TIMEOUT_SECONDS
    if deadline_monotonic is not None:
        timeout = max(0.2, min(timeout, deadline_monotonic - time.monotonic()))
    if pin.styled:
        return capture_pane_screen_styled(pane_id, timeout=timeout)
    return capture_pane_screen(pane_id, timeout=timeout)


def observe_command_execution(
    pane_id: str,
    pin: CommandExecutionPin,
    *,
    command_text: str,
    composer_pin: ComposerEmptinessPin,
    baseline_rows: Optional[Sequence[str]] = None,
    deadline_monotonic: Optional[float] = None,
    screen: Optional[Callable[[], Sequence[str]]] = None,
) -> Tuple[str, Optional[str]]:
    """The two-close observation for one written declared command.

    Returns ``(submission_observed, evidence_ref)``:

    - ``submitted`` — the pinned execution signal's occurrence count rose
      above the pre-write baseline within the bounded window: the
      command's own UI/effect was observed.  This is the only shape that
      may close ``accepted``, and the evidence ref travels with it.
    - ``unsubmitted`` — the window expired and the composer still holds
      content (the emptiness determination on the final capture): the
      command provably never submitted.
    - ``unknown`` — no classification could be made (every capture failed,
      or the text left the composer without the signal appearing).  Never
      read as "probably executed".
    """
    baseline_count = (
        _execution_signal_count(pin, baseline_rows, command_text)
        if baseline_rows is not None
        else 0
    )
    last_rows: Optional[Sequence[str]] = None
    while True:
        if screen is not None:
            rows: Optional[Sequence[str]] = screen()
        else:
            try:
                rows = capture_execution_rows(pane_id, pin, deadline_monotonic=deadline_monotonic)
            except NativePaneInputUnavailable:
                rows = None
        if rows is not None:
            last_rows = rows
            # The deadline boundary is authoritative *after* the capture
            # returns: the capture timeout is floored, so a capture begun
            # just before expiry can complete after it, and a signal seen
            # only then was never proven inside the bounded window.  Only
            # an on-time capture may close ``submitted``; a late one can
            # still inform the resting-text check below, which never
            # licenses acceptance.
            on_time = deadline_monotonic is None or time.monotonic() <= deadline_monotonic
            if on_time and _execution_signal_count(pin, rows, command_text) > baseline_count:
                return (SUBMISSION_SUBMITTED, submission_evidence_ref(pane_id, rows))
        if deadline_monotonic is None or time.monotonic() >= deadline_monotonic:
            break
        time.sleep(min(_EXECUTION_POLL_SECONDS, max(0.0, deadline_monotonic - time.monotonic())))
    if last_rows is not None:
        still = observe_composer_empty(pane_id, composer_pin, screen=lambda: last_rows)
        if still is False:
            return (SUBMISSION_UNSUBMITTED, submission_evidence_ref(pane_id, last_rows))
    return (SUBMISSION_UNKNOWN, None)
