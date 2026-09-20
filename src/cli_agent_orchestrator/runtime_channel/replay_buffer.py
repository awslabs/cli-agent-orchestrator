"""Bounded per-stream replay buffer for the runtime channel (#776).

Lives runtime-side, one instance per (terminal, stream). Positions are
assigned here, at append time — before any queue, socket or reconnect can lose
the chunk — which is the property that makes a later gap *reportable* rather
than silently absorbed.

Bounded replay is deliberate: this is not an at-least-once log. A reconnect
inside the retained window replays; a resume position that has been evicted
produces an explicit GapInfo the caller turns into a GapFrame.
"""

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple


@dataclass(frozen=True)
class GapInfo:
    """Bytes in [from_pos, to_pos) were evicted before they could be replayed."""

    from_pos: int
    to_pos: int


class ReplayBuffer:
    """Append-only byte stream with a bounded replay window.

    Not thread-safe: each stream is owned by one reader thread/task on the
    runtime side, matching how FifoManager already delivers chunks.
    """

    def __init__(self, max_bytes: int, generation: int = 0):
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._generation = generation
        # (start_pos, chunk) entries; total retained size kept <= max_bytes by
        # evicting whole chunks from the left. start of the retained window is
        # the first entry's start_pos (or end_pos when empty).
        self._chunks: Deque[Tuple[int, bytes]] = deque()
        self._retained = 0
        self._end_pos = 0

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def end_pos(self) -> int:
        """Watermark: position just past the last byte ever appended."""
        return self._end_pos

    @property
    def window_start(self) -> int:
        """Oldest position still replayable."""
        return self._chunks[0][0] if self._chunks else self._end_pos

    def append(self, chunk: bytes) -> int:
        """Record a captured chunk; returns its start position.

        A chunk larger than the whole window is still assigned a position and
        advances the watermark, but is retained only up to the budget — the
        unreplayable prefix simply falls outside the window like any evicted
        bytes.
        """
        if not chunk:
            return self._end_pos
        start = self._end_pos
        self._chunks.append((start, chunk))
        self._retained += len(chunk)
        self._end_pos += len(chunk)
        while self._retained > self._max_bytes and self._chunks:
            _, evicted = self._chunks.popleft()
            self._retained -= len(evicted)
        return start

    def replay_from(self, pos: int) -> Tuple[Optional[GapInfo], List[Tuple[int, bytes]]]:
        """Return (gap, chunks) needed to bring a consumer at ``pos`` current.

        - ``pos`` inside the window: no gap, chunks from ``pos`` onward (the
          first chunk is trimmed so its start is exactly ``pos``).
        - ``pos`` before the window: GapInfo(pos, window_start) plus every
          retained chunk.
        - ``pos`` at or past end_pos: nothing to do. A pos beyond the
          watermark is a protocol violation (the consumer claims bytes that
          were never produced) and raises ValueError.
        """
        if pos > self._end_pos:
            raise ValueError(f"resume position {pos} is past end_pos {self._end_pos}")
        if pos == self._end_pos:
            return None, []

        gap: Optional[GapInfo] = None
        start = self.window_start
        if pos < start:
            gap = GapInfo(from_pos=pos, to_pos=start)
            pos = start

        out: List[Tuple[int, bytes]] = []
        for chunk_start, chunk in self._chunks:
            chunk_end = chunk_start + len(chunk)
            if chunk_end <= pos:
                continue
            if chunk_start < pos:
                out.append((pos, chunk[pos - chunk_start :]))
            else:
                out.append((chunk_start, chunk))
        return gap, out
