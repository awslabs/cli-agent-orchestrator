"""Wire contract between the central server's RunnerHub and a ``cao-runner``.

Everything is a JSON text frame. Direction and shape:

``hello`` (runner -> server, first frame)
    ``{"type": "hello", "protocol": 1, "runner_id", "claims", "accepts_unclaimed",
    "max_terminals", "tmp_dir", "terminals": [{"session", "window", "terminal_id"}]}``.
    ``terminals`` is what the runner is already hosting, so a restarted server
    (or a reconnecting runner) rebuilds its routing from the runner's truth
    instead of guessing.

``welcome`` (server -> runner)
    ``{"type": "welcome", "protocol": 1}``. A protocol mismatch is refused with
    a close code before any work is accepted, never reinterpreted.

``call`` (server -> runner)
    ``{"type": "call", "id", "op", "args"}``. ``op`` must be in :data:`OPS`; the
    runner refuses anything else.

``result`` (runner -> server)
    ``{"type": "result", "id", "ok": true, "value"}`` or
    ``{"type": "result", "id", "ok": false, "error": {"kind", "message"}}``.

``inventory`` (runner -> server, periodic)
    ``{"type": "inventory", "terminals": [...], "creating": [terminal_id, ...]}``.
    ``creating`` lists creates still executing, whose panes may not exist yet.
    ``hello`` carries the same ``creating`` field.

``output`` (runner -> server)
    ``{"type": "output", "pipe_id", "seq", "data"}`` — base64 pane bytes for an
    armed pipe. ``pipe_id`` is chosen by the server in the ``pipe_pane`` call
    and identifies one arming of one pane, so bytes from an earlier pipe on the
    same window are refused. ``seq`` starts at 1 and increases by one per frame
    of that pipe: the server drops a replayed frame and logs a gap.

A call whose connection drops before its ``result`` arrives fails on the server
with :class:`RunnerOutcomeUnknown`. The server never resubmits it: a lost
``send_keys`` response does not prove the keys were not typed.
"""

import re
from typing import Any, Dict

from cli_agent_orchestrator.backends.base import (
    TerminalBackendError,
    TerminalNotFoundError,
)

PROTOCOL_VERSION = 1

# Close codes in the 4000-4999 private range (RFC 6455 §7.4.2).
CLOSE_PROTOCOL_MISMATCH = 4400
CLOSE_BAD_HELLO = 4422
CLOSE_REPLACED = 4409

# Runner ids become log fields and routing keys; keep them boring.
RUNNER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

# A CAO terminal id (``utils.terminal.generate_terminal_id``), as read back from
# a tmux window's ``@cao_terminal_id`` when a restarted runner re-adopts it.
TERMINAL_ID_PATTERN = re.compile(r"^[0-9a-f]{8}$")

# A staged provider file is addressed by bare name inside the runner's own
# CAO tmp dir. It must start with the 8-hex terminal id it belongs to and can
# never contain a path separator, so a server bug cannot make a runner write
# outside that directory.
STAGED_FILE_PATTERN = re.compile(r"^[0-9a-f]{8}[A-Za-z0-9._-]{0,120}$")

# Upper bound on one staged file. Provider prompt/MCP files are kilobytes; a
# multi-megabyte file here means something unexpected is being staged.
MAX_STAGED_FILE_BYTES = 1024 * 1024

# The handshake header that carries the runner token. Named, so the two places
# that read and write it are not mistaken for CAO API bearer helpers: this is
# the runner channel's own credential, unrelated to the local API's bearer.
TOKEN_HEADER = "Authorization"

# Dead-peer detection on the runner channel. A half-open connection is dropped
# after PING_INTERVAL + PING_TIMEOUT; that is also how long a genuine runner's
# reconnect can be refused while the hub still believes its old socket is live.
PING_INTERVAL = 20.0
PING_TIMEOUT = 20.0

# hello -> welcome must complete within this, on either side. It covers one
# round trip plus the hub rebuilding a runner's routes; nothing else is waited on.
HANDSHAKE_TIMEOUT = 15.0

# Handing one frame to the hub's event loop for sending. The loop only enqueues
# the write, so anything near this bound means the loop itself is wedged.
SEND_TIMEOUT = 10.0

# Frames carry full scrollback reads (tmux history is raised to thousands of
# lines), so the default 1 MiB frame cap is too small.
MAX_FRAME_BYTES = 16 * 1024 * 1024

# The terminal operations a runner executes. The server only ever sends these;
# the runner rejects any other name.
OPS = frozenset(
    {
        "create_session",
        "create_window",
        "session_exists",
        "session_exists_strict",
        "list_sessions",
        "kill_session",
        "kill_window",
        "cleanup_terminal_exact",
        "send_keys",
        "send_special_key",
        "get_history",
        "get_pane_working_directory",
        "get_pane_current_command",
        "pipe_pane",
        "stop_pipe_pane",
        "stage_files",
    }
)


class RunnerError(TerminalBackendError):
    """A remote terminal operation did not complete. ``status_code`` is its HTTP status.

    A ``TerminalBackendError`` first, so every core path that already handles a
    backend failure (teardown, liveness probes, rollback) handles these the same
    way; the API maps the status code on top. The three subclasses are the three
    things a caller can actually act on differently.
    """

    status_code = 500


class RunnerUnavailable(RunnerError):
    """The runner that owns a terminal is not connected; nothing was sent. Retry is safe."""

    status_code = 503


class RunnerOutcomeUnknown(RunnerError):
    """The call was sent but its result never arrived.

    Distinct from :class:`RunnerUnavailable` on purpose: here the operation may
    or may not have run on the runner, so a caller must not treat it as "did
    not happen" and blindly repeat it.
    """

    status_code = 504


class RunnerReportedError(RunnerError):
    """The runner executed the operation and reported a failure (e.g. tmux refused)."""

    status_code = 502


def encode_error(exc: BaseException) -> Dict[str, str]:
    """Classify a runner-side exception for transport.

    The kinds are the exception families callers of ``TerminalBackend`` already
    branch on: ``ValueError`` for "session/window not found" and invalid
    directories, ``TerminalNotFoundError`` and ``TerminalBackendError``. Anything
    else is reported as a backend error so it can never be mistaken for one of
    the specific outcomes.
    """
    if isinstance(exc, TerminalNotFoundError):
        kind = "terminal_not_found"
    elif isinstance(exc, TerminalBackendError):
        kind = "backend_error"
    elif isinstance(exc, ValueError):
        kind = "value_error"
    else:
        kind = "backend_error"
    return {"kind": kind, "message": str(exc) or type(exc).__name__}


def raise_remote_error(error: Any, runner_id: str) -> None:
    """Re-raise a transported error as the matching local exception type."""
    if not isinstance(error, dict):
        raise RunnerReportedError(f"runner {runner_id}: malformed error {error!r}")
    kind = error.get("kind")
    message = f"runner {runner_id}: {error.get('message', '')}"
    if kind == "value_error":
        raise ValueError(message)
    if kind == "terminal_not_found":
        raise TerminalNotFoundError("", message)
    raise RunnerReportedError(message)
