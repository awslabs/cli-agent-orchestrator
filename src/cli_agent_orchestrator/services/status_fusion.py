"""One terminal status, fused from every signal CAO already has.

The problem this solves is measured, not hypothetical. On a live fleet of 36
terminals: 15 reported ``dead``, 20 reported ``not_fifo_monitored``, and
exactly **one** reported a status anybody could act on. The dashboard rendered
those 20 as "Managed Live" in the info palette — they looked healthy — and two
of them had been showing a *processing* screen for three and a half days.

Every signal needed to do better was already inside the process, just never
joined:

===================  ==========================================================
``fifo``             the StatusMonitor's stream classification. Exists only
                     where a FIFO is attached, which by construction excludes
                     every native TUI (``terminal_projection``:
                     ``fifo_monitored = not native_tui``).
``screen``           ``provider.get_status_from_screen(...)``, whose own
                     docstring asks for "a fixed-height list of viewport rows
                     with all cursor moves and in-place redraws already
                     resolved, escape-free, right-padded with spaces" — which
                     is precisely what ``tmux capture-pane -p`` returns,
                     because tmux is already the terminal emulator. It needs no
                     FIFO. It was never called.
``liveness``         whether the pane's rendered content changed between two
                     observations. One bit, and on its own nearly worthless:
                     all 15 live panes were byte-identical across a 60s window,
                     so a diff-only detector would have called every one of
                     them idle, including the two that were wedged.
``activity``         seconds since ``last_active``.
``lifecycle``        pane present / absent / superseded. Already computed.
===================  ==========================================================

Why fusion rather than a better single detector
-----------------------------------------------

Because the state that matters most is not visible to any signal alone.

A terminal whose screen says ``processing``, whose pane has not changed a byte
in days, and whose ``last_active`` is ancient, is **wedged** — the TUI is
painted mid-work and nothing is driving it. ``screen`` alone calls that
healthy processing. ``liveness`` alone calls it idle. ``activity`` alone cannot
tell it from a legitimately parked worker. Only the join says "claims to be
working, demonstrably is not", and that is exactly the state an unattended
orchestrator most needs to surface.

What this module will not do
----------------------------

It reports a **status**, and status is operational evidence. It is not durable
data: nothing here may become a callback, a completion proof, a merge gate, or
a token accounting. The repository records that boundary as the durable-data
bright line after a TUI-scraping proposal, and fusing more signals does not
move it — a fused status is still a reading of a screen, and a screen can lie
about anything a model chose to render.

Known limitation: the wedge needs a warm observer
--------------------------------------------------

``liveness`` is a duration measured between two observations, so on the first
ever sight of a pane there is no duration and the wedge join cannot fire. A
process that has just started therefore cannot retroactively know that a pane
has been frozen for three days; it learns that one resample interval later.

This is deliberate rather than papered over. The tempting fix is to seed the
liveness clock from ``last_active``, which would make a cold start able to
accuse immediately — but those are two different clocks measuring two
different things (rendered output versus orchestrator-observed activity), and
the entire value of the wedge join is that it requires two INDEPENDENT sources
to agree. Seeding one from the other would collapse the join into a single
signal wearing two names, and the first false accusation would be
indistinguishable from a true one.

``UNKNOWN`` beats confidently wrong, everywhere. A signal that is absent,
stale, or unreadable degrades the answer it would have decided; it never falls
through to a more confident one. Every fused result carries the signals it saw
and the reason it chose, so a caller that disagrees can see exactly which input
to distrust.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)

#: How long a pane may show a ``processing`` screen with no rendered change
#: before the fusion is willing to call it wedged.
#:
#: Deliberately generous. A model thinking without emitting looks exactly like
#: a wedged one for as long as it thinks, and the cost of the two errors is not
#: symmetric: calling a thinking worker wedged invites an operator to kill live
#: work, while waiting another twenty minutes on a genuinely wedged one costs
#: twenty minutes. The measured wedge that motivated this ran for 3.5 DAYS, so
#: nothing is lost by being slow to accuse.
WEDGED_QUIET_SECONDS = 20 * 60

#: A wedge claim additionally requires the terminal to have been silent this
#: long overall. Two independent clocks must agree before the accusation is
#: made — the rendered-content clock and the orchestrator's own activity clock.
WEDGED_INACTIVE_SECONDS = 30 * 60

#: Statuses that assert the agent is actively working. Only these can be
#: contradicted into a wedge; an idle or completed screen that stops changing
#: is simply idle, which is the truth.
_WORKING_STATUSES = frozenset({TerminalStatus.PROCESSING})

#: Lifecycle values that answer the question by themselves. A row whose pane is
#: gone has no provider state, and inventing one is what produced a wall of
#: phantom cards that looked like workers waiting to be talked to.
_TERMINAL_LIFECYCLES = {
    "dead": "dead",
    "superseded": "superseded",
    "unknown-liveness": "unknown-liveness",
}


@dataclass(frozen=True)
class Signal:
    """One observation, with why it should or should not be believed."""

    name: str
    #: The value observed, or None when the signal could not be taken.
    value: Optional[Any] = None
    #: `available` | `absent` | `unreadable`. `absent` is a legitimate,
    #: expected answer (no FIFO on a native TUI); `unreadable` is a failure to
    #: observe something that should have been observable, and the two must
    #: not be conflated — one is architecture, the other is a fault.
    state: str = "absent"
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"name": self.name, "state": self.state}
        if self.value is not None:
            out["value"] = (
                self.value.value if isinstance(self.value, TerminalStatus) else self.value
            )
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class FusedStatus:
    """The fused answer plus everything needed to audit it."""

    status: TerminalStatus
    #: `high` when a calibrated classifier decided it; `medium` when a single
    #: uncorroborated reading did; `low` when only weak evidence was available;
    #: `none` when nothing was.
    confidence: str
    reason: str
    signals: List[Signal] = field(default_factory=list)
    #: True only for the join no single signal can make.
    wedged: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "confidence": self.confidence,
            "reason": self.reason,
            "wedged": self.wedged,
            "signals": [s.to_dict() for s in self.signals],
        }


def _as_status(value: Any) -> Optional[TerminalStatus]:
    if isinstance(value, TerminalStatus):
        return value
    if isinstance(value, str):
        try:
            return TerminalStatus(value)
        except ValueError:
            return None
    return None


def fuse(
    *,
    lifecycle: Optional[str],
    fifo: Signal,
    screen: Signal,
    liveness: Signal,
    activity: Signal,
    wedged_quiet_seconds: int = WEDGED_QUIET_SECONDS,
    wedged_inactive_seconds: int = WEDGED_INACTIVE_SECONDS,
) -> FusedStatus:
    """Fuse observations into one status. PURE — no I/O, no clock.

    Purity is what makes the precedence rules testable at all: every input is
    a value, so a test states the fleet condition it means to pin instead of
    building a tmux server to imply it.

    Precedence, most decisive first:

    1. **lifecycle** — a pane that is gone or superseded has no provider state.
    2. **wedge join** — a working claim contradicted by two independent
       quiet clocks. Checked BEFORE the classifiers are believed, because the
       whole point is that the classifier is the thing being contradicted.
    3. **fifo** — the stream classifier, when it ran. It sees the byte stream
       rather than a composited frame, so it is not subject to a redraw having
       evicted the marker it needed.
    4. **screen** — the same per-provider detector over a settled viewport.
    5. **liveness alone** — cannot name a status, only refuse to invent one.
    """
    signals = [fifo, screen, liveness, activity]

    resolved = _TERMINAL_LIFECYCLES.get((lifecycle or "").strip())
    if resolved is not None:
        return FusedStatus(
            status=TerminalStatus.UNKNOWN,
            confidence="high",
            reason=f"lifecycle is {resolved!r}: the pane is not there to have a status",
            signals=signals,
        )

    fifo_status = _as_status(fifo.value) if fifo.state == "available" else None
    screen_status = _as_status(screen.value) if screen.state == "available" else None
    claimed = fifo_status or screen_status

    quiet_for = liveness.value if liveness.state == "available" else None
    inactive_for = activity.value if activity.state == "available" else None

    if (
        claimed in _WORKING_STATUSES
        and isinstance(quiet_for, (int, float))
        and quiet_for >= wedged_quiet_seconds
        and isinstance(inactive_for, (int, float))
        and inactive_for >= wedged_inactive_seconds
    ):
        return FusedStatus(
            status=TerminalStatus.UNKNOWN,
            confidence="high",
            reason=(
                f"screen claims {claimed.value!r} but the pane has rendered "
                f"nothing new for {int(quiet_for)}s and the terminal has been "
                f"inactive for {int(inactive_for)}s: two independent clocks "
                "contradict the claim"
            ),
            signals=signals,
            wedged=True,
        )

    if fifo_status is not None:
        return FusedStatus(
            status=fifo_status,
            confidence="high",
            reason="classified from the output stream by the provider's own detector",
            signals=signals,
        )

    if screen_status is not None:
        corroborated = isinstance(quiet_for, (int, float))
        return FusedStatus(
            status=screen_status,
            confidence="high" if corroborated else "medium",
            reason=(
                "classified from the rendered pane by the provider's own "
                "screen detector"
                + ("" if corroborated else "; no liveness sample to corroborate it")
            ),
            signals=signals,
        )

    if screen.state == "unreadable" or fifo.state == "unreadable":
        return FusedStatus(
            status=TerminalStatus.UNKNOWN,
            confidence="none",
            reason="every classifier that should have been able to read this " "terminal failed to",
            signals=signals,
        )

    return FusedStatus(
        status=TerminalStatus.NOT_FIFO_MONITORED,
        confidence="none",
        reason=(
            "no FIFO stream and no screen detector for this provider: nothing "
            "available can classify it"
        ),
        signals=signals,
    )


# ---------------------------------------------------------------------------
# observation
# ---------------------------------------------------------------------------


def screen_signal(provider: Any, screen_lines: Optional[List[str]]) -> Signal:
    """Classify a captured viewport with the provider's own screen detector.

    ``screen_lines`` must be the UNTRIMMED ``capture-pane -p`` split — the
    detector is calibrated for a fixed-height viewport and right-padded blank
    rows are part of that shape. ``get_history`` deliberately pops trailing
    blanks for the raw-stream detectors, and reusing its output here would feed
    a viewport detector a shape it was never tuned for.
    """
    if provider is None:
        return Signal("screen", state="absent", detail="no provider instance")
    if not getattr(provider, "supports_screen_detection", False):
        return Signal(
            "screen",
            state="absent",
            detail=f"{type(provider).__name__} ships no viewport-calibrated detector",
        )
    if screen_lines is None:
        return Signal("screen", state="unreadable", detail="pane could not be captured")
    try:
        status = provider.get_status_from_screen(screen_lines)
    except Exception as exc:  # noqa: BLE001 - a detector fault is an observation
        logger.debug("screen detection failed: %s", exc)
        return Signal("screen", state="unreadable", detail=f"{type(exc).__name__}: {exc}")
    if status is None or status == TerminalStatus.UNKNOWN:
        return Signal(
            "screen",
            state="absent",
            detail="detector matched no pattern on this frame",
        )
    return Signal("screen", value=status, state="available")


def liveness_signal(
    previous_digest: Optional[str],
    current_digest: Optional[str],
    *,
    unchanged_for_seconds: Optional[float],
) -> Signal:
    """Seconds the rendered pane has gone without changing.

    Reported as a DURATION rather than a boolean because the fusion needs to
    compare it against a threshold, and because "unchanged since we started
    looking 4 seconds ago" and "unchanged for three days" are not the same
    observation even though a diff returns the same bit for both.
    """
    if current_digest is None:
        return Signal("liveness", state="unreadable", detail="pane could not be captured")
    if previous_digest is None or unchanged_for_seconds is None:
        return Signal(
            "liveness",
            state="absent",
            detail="no prior sample to compare against",
        )
    if previous_digest != current_digest:
        return Signal(
            "liveness", value=0, state="available", detail="pane changed since the previous sample"
        )
    return Signal("liveness", value=int(unchanged_for_seconds), state="available")


def activity_signal(inactive_seconds: Optional[float]) -> Signal:
    if inactive_seconds is None:
        return Signal("activity", state="absent", detail="no last_active recorded")
    return Signal("activity", value=int(inactive_seconds), state="available")


def fifo_signal(status: Any, *, monitored: bool) -> Signal:
    """The stream classifier's answer, or why there isn't one.

    ``monitored=False`` is ``absent``, never ``unreadable``: a native TUI owns
    its pane's argv and renders a full-screen interface, so there is no
    line-oriented stream to classify. That is architecture, not a fault, and
    reporting it as a failure would send an operator looking for a broken pipe
    that was never supposed to exist.
    """
    if not monitored:
        return Signal("fifo", state="absent", detail="native TUI: no FIFO stream exists")
    resolved = _as_status(status)
    if resolved is None or resolved == TerminalStatus.UNKNOWN:
        return Signal("fifo", state="absent", detail="stream classifier matched no pattern yet")
    return Signal("fifo", value=resolved, state="available")
