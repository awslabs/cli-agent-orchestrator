"""Resolution of the bundled cao-mcp-server command for agent MCP configs.

Bundled agent profiles declare the orchestration MCP server as the bare console
script ``cao-mcp-server``. That only resolves if the script's directory is on
the *agent subprocess's* ``PATH`` — which is not guaranteed across install
methods (an unactivated venv, a devcontainer, a ``pip install --prefix`` to a
non-standard location). When it fails to resolve, the agent starts without its
orchestration tools (handoff / assign / send_message) and silently no-ops.

``resolve_cao_mcp_command`` rewrites the bare command to a PATH-independent
invocation, mirroring the three-tier fallback the Copilot provider already used
inline:

    1. the ``cao-mcp-server`` script sitting next to the running interpreter
       (the same environment that launched cao-server — the common case for
       ``uv tool install`` / ``pipx``), then
    2. ``cao-mcp-server`` as resolved on ``PATH``, then
    3. ``<python> -m cli_agent_orchestrator.mcp_server.server`` — always
       runnable because it does not depend on a console script being on PATH.

Any command other than the bare ``cao-mcp-server`` (e.g. a user's custom MCP
server, or an explicit absolute path) passes through unchanged.
"""

import logging
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

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


def _sibling_script() -> str:
    """Absolute path to cao-mcp-server next to the running interpreter, or ""."""
    if not sys.executable:  # frozen/embedded interpreter — Path("") would raise
        return ""
    sibling = Path(sys.executable).with_name(_SCRIPT_FILENAME)
    return str(sibling) if sibling.exists() else ""


def resolve_cao_mcp_command(
    command: str, args: List[str], *, persisted: bool = False
) -> Tuple[str, List[str]]:
    """Resolve a bare ``cao-mcp-server`` command to a PATH-independent form.

    Any command other than the bundled ``cao-mcp-server`` passes through
    unchanged. For the bundled command, the resolution order depends on whether
    the result is written to disk:

    - ``persisted=False`` (default, runtime providers that rebuild the launch
      config every time): prefer the script next to the running interpreter —
      an exact, hijack-proof match recomputed each launch.
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
    on_path = shutil.which(CAO_MCP_SERVER_COMMAND)
    order = (
        [("PATH", on_path), ("sibling", sibling)]
        if persisted
        else [
            ("sibling", sibling),
            ("PATH", on_path),
        ]
    )
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
