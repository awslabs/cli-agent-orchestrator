"""Resolution of the bundled cao-mcp-server command for agent MCP configs.

Bundled agent profiles declare the orchestration MCP server as the bare console
script ``cao-mcp-server``. That only resolves if the script's directory is on
the *agent subprocess's* ``PATH`` — which is not guaranteed across install
methods (an unactivated venv, a devcontainer, a ``pip install --prefix`` to a
non-standard location). When it fails to resolve, the agent starts without its
orchestration tools (handoff / assign / send_message) and silently no-ops.

``resolve_cao_mcp_command`` rewrites the bare command to a PATH-independent
invocation:

    1. Runtime configs use the ``cao-mcp-server`` script sitting next to the
       running interpreter
       (the same environment that launched cao-server — the common case for
       ``uv tool install`` / ``pipx``), then the current interpreter's module
       entrypoint.
    2. Persisted configs may prefer ``cao-mcp-server`` as resolved on ``PATH``
       so a stable launcher can survive an upgrade, then fall back to the
       interpreter sibling/module target.

The module entrypoint (``<python> -m cli_agent_orchestrator.mcp_server.server``)
is always runnable because it does not depend on a console script being on PATH.

Any command other than the bare ``cao-mcp-server`` (e.g. a user's custom MCP
server, or an explicit absolute path) passes through unchanged.
"""

import logging
import shutil
import stat
import sys
from pathlib import Path
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)

# The bundled orchestration MCP server's console-script name.
CAO_MCP_SERVER_COMMAND = "cao-mcp-server"

# Module entrypoint equivalent of the console script — runnable by the
# interpreter directly, with no dependency on a script being on PATH.
CAO_MCP_SERVER_MODULE = "cli_agent_orchestrator.mcp_server.server"

# Console-script filename to look for next to the interpreter. On Windows the
# script is installed as a .exe wrapper.
_SCRIPT_FILENAME = (
    f"{CAO_MCP_SERVER_COMMAND}.exe" if sys.platform == "win32" else CAO_MCP_SERVER_COMMAND
)

# Bound the inspection of a legacy absolute console-script path.  A CAO
# generated wrapper is only a few hundred bytes; a bounded read keeps a
# configuration refresh from following an arbitrary large user-owned file.
_MAX_CONSOLE_SCRIPT_BYTES = 64 * 1024
_CONSOLE_SCRIPT_MODULE_IMPORT = b"from cli_agent_orchestrator.mcp_server.server import main"


def _sibling_script() -> str:
    """Absolute path to cao-mcp-server next to the running interpreter, or ""."""
    if not sys.executable:  # frozen/embedded interpreter — Path("") would raise
        return ""
    sibling = Path(sys.executable).with_name(_SCRIPT_FILENAME)
    return str(sibling) if sibling.exists() else ""


def get_cao_mcp_server_profile_args(command: Any) -> Optional[List[str]]:
    """Return profile args when a stored OpenCode command is CAO-owned.

    OpenCode stores a local MCP launch as one argv list.  Its old entries may
    contain the bare helper, the module fallback, or an absolute console-script
    path created by a prior CAO install.  Only the first two forms and an
    absolute, regular script carrying CAO's exact console-script import are
    safe to refresh.  In particular, a user executable merely named
    ``cao-mcp-server`` is not CAO-owned.

    ``None`` means the stored command is unrecognized and must be preserved
    byte-for-byte.  A returned list is a fresh copy of the user profile args.
    """
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        return None

    executable = command[0]
    if executable == CAO_MCP_SERVER_COMMAND:
        return list(command[1:])

    if len(command) >= 3 and command[1] == "-m" and command[2] == CAO_MCP_SERVER_MODULE:
        return list(command[3:])

    if _is_cao_mcp_console_script(executable):
        return list(command[1:])
    return None


