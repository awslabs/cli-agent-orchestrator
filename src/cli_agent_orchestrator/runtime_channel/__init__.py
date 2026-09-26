"""Runtime channel protocol for remote execution (issues #745/#776, CAO 3.0).

A runtime (worker or supervisor pod) holds one persistent outbound WebSocket to
the central cao-server. Commands travel down (launch, input, resize, attach,
cancel, teardown) with op_id correlation and acknowledged retained results;
terminal output and worker-derived status stream up with per-stream
(generation, position) sequencing, bounded replay, and explicit gap frames.

This package deliberately contains no transport wiring yet — only the frame
contract and the replay buffer, so both sides (server endpoint and the
execution bridge) build against the same validated models. See
docs/issues/745-remote-execution-boundary/design.md for the full contract.
"""

from cli_agent_orchestrator.runtime_channel.protocol import (
    AckFrame,
    CommandFrame,
    CommandOutcome,
    CommandResultFrame,
    CommandType,
    EventFrame,
    EventType,
    Frame,
    FrameKind,
    GapFrame,
    HeartbeatFrame,
    HelloFrame,
    StreamFrame,
    StreamName,
    StreamPosition,
    decode_frame,
    encode_frame,
)
from cli_agent_orchestrator.runtime_channel.replay_buffer import GapInfo, ReplayBuffer

__all__ = [
    "AckFrame",
    "CommandFrame",
    "CommandOutcome",
    "CommandResultFrame",
    "CommandType",
    "EventFrame",
    "EventType",
    "Frame",
    "FrameKind",
    "GapFrame",
    "GapInfo",
    "HeartbeatFrame",
    "HelloFrame",
    "ReplayBuffer",
    "StreamFrame",
    "StreamName",
    "StreamPosition",
    "decode_frame",
    "encode_frame",
]
