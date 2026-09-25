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

This is also where a bundled entry is redirected to the shared HTTP endpoint
(#745). When ``CAO_MCP_HTTP_URL`` is set, ``cao-mcp-server`` is replaced by
``cao-mcp-stdio-bridge`` with the endpoint and token injected into the child
env. Doing it here rather than in each provider is what lets the shim's promise
hold literally — no provider code knows the endpoint exists — and it is why an
agent pod needs no MCP server, and therefore no broker credentials, of its own.
"""

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

logger = logging.getLogger(__name__)

# Set when a shared HTTP MCP endpoint exists for agents to use instead of each
# starting its own in-pod server (#745). Read here rather than imported from
# ``mcp_server.http_hosting`` to keep this leaf utility free of that dependency.
SHARED_ENDPOINT_URL_ENV = "CAO_MCP_HTTP_URL"
RUNTIME_TOKEN_ENV = "CAO_RUNTIME_TOKEN"

# The bundled orchestration MCP server's console-script name.
CAO_MCP_SERVER_COMMAND = "cao-mcp-server"

# Module entrypoint equivalent of the console script — runnable by the
# interpreter directly, with no dependency on a script being on PATH.
CAO_MCP_SERVER_MODULE = "cli_agent_orchestrator.mcp_server.server"

# The stdio→HTTP forwarding shim (#745). A provider pointed at the shared
# endpoint declares this instead of ``cao-mcp-server``, and needs the identical
# PATH-independent resolution: it is bundled the same way and fails the same
# way when the agent subprocess's PATH does not include the script dir.
CAO_MCP_STDIO_BRIDGE_COMMAND = "cao-mcp-stdio-bridge"
CAO_MCP_STDIO_BRIDGE_MODULE = "cli_agent_orchestrator.mcp_server.stdio_bridge"

# Bundled console script → module entrypoint. A command absent from this map is
# someone else's MCP server and passes through untouched.
_BUNDLED_COMMANDS = {
    CAO_MCP_SERVER_COMMAND: CAO_MCP_SERVER_MODULE,
    CAO_MCP_STDIO_BRIDGE_COMMAND: CAO_MCP_STDIO_BRIDGE_MODULE,
}


def _script_filename(command: str) -> str:
    """Console-script filename to look for. Windows installs a .exe wrapper."""
    return f"{command}.exe" if sys.platform == "win32" else command


# Retained for the default command so existing references keep working.
_SCRIPT_FILENAME = _script_filename(CAO_MCP_SERVER_COMMAND)


def _sibling_script(command: str = CAO_MCP_SERVER_COMMAND) -> str:
    """Absolute path to a bundled script next to the running interpreter, or ""."""
    if not sys.executable:  # frozen/embedded interpreter — Path("") would raise
        return ""
    sibling = Path(sys.executable).with_name(_script_filename(command))
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
    # When a shared endpoint is configured, the bundled orchestration server
    # becomes the forwarding shim (#745) — one substitution, before path
    # resolution, so every caller of this function is consistent. Only the
    # bundled command is substituted: someone else's MCP server is not ours to
    # redirect, and an entry already naming the shim needs no change.
    if command == CAO_MCP_SERVER_COMMAND and shared_endpoint_url():
        logger.debug("redirecting %s to the shared endpoint via the shim", command)
        command = CAO_MCP_STDIO_BRIDGE_COMMAND

    module = _BUNDLED_COMMANDS.get(command)
    if module is None:
        return command, list(args)

    sibling = _sibling_script(command)
    on_path = shutil.which(command)
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
    return interpreter, ["-m", module, *args]


def shared_endpoint_url() -> str:
    """The shared HTTP MCP endpoint's URL, or "" when there is none."""
    return os.environ.get(SHARED_ENDPOINT_URL_ENV, "").strip()


def shared_endpoint_child_env(*, persisted: bool = False) -> dict:
    """Env a forwarded MCP child needs, for callers that build env themselves.

    Exactly two things the profile cannot know: which endpoint to dial and the
    token to present. Empty dict when no endpoint is configured, so a caller can
    merge it unconditionally.

    The token is omitted when unset rather than sent empty — the shim's own
    check then reports it as absent, which is the legible failure. It is not a
    new secret in the agent's reach either way: the pod that runs the agent
    already carries ``CAO_RUNTIME_TOKEN`` in its environment, because that is
    what its runtime channel authenticates with.

    ``persisted`` omits the TOKEN, and only the token. Set it when the result is
    written to a config file the provider reads at a later launch. The endpoint
    URL still goes in — it is deployment configuration, not a credential, and the
    shim cannot find the server without it.

    The token is left out because it does not need to be there: the shim inherits
    it from the process environment of the pod that launches it, which carries
    ``CAO_RUNTIME_TOKEN`` for its own runtime channel. Writing it into the file
    as well added nothing and put the channel credential on disk in provider
    config — in Kiro's agent JSON and Cursor's plugin.json at the default umask,
    so mode 0644 (Copilot review on #802). A secret that is redundant at rest
    should not be at rest.

    WHAT THIS IS NOT. It is not an isolation boundary, and the per-command gate in
    :func:`shared_endpoint_child_env_for` is not either. Both only decide what is
    WRITTEN. ``TmuxClient.create_session`` forwards every non-blocked ``CAO_*``
    variable from the server's environment into the provider's pane
    (``clients/tmux.py``), so the provider process — and therefore any MCP child
    it spawns, third-party ones included — inherits ``CAO_RUNTIME_TOKEN`` anyway.

    So the honest claim is narrow: these two rules reduce the credential's
    exposure AT REST and keep it out of files a third-party server's author never
    expected to hold a secret. They do not stop a third-party MCP server from
    reading the token out of its own environment. Real isolation needs the token
    withheld from the pane and delivered to the shim alone — which the MCP
    config's per-entry ``env`` is the only existing channel for, and that puts it
    back on disk. Closing that properly means the shim fetching its own
    credential rather than being handed one; until then this is a reduction, not
    a boundary (Copilot review on #802).
    """
    url = shared_endpoint_url()
    if not url:
        return {}
    env = {SHARED_ENDPOINT_URL_ENV: url}
    if persisted:
        return env
    token = os.environ.get(RUNTIME_TOKEN_ENV, "").strip()
    if token:
        env[RUNTIME_TOKEN_ENV] = token
    return env


def shared_endpoint_child_env_for(command: str, *, persisted: bool = False) -> dict:
    """:func:`shared_endpoint_child_env`, but only for the entry it belongs to.

    *command* is the entry's command **as declared**, before resolution. The
    forwarding env is CAO's own: only the bundled server (or an entry already
    naming the shim) is redirected, so only that child needs the endpoint — and
    only that child should be handed ``CAO_RUNTIME_TOKEN``. A third-party MCP
    server declared by an agent profile or plugin is launched unchanged; giving
    it the channel credential would widen the token's reach to code CAO does not
    ship, for no purpose, and — where the provider persists its config — write it
    into a file that server's author never expected to hold a secret.

    Reported by Copilot review on #802 (findings 2 and 9): providers that build
    a child env by hand merged this unconditionally, while
    :func:`resolve_mcp_server_config` had always gated it.
    """
    if command not in _BUNDLED_COMMANDS:
        return {}
    return shared_endpoint_child_env(persisted=persisted)


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
    # A bundled entry being redirected to the shared endpoint also needs the
    # endpoint and token in the child's env; the command swap itself happens in
    # resolve_cao_mcp_command.
    #
    # These two keys are the DEPLOYMENT's, and they win over the profile. The
    # merge used to run the other way, with a comment blessing it as "a value the
    # profile set explicitly wins" — but the two values here are exactly the ones
    # a profile must not choose. An agent-editable profile setting
    # CAO_MCP_HTTP_URL redirected this shim to an arbitrary endpoint, and
    # CAO_RUNTIME_TOKEN went with it, handing the channel credential to whatever
    # was listening. That inverted the isolation the surrounding functions exist
    # to enforce (Copilot review on #802).
    #
    # Unrelated profile variables are still preserved: only the keys the
    # deployment actually defines are overridden, and an override is logged so a
    # profile that tries is visible rather than silently ignored.
    if resolved.get("command") == CAO_MCP_SERVER_COMMAND:
        extra = shared_endpoint_child_env(persisted=persisted)
        if extra:
            profile_env = dict(resolved.get("env") or {})
            clobbered = sorted(
                key
                for key, value in extra.items()
                if key in profile_env and profile_env[key] != value
            )
            if clobbered:
                logger.warning(
                    "ignoring profile-supplied %s for the shared MCP endpoint: "
                    "the endpoint and its token are operator-controlled",
                    ", ".join(clobbered),
                )
            resolved["env"] = {**profile_env, **extra}
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
