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


#: The env var that tells a CAO MCP server which terminal is calling it. Every
#: authorization decision taken next to an agent keys on it
#: (``security.mcp_tool_gate.caller_is_bound``), which is why a profile does not
#: get to choose its value.
CAO_TERMINAL_ID_ENV = "CAO_TERMINAL_ID"

#: The MCP config field carrying a server's environment, in every provider format
#: that has one.
_ENV_FIELD = "env"

#: Console-script filenames that ARE the bundled CAO MCP server, whatever the
#: entry happens to be called in a provider's ``mcpServers`` map.
_CAO_SERVER_SCRIPT_NAMES = frozenset({CAO_MCP_SERVER_COMMAND, f"{CAO_MCP_SERVER_COMMAND}.exe"})


def _basename(token: str) -> str:
    """Last path component of ``token`` under either separator.

    Deliberately not ``Path(token).name``: a POSIX host must still recognise a
    Windows-shaped path in a config file it did not write, and ``PurePath`` on
    Linux treats a backslash as an ordinary character.
    """
    return token.replace("\\", "/").rsplit("/", 1)[-1]


def is_bundled_cao_server(config: dict) -> bool:
    """Does this MCP server entry launch **CAO's own** MCP server?

    Identified by SHAPE, never by the key it sits under in ``mcpServers``. The
    name is the attacker's to choose: review of #1062 found providers that
    protected the entry literally named ``cao-mcp-server`` while a differently
    named entry whose *command* was ``cao-mcp-server`` went through the
    unprotected branch. Every form the resolver can emit or a profile can write
    counts:

    * the bare console script, an absolute path to it, or a ``.exe`` wrapper --
      matched on the basename, so ``/opt/venv/bin/cao-mcp-server`` is caught;
    * ``<python> -m cli_agent_orchestrator.mcp_server.server`` (what
      :func:`resolve_cao_mcp_command` falls back to);
    * the script named in ``args`` rather than ``command`` -- ``uvx --from ...
      cao-mcp-server``, which the Codex writer already documented as reachable.

    A ``command`` given as a list (OpenCode's flattened form) is read too.
    """
    tokens: List[str] = []
    command = config.get("command")
    if isinstance(command, str) and command:
        tokens.append(command)
    elif isinstance(command, (list, tuple)):
        tokens.extend(str(part) for part in command)
    tokens.extend(str(arg) for arg in (config.get("args") or []))
    for index, token in enumerate(tokens):
        if _basename(token) in _CAO_SERVER_SCRIPT_NAMES:
            return True
        if token == "-m" and index + 1 < len(tokens) and tokens[index + 1] == CAO_MCP_SERVER_MODULE:
            return True
    return False


