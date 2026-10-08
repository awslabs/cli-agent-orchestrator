"""Frames exchanged between cao-server and an execution runtime (#745).

An execution runtime runs ``cao-bridge`` next to its agents' tmux server and keeps
one outbound WebSocket to the central server's ``/runtime/channel``. The server
sends commands down; the runtime answers each with a result carrying the same
``op_id`` and pushes terminal status changes up. Every frame is one JSON text
message.
"""

from enum import Enum
from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, Field, TypeAdapter
from typing_extensions import Annotated

from cli_agent_orchestrator.models.terminal import TerminalStatus

#: Bumped whenever a frame or command changes meaning. Both sides refuse a peer
#: with a different version at hello, before any work is accepted.
PROTOCOL_VERSION = 1


class CommandType(str, Enum):
    LAUNCH = "launch"
    INPUT = "input"
    KEY = "key"
    OUTPUT = "output"
    WORKING_DIRECTORY = "working_directory"
    EXIT = "exit"
    DELETE = "delete"


#: A runtime id is addressed as one URL path segment (``/runtimes/{runtime_id}/...``):
#: letters, digits, ``.``, ``_`` and ``-``, starting with a letter or digit, at
#: most 128 characters. A Kubernetes pod name fits.
RUNTIME_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class Hello(BaseModel):
    """First frame in each direction.

    The runtime's hello lists every terminal it runs, with its current status
    (``unknown`` until its status monitor has one).
    """

    kind: Literal["hello"] = "hello"
    protocol_version: int
    runtime_id: str = Field(pattern=RUNTIME_ID_PATTERN)
    statuses: Dict[str, TerminalStatus] = Field(default_factory=dict)


class Command(BaseModel):
    kind: Literal["command"] = "command"
    op_id: str
    type: CommandType
    terminal_id: Optional[str] = None
    # The session of the central row the command was routed by. Terminal ids
    # are reused once a terminal is gone; with its session, a runtime that has
    # since launched another terminal under the same id does not act on that
    # one. None where no row names one (an unrecorded terminal's cleanup).
    session_name: Optional[str] = None
    payload: Dict[str, Any] = Field(default_factory=dict)


class Result(BaseModel):
    kind: Literal["result"] = "result"
    op_id: str
    ok: bool
    payload: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None


class Status(BaseModel):
    kind: Literal["status"] = "status"
    terminal_id: str
    status: TerminalStatus


Frame = Annotated[Union[Hello, Command, Result, Status], Field(discriminator="kind")]
_FRAME: "TypeAdapter[Union[Hello, Command, Result, Status]]" = TypeAdapter(Frame)


def encode(frame: BaseModel) -> str:
    return frame.model_dump_json()


def decode(text: Union[str, bytes]) -> Union[Hello, Command, Result, Status]:
    """Parse one frame. Raises ``pydantic.ValidationError`` on anything else."""
    frame: Union[Hello, Command, Result, Status] = _FRAME.validate_json(text)
    return frame
