"""Cached, bounded observation of a tmux pane's rendered viewport.

Supplies the ``screen`` and ``liveness`` inputs to :mod:`status_fusion`.

Cost is the whole design constraint. ``project_session`` runs on every
dashboard poll, and COND-0242 records what happens when observation is not
bounded: a per-terminal probe that reached the pane through a server-wide
``list-sessions`` contended with ordinary API work badly enough to cause
sustained control-plane unavailability. So:

* **one tmux call per observation, addressed by pane id.** The projection
  already carries ``pane_id``; resolving it again through ``list-panes`` would
  reintroduce exactly the traffic that incident was about.
* **a minimum resample interval.** Repeated polls inside the window reuse the
  cached frame. A dashboard refreshing every two seconds must not turn into
  twenty ``capture-pane`` calls every two seconds.
* **a hard timeout**, and a failure that leaves as ``None`` rather than as an
  empty frame. An observation that could not be made must never be
  indistinguishable from an observation of an empty pane — that is the same
  trap COND-0242 names, and here it would let the fusion read a failed capture
  as a quiet screen and accuse a healthy worker of being wedged.

The cache also owns the ``unchanged_for`` clock, because that duration cannot
be derived from a single sample. It records when a pane's content digest was
FIRST seen and reports the age of that digest, so "unchanged since we started
looking four seconds ago" stays distinguishable from "unchanged for three
days" — a diff alone returns the same bit for both.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Minimum seconds between two real captures of the same pane.
MIN_RESAMPLE_SECONDS = 5.0

#: Hard bound on one capture. Deliberately short: this is an observation on a
#: latency-sensitive path, and a slow answer is worth less than a fast absence.
CAPTURE_TIMEOUT_SECONDS = 3.0


@dataclass
class _Sample:
    digest: str
    #: When THIS digest was first observed — not when it was last observed.
    first_seen: float
    last_checked: float
    lines: List[str]


class PaneObserver:
    """Digest + viewport cache for live panes, keyed by pane id."""

    def __init__(
        self,
        *,
        min_resample_seconds: float = MIN_RESAMPLE_SECONDS,
        timeout_seconds: float = CAPTURE_TIMEOUT_SECONDS,
        clock=time.monotonic,
        capture=None,
    ):
        self._min_resample = min_resample_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._capture = capture or self._capture_pane
        self._samples: Dict[str, _Sample] = {}
        self._lock = threading.Lock()

    # -- observation ----------------------------------------------------

    def _capture_pane(self, pane_id: str) -> Optional[str]:
        """``tmux capture-pane -p -t <pane_id>``, or None.

        No ``-e``: the viewport detectors want escape-free text. No trailing
        blank-line trimming either — ``get_status_from_screen`` is calibrated
        for a fixed-height, right-padded viewport, and ``get_history`` pops
        those blanks specifically for the RAW-stream detectors. Feeding a
        viewport detector the trimmed shape would hand it geometry it was
        never tuned for.
        """
        from cli_agent_orchestrator.clients.tmux import tmux_binary

        try:
            proc = subprocess.run(
                [tmux_binary(), "capture-pane", "-p", "-t", pane_id],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.debug("capture-pane %s failed: %s", pane_id, exc)
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout

    def observe(self, pane_id: Optional[str]) -> Tuple[Optional[List[str]], Optional[float]]:
        """``(viewport_lines, unchanged_for_seconds)`` for one pane.

        ``unchanged_for_seconds`` is None on the first ever sight of a pane —
        there is no prior frame, so no duration exists yet and inventing 0
        would claim the pane had just changed.
        """
        if not pane_id:
            return None, None
        now = self._clock()
        with self._lock:
            cached = self._samples.get(pane_id)
            if cached is not None and (now - cached.last_checked) < self._min_resample:
                return cached.lines, now - cached.first_seen

        text = self._capture(pane_id)
        if text is None:
            # A failed capture must not poison the cache: keeping the previous
            # sample lets the NEXT successful read continue the same
            # unchanged-for clock instead of restarting it.
            return None, None

        lines = text.split("\n")
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]
        with self._lock:
            cached = self._samples.get(pane_id)
            if cached is not None and cached.digest == digest:
                cached.last_checked = now
                cached.lines = lines
                return lines, now - cached.first_seen
            self._samples[pane_id] = _Sample(
                digest=digest, first_seen=now, last_checked=now, lines=lines
            )
        # First sight of this content: the clock starts now and there is no
        # duration to report yet.
        return lines, None if cached is None else 0.0

    def forget(self, pane_id: str) -> None:
        with self._lock:
            self._samples.pop(pane_id, None)

    def prune(self, live_pane_ids) -> int:
        """Drop cache entries for panes that no longer exist."""
        keep = set(live_pane_ids or ())
        with self._lock:
            gone = [p for p in self._samples if p not in keep]
            for pane in gone:
                del self._samples[pane]
        return len(gone)


#: Process-wide observer. The unchanged-for clock is only meaningful if
#: successive polls consult the SAME cache, so this is deliberately shared
#: rather than constructed per request.
observer = PaneObserver()
