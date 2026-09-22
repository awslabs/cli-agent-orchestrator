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
from typing import Deque, List, Optional, Tuple, Union


@dataclass(frozen=True)
class GapInfo:
    """Bytes in [from_pos, to_pos) were evicted before they could be replayed."""

    from_pos: int
    to_pos: int


#: One element of a replay: either bytes at a position, or a reported hole where
#: bytes should have been. :meth:`ReplayBuffer.replay_from` returns these in
#: stream order so a caller can emit them without reordering.
ReplayItem = Union[GapInfo, Tuple[int, bytes]]


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

    def begin_generation(self) -> int:
        """Declare the previous byte stream over and start numbering from 0.

        The producer's offset counter is per-stream and monotonic, so an offset
        BEHIND this buffer's watermark means the counter restarted: the FIFO
        reader was stopped and re-armed, or the pane was re-created, and the
        bytes now arriving belong to a different stream that happens to reuse the
        terminal id. Appending them contiguously (what ``append_at`` does for a
        stale offset on its own) splices the new stream onto the old one, so
        every resume position and replay afterwards describes a transcript that
        never existed (Copilot review on #802).

        ``generation`` is the protocol's fence for exactly this, and this is the
        transition that advances it: consumers compare generations and treat a
        higher one as a new stream rather than a continuation, instead of being
        told position 0 of a fresh stream is a rewind of the old one.

        Returns the new generation. The retained window is dropped with the
        stream it described -- replaying those bytes under the new generation
        would attribute one stream's output to another.
        """
        self._generation += 1
        self._chunks.clear()
        self._retained = 0
        self._end_pos = 0
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

    def append_at(self, start: int, chunk: bytes) -> Tuple[Optional[GapInfo], int]:
        """Record a chunk whose position the PRODUCER assigned; returns (gap, pos).

        ``append`` numbers chunks in arrival order, which is only the same thing
        as stream order when nothing between the producer and here can lose one.
        For a runtime bridge the in-process event bus sits in between, and it is
        bounded and drops on a full queue, so arrival-order numbering turned a
        stream with holes into a contiguous one: the server received a shortened
        transcript with a watermark that claimed it was whole, and neither side
        could ever report the loss.

        A ``start`` past the watermark is exactly that loss, with its extent
        known: the returned ``GapInfo`` covers the missing bytes, the watermark
        jumps over them, and the caller turns it into a GapFrame. The retained
        window keeps a hole rather than filler -- nothing may invent bytes the
        terminal did not emit.

        A ``start`` BEHIND the watermark is a producer that restarted its count
        (a torn-down terminal's last flush, a re-created reader). Those bytes are
        real and new to this stream, so they are appended contiguously and the
        stale offset is ignored; the caller can compare the returned position
        with ``start`` to notice.
        """
        if start <= self._end_pos:
            return None, self.append(chunk)
        gap = GapInfo(from_pos=self._end_pos, to_pos=start)
        self._end_pos = start
        return gap, self.append(chunk)

    def replay_from(self, pos: int) -> List[ReplayItem]:
        """Return everything needed to bring a consumer at ``pos`` current.

        Items come back in STREAM ORDER, each either a ``GapInfo`` or a
        ``(pos, chunk)`` pair, so a caller emits them in the order the terminal
        produced them:

        - ``pos`` inside the window: the chunks from ``pos`` onward (the first is
          trimmed so its start is exactly ``pos``), with a ``GapInfo`` before any
          chunk that does not start where the previous one ended.
        - ``pos`` before the window: a leading ``GapInfo(pos, window_start)``,
          then the retained items.
        - ``pos`` at or past end_pos: nothing to do. A pos beyond the watermark
          is a protocol violation (the consumer claims bytes that were never
          produced) and raises ValueError.

        Interior holes are reported, not just the evicted prefix. ``append_at``
        leaves a hole in the retained window when the bus drops a chunk, and it
        returns a ``GapInfo`` the live path turns into a GapFrame — but if the
        channel is down at that moment, that frame is never sent. The consumer
        then reconnects asking for the end of the last chunk it did receive,
        which is INSIDE the window, so a gap keyed only on ``window_start``
        reported nothing: the next chunk arrived at a higher position, the server
        recorded it, and a transcript with a hole in it was accepted as whole —
        exactly the loss positions exist to make reportable (Copilot review
        on #802). Chunk starts are the authority here; a hole needs no extra
        bookkeeping to detect, only to be looked for.

        Stream order matters for the same reason. Sending every gap first and
        the bytes afterwards would advance a consumer's watermark past chunks it
        has not received, so a reconnect dying mid-replay would leave it claiming
        bytes that were never delivered — reintroducing the silent loss one layer
        up.
        """
        if pos > self._end_pos:
            raise ValueError(f"resume position {pos} is past end_pos {self._end_pos}")
        if pos == self._end_pos:
            return []

        out: List[ReplayItem] = []
        start = self.window_start
        if pos < start:
            out.append(GapInfo(from_pos=pos, to_pos=start))
            pos = start

        expected = pos
        for chunk_start, chunk in self._chunks:
            chunk_end = chunk_start + len(chunk)
            if chunk_end <= expected:
                continue
            if chunk_start > expected:
                out.append(GapInfo(from_pos=expected, to_pos=chunk_start))
                out.append((chunk_start, chunk))
            elif chunk_start < expected:
                out.append((expected, chunk[expected - chunk_start :]))
            else:
                out.append((chunk_start, chunk))
            expected = chunk_end
        return out
