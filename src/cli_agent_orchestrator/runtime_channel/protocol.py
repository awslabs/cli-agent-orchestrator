"""Frame contract for the runtime channel (#745/#776).

Every frame on the wire is one JSON object whose ``kind`` field selects the
model. The union is discriminated so an unknown or malformed frame fails
validation at the boundary instead of being half-interpreted downstream.

Two invariants live here rather than in the transport:

- Command correlation: a ``CommandFrame`` carries a server-assigned ``op_id``;
  the runtime answers with a ``CommandResultFrame`` bearing the same ``op_id``
  and retains that result until the server acks it. A lost response is
  recovered by re-requesting the retained result, never by resubmitting the
  command — agent work is not idempotent.
- Stream fencing: output positions are meaningless without their
  ``generation``. A new runtime/assignment taking over a terminal identity
  starts a new generation, so its bytes can never be mistaken for a
  continuation of the old stream, and a status event from a prior generation
  cannot settle the current assignment.
"""

import json
from enum import Enum
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from cli_agent_orchestrator.models.terminal import TerminalId, TerminalStatus

# One protocol version for the whole frame set. Bumped on any incompatible
# change; both sides reject a mismatched hello before accepting work
# (#745: "unsupported combinations must fail before accepting new work").
PROTOCOL_VERSION = 1


class FrameKind(str, Enum):
    CMD = "cmd"
    CMD_RESULT = "cmd_result"
    ACK = "ack"
    EVENT = "event"
    STREAM = "stream"
    GAP = "gap"
    HELLO = "hello"
    HEARTBEAT = "heartbeat"


class CommandType(str, Enum):
    """Server → runtime operations. Each maps onto work the bridge performs
    beside the tmux socket; the server never touches tmux in remote mode."""

    LAUNCH = "launch"
    INPUT = "input"
    SPECIAL_KEY = "special_key"
    RESIZE = "resize"
    ATTACH_OPEN = "attach_open"
    ATTACH_DATA = "attach_data"
    ATTACH_CLOSE = "attach_close"
    EXTRACT = "extract"
    HISTORY = "history"
    CANCEL = "cancel"
    TEARDOWN = "teardown"
    # Run a Python workflow / flow pre-script in the runtime instead of on the
    # server host (#745). RUN_SCRIPT carries the script body + constructed env;
    # CANCEL_SCRIPT terminates an in-flight run by its op_id. These are
    # runtime-scoped (no terminal_id): scripts are not terminals.
    RUN_SCRIPT = "run_script"
    CANCEL_SCRIPT = "cancel_script"


class EventType(str, Enum):
    STATUS = "status"
    READY = "ready"
    EXITED = "exited"


class StreamName(str, Enum):
    """The two per-terminal output streams. Their positions are independent
    counters and never interchangeable (#776)."""

    CAPTURE = "capture"  # background pipe-pane/FIFO output
    ATTACH = "attach"  # interactive PTY bytes for a live viewer


class CommandOutcome(str, Enum):
    OK = "ok"
    FAILED = "failed"
    # Cancellation semantics (#745): CANCEL_REQUESTED acknowledges acceptance;
    # only a later terminal outcome proves execution actually stopped. A dead
    # channel or expired lease must surface as UNKNOWN, never as STOPPED.
    CANCEL_REQUESTED = "cancel_requested"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class _FrameBase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandFrame(_FrameBase):
    kind: Literal[FrameKind.CMD] = FrameKind.CMD
    op_id: str = Field(min_length=1)
    # None for runtime-scoped commands (LAUNCH targets the runtime; the
    # terminal identity is allocated by the launch itself and reported back in
    # the CommandResultFrame). Every terminal-scoped command carries the id.
    terminal_id: Optional[TerminalId] = None
    type: CommandType
    payload: Dict[str, Any] = Field(default_factory=dict)


class CommandResultFrame(_FrameBase):
    kind: Literal[FrameKind.CMD_RESULT] = FrameKind.CMD_RESULT
    op_id: str = Field(min_length=1)
    terminal_id: Optional[TerminalId] = None
    outcome: CommandOutcome
    # Retained runtime-side until the matching AckFrame arrives, so worker
    # cleanup cannot outrun result delivery (#745).
    payload: Dict[str, Any] = Field(default_factory=dict)


class AckFrame(_FrameBase):
    kind: Literal[FrameKind.ACK] = FrameKind.ACK
    op_id: str = Field(min_length=1)


class EventFrame(_FrameBase):
    kind: Literal[FrameKind.EVENT] = FrameKind.EVENT
    terminal_id: TerminalId
    generation: int = Field(ge=0)
    type: EventType
    status: Optional[TerminalStatus] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class StreamFrame(_FrameBase):
    kind: Literal[FrameKind.STREAM] = FrameKind.STREAM
    terminal_id: TerminalId
    stream: StreamName
    generation: int = Field(ge=0)
    # Byte offset of the FIRST byte of ``data`` within (terminal, stream,
    # generation), assigned at capture time — before any lossy queue can
    # discard the chunk. A counter added only after loss cannot reveal the
    # missing bytes (#776).
    pos: int = Field(ge=0)
    data: str  # base64-encoded raw bytes

    @property
    def end_pos(self) -> int:
        import base64

        return self.pos + len(base64.b64decode(self.data))


class GapFrame(_FrameBase):
    """Explicit loss marker: bytes in [from_pos, to_pos) are gone.

    Republished to consumers so "the agent went quiet" stays distinguishable
    from "the text saying what went wrong was lost". ``to_pos`` is None when
    the runtime cannot bound the loss (e.g. a lost generation)."""

    kind: Literal[FrameKind.GAP] = FrameKind.GAP
    terminal_id: TerminalId
    stream: StreamName
    generation: int = Field(ge=0)
    from_pos: int = Field(ge=0)
    to_pos: Optional[int] = Field(default=None, ge=0)


class StreamPosition(_FrameBase):
    terminal_id: TerminalId
    stream: StreamName
    generation: int = Field(ge=0)
    end_pos: int = Field(ge=0)


class HelloFrame(_FrameBase):
    """First frame after (re)connect, in both directions.

    The runtime advertises its live streams and their end positions; the
    server replies with resume positions per stream. Resume inside the replay
    window replays; outside it the runtime emits a GapFrame instead."""

    kind: Literal[FrameKind.HELLO] = FrameKind.HELLO
    protocol_version: int
    runtime_id: str = Field(min_length=1)
    streams: List[StreamPosition] = Field(default_factory=list)
    resume: List[StreamPosition] = Field(default_factory=list)


class HeartbeatFrame(_FrameBase):
    """Keepalive carrying end-position watermarks, so loss of a FINAL chunk is
    detectable even when no later output ever arrives (#776)."""

    kind: Literal[FrameKind.HEARTBEAT] = FrameKind.HEARTBEAT
    streams: List[StreamPosition] = Field(default_factory=list)


Frame = Annotated[
    Union[
        CommandFrame,
        CommandResultFrame,
        AckFrame,
        EventFrame,
        StreamFrame,
        GapFrame,
        HelloFrame,
        HeartbeatFrame,
    ],
    Field(discriminator="kind"),
]

_frame_adapter: TypeAdapter[Frame] = TypeAdapter(Frame)


def encode_frame(frame: BaseModel) -> str:
    return frame.model_dump_json(exclude_none=True)


def decode_frame(raw: Union[str, bytes]) -> Frame:
    """Parse and validate one wire frame. Raises pydantic.ValidationError on
    an unknown kind or malformed fields — the caller closes the channel with a
    protocol error rather than guessing."""
    return _frame_adapter.validate_python(json.loads(raw))