def _is_cao_mcp_console_script(executable: str) -> bool:
    """Whether *executable* is a bounded, generated CAO console script.

    Inspect only absolute regular files and fail closed on all filesystem
    errors.  This provenance check supports migration of a prior CAO install
    without taking ownership of an unrelated same-basename custom command.
    """
    path = Path(executable)
    if not path.is_absolute():
        return False
    try:
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > _MAX_CONSOLE_SCRIPT_BYTES:
            return False
        with path.open("rb") as file:
            contents = file.read(_MAX_CONSOLE_SCRIPT_BYTES + 1)
    except OSError:
        return False
    return len(contents) <= _MAX_CONSOLE_SCRIPT_BYTES and _CONSOLE_SCRIPT_MODULE_IMPORT in contents


def resolve_cao_mcp_command(
    command: str, args: List[str], *, persisted: bool = False
) -> Tuple[str, List[str]]:
    """Resolve a bare ``cao-mcp-server`` command to a PATH-independent form.

    Any command other than the bundled ``cao-mcp-server`` passes through
    unchanged. For the bundled command, the resolution order depends on whether
    the result is written to disk:

    - ``persisted=False`` (default, runtime providers that rebuild the launch
      config every time): prefer the script next to the running interpreter,
      then its module entrypoint. Do not resolve a global PATH launcher, which
      could belong to an older CAO installation.
    - ``persisted=True`` (the resolved command is written to a config file the
      provider reads later, e.g. Kiro/Q agent JSON): prefer the script as
      resolved on ``PATH``. Tool installers (uv, pipx) keep a *stable* launcher
      there (e.g. ``~/.local/bin/cao-mcp-server``) that survives upgrades,
      whereas the interpreter-sibling path lives under a versioned venv dir that
      ``uv tool upgrade`` relocates — which would leave a persisted path stale.

    Both orders fall back to the module entrypoint (``<python> -m
    cli_agent_orchestrator.mcp_server.server``), which needs no console script
    on PATH.

    Args:
        command: The ``command`` field from an MCP server config.
        args: The ``args`` field (may be empty).
        persisted: Whether the resolved command will be written to disk and
            reused across CAO upgrades (see above).

    Returns:
        A ``(command, args)`` tuple.
    """
    if command != CAO_MCP_SERVER_COMMAND:
        return command, list(args)

    sibling = _sibling_script()
    order: List[Tuple[str, Optional[str]]] = [("sibling", sibling)]
    if persisted:
        order.insert(0, ("PATH", shutil.which(CAO_MCP_SERVER_COMMAND)))
    for label, candidate in order:
        if candidate:
            logger.debug("Resolved %s via %s: %s", command, label, candidate)
            return candidate, list(args)

    # Module entrypoint via the current interpreter — runnable without any
    # console script on PATH. Falls back to a bare ``python3`` only if
    # sys.executable is unavailable (best effort in degenerate environments).
    # Caller-supplied args are appended after the module path so flags reach
    # the server in this tier too.
    interpreter = sys.executable or "python3"
    logger.debug("Resolved %s to module entrypoint via %s", command, interpreter)
    return interpreter, ["-m", CAO_MCP_SERVER_MODULE, *args]


def resolve_mcp_server_config(config: dict, *, persisted: bool = False) -> dict:
    """Return a copy of an MCP server config with its command resolved.

    ``persisted`` is forwarded to :func:`resolve_cao_mcp_command`; set it True
    when the result is written to a config file the provider reads at a later
    launch (e.g. Kiro/Q agent JSON). Convenience wrapper for the common
    case of an entry shaped like ``{"command": ..., "args": [...], ...}``.
    Leaves all other keys (``type``, ``env``, ...) untouched.

    Entries without a ``command`` (e.g. url/transport servers shaped
    ``{"type": "http", "url": ...}``) pass through untouched — resolution only
    applies to command-launched servers, and injecting ``command=""``/``args``
    into a command-less entry would corrupt it for providers that emit every
    present key.
    """
    if "command" not in config:
        return dict(config)
    resolved = dict(config)
    command = resolved.get("command", "")
    args = resolved.get("args", []) or []
    new_command, new_args = resolve_cao_mcp_command(command, args, persisted=persisted)
    if (new_command, new_args) == (command, args):
        # Passthrough (non-bundled command): don't write back keys the entry
        # didn't have — e.g. don't add args=[] to an entry that omitted args.
        return resolved
    resolved["command"] = new_command
    resolved["args"] = new_args
    return resolved