def forward_terminal_identity(
    config: dict, terminal_id: str, *, server_name: str = "", inherited: bool = False
) -> dict:
    """Return ``config`` with ``CAO_TERMINAL_ID`` pinned to the REAL terminal id.

    Providers whose agent does not pass its own environment to MCP subprocesses
    have to inject the identity into each server entry's ``env``. Several of them
    (claude_code, cursor_cli, omp, kimi_cli, copilot_cli, codex,
    kimi_runtime_home) each wrote ``if "CAO_TERMINAL_ID" not in env`` /
    ``env.setdefault(...)`` -- "do not override an explicit preset", from the days
    when the value was a convenience for handoff/assign rather than an
    authorization input. It has been an authorization input since #671/#972:
    the MCP gates read it to decide whether the caller is a bound terminal gated
    on its recorded allowlist, or an operator context that is not gated at all.
    Honouring a profile's preset therefore let the profile pick its own
    authorization posture -- ``env: {CAO_TERMINAL_ID: ""}`` was an operator
    context until the companion fix in ``caller_is_bound``, and a preset naming
    ANOTHER terminal's id still borrows that terminal's allowlist. Found by
    independent review of #1062; #940/#958 took the same decision for
    antigravity, where a pinned id also leaked across terminals.

    So: the launching terminal's own id always wins, and a profile that tried to
    pin one is logged rather than silently obeyed -- the operator who wrote the
    profile needs to know its line had no effect. Values other than
    ``CAO_TERMINAL_ID`` in ``env`` are untouched.

    ``inherited=True`` is for the providers whose MCP children inherit the agent
    process's environment (Kimi Code), where the launch line exports the identity
    once for every server. There, an ABSENT value needs no injection and the
    module documents why -- but a value the profile *declared* is what that server
    is launched with for this key, inherited or not, so it is still overridden.
    Absent plus ``inherited`` is therefore the only case that returns the entry
    untouched. An empty ``terminal_id`` is also a no-op: there is nothing truer to
    replace a declared value with, and writing ``""`` would strip the identity
    rather than correct it.

    PURE: returns a new entry with a new ``env`` dict, so a caller iterating a
    profile's ``mcpServers`` cannot have the profile's own nested dict mutated
    underneath it (the previous in-place ``env[...] = ...`` did exactly that).
    """
    entry = dict(config)
    env = dict(entry.get(_ENV_FIELD) or {})
    declared = env.get(CAO_TERMINAL_ID_ENV)
    if not terminal_id or (inherited and declared is None):
        # No-op, but still hand back the detached ``env`` when the entry had one,
        # so every return from this function keeps the purity contract above.
        if _ENV_FIELD in entry:
            entry[_ENV_FIELD] = env
        return entry
    if declared is not None and declared != terminal_id:
        logger.warning(
            "Overriding %s=%r declared in the env of MCP server %r: a profile does not "
            "choose the calling terminal's identity -- the CAO MCP gates authorize on "
            "it -- so this server is launched with this terminal's own id (%s)",
            CAO_TERMINAL_ID_ENV,
            declared,
            server_name or entry.get("command", ""),
            terminal_id,
        )
    env[CAO_TERMINAL_ID_ENV] = terminal_id
    entry[_ENV_FIELD] = env
    return entry


#: The POSIX shell fragment that carries a working directory for a provider whose
#: MCP config format has no field for one. ``$1`` is the directory, ``$0`` is the
#: label below, and everything after ``$1`` is the real command. ``exec`` replaces
#: the shell so no extra process survives, the environment passes through
#: untouched, and argument boundaries are preserved without any quoting because
#: each argument is a separate argv element rather than text to be re-parsed.
_CWD_SHIM_SCRIPT = 'cd -- "$1" && shift && exec "$@"'

#: ``$0`` for the shim. Purely cosmetic -- it is what shows up in ``ps`` -- but a
#: named label beats an empty or misleading one when an operator is looking at a
#: process list wondering what spawned their MCP server.
_CWD_SHIM_LABEL = "cao-cwd-shim"


def apply_cwd_shim(config: dict) -> dict:
    """Return ``config`` with its ``cwd`` carried by a ``/bin/sh`` wrapper.

    Seven of CAO's providers write MCP configuration in a format with **no
    working-directory field** (verified against each vendor's own documentation,
    2026-09-16). The Agent Plugins mapper always supplies an absolute, contained
    ``cwd`` -- defaulting to the plugin root -- so a plugin whose ``command`` or
    ``args`` are relative to its own directory would otherwise execute from the
    provider's session directory. Reported by review 5222539218 on #584 (item 4).

    PURE and it NEVER RAISES, both deliberately. It is called on the delivery
    path, where the alternative to returning something usable is costing the
    operator the whole agent rather than one server; and it returns a new dict so
    a caller iterating a mapping cannot be surprised by mutation.

    Identity unless the entry has BOTH a non-empty string ``command`` and a
    non-empty string ``cwd``. A remote entry has no ``command`` and is therefore
    untouched -- belt and braces, since only ``_map_stdio`` ever sets ``cwd``.
    """

    command = config.get("command")
    cwd = config.get("cwd")
    if not isinstance(command, str) or not command:
        return config
    if not isinstance(cwd, str) or not cwd:
        return config

    raw_args = config.get("args")
    args = list(raw_args) if isinstance(raw_args, (list, tuple)) else []

    shimmed = dict(config)
    shimmed["command"] = "/bin/sh"
    shimmed["args"] = ["-c", _CWD_SHIM_SCRIPT, _CWD_SHIM_LABEL, cwd, command, *args]
    shimmed.pop("cwd", None)
    return shimmed
