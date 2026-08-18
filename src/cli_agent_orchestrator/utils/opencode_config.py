"""Read-modify-write helper for the shared ``opencode.json`` config file.

Provides idempotent upsert operations for MCP server declarations and per-agent
tool gating, plus the ``to_opencode_agent_id`` helper that derives a single
slash-safe identifier used consistently for the installed ``.md`` filename,
the runtime ``--agent`` argument, and the ``agent.<id>.tools`` key.

No file locking is applied; concurrent ``cao install --provider opencode_cli``
invocations are not a supported scenario.
"""

import copy
import json
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from cli_agent_orchestrator.constants import (
    CAO_HOME_DIR,
    OPENCODE_CONFIG_DIR,
    OPENCODE_CONFIG_FILE,
    SKILLS_DIR,
)
from cli_agent_orchestrator.utils.mcp_resolution import (
    CAO_MCP_SERVER_COMMAND,
    get_cao_mcp_server_profile_args,
    resolve_cao_mcp_command,
)

logger = logging.getLogger(__name__)

_SCHEMA = "https://opencode.ai/config.json"
_RUNTIME_CONFIG_NAME = "opencode.json"
_RUNTIME_DEPENDENCY_CACHE_NAME = ".cao-runtime-dependencies"
_RUNTIME_DEPENDENCY_COMPLETE_MARKER_NAME = ".cao-complete"
_RUNTIME_DEPENDENCY_COMPLETE_MARKER = "cao-opencode-runtime-dependencies-v1\n"
_RUNTIME_DEPENDENCY_FILE_NAMES = ("package.json", "package-lock.json")
_RUNTIME_DEPENDENCY_DIRECTORY_NAME = "node_modules"
_RUNTIME_DEPENDENCY_NAMES = {
    *_RUNTIME_DEPENDENCY_FILE_NAMES,
    _RUNTIME_DEPENDENCY_DIRECTORY_NAME,
}
_CONFIG_FILES_NOT_TO_LINK = {
    _RUNTIME_CONFIG_NAME,
    "opencode.jsonc",
    _RUNTIME_DEPENDENCY_CACHE_NAME,
    *_RUNTIME_DEPENDENCY_NAMES,
}
_TERMINAL_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def to_opencode_agent_id(profile_name: str) -> str:
    """Derive the OpenCode agent ID from a CAO profile name.

    OpenCode treats the filename stem of an agent ``.md`` file as its agent ID
    (used for ``--agent <id>`` and keyed by the same value under
    ``agent.<id>`` in ``opencode.json``). Profile names may contain ``/`` —
    illegal in filenames — so the conversion replaces every slash with ``__``.

    The output is the single source of truth for:

    - the installed ``<id>.md`` filename under ``OPENCODE_AGENTS_DIR``
    - the ``agent.<id>.tools`` key written to ``opencode.json``
    - the value passed to ``opencode --agent <id>`` at runtime

    Idempotent: inputs that contain no ``/`` are returned unchanged.
    """
    return profile_name.replace("/", "__")


def ensure_skills_symlink() -> None:
    """Create ``OPENCODE_CONFIG_DIR/skills`` as a symlink pointing at ``SKILLS_DIR``.

    Idempotent: no-op when the correct symlink already exists.
    Warns and skips without modification when the target path is occupied by any
    other entity (non-symlink directory, file, or symlink pointing elsewhere) —
    CAO does not repair user-owned state at this path.
    """
    target = OPENCODE_CONFIG_DIR / "skills"

    if target.is_symlink():
        # Handles both valid and broken symlinks.
        if target.resolve() == SKILLS_DIR.resolve():
            return  # Already correct — idempotent no-op.
        logger.warning(
            "opencode skills symlink at %s points to %s instead of %s — skipping",
            target,
            target.resolve(),
            SKILLS_DIR.resolve(),
        )
        return

    if target.exists():
        # A real directory or file — do not touch it.
        logger.warning(
            "opencode skills target %s exists but is not a symlink — skipping",
            target,
        )
        return

    OPENCODE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target.symlink_to(SKILLS_DIR)


def read_config() -> Dict[str, Any]:
    """Load ``opencode.json``, returning an empty skeleton if the file is absent."""
    if not OPENCODE_CONFIG_FILE.exists():
        return {"$schema": _SCHEMA}
    result: Dict[str, Any] = json.loads(OPENCODE_CONFIG_FILE.read_text(encoding="utf-8"))
    return result


def write_config(data: Dict[str, Any]) -> None:
    """Persist *data* to ``opencode.json``, creating parent directories as needed."""
    OPENCODE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    OPENCODE_CONFIG_FILE.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )


def prepare_opencode_runtime_config(terminal_id: str) -> Path:
    """Create a private, per-terminal OpenCode config snapshot.

    The shared config is installation/user state and must never be rewritten at
    launch.  A private snapshot can instead replace stale CAO-owned MCP helper
    paths with the current process's helper while retaining every user-owned
    command and argument exactly as stored.
    """
    root = _runtime_config_root(terminal_id)
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise RuntimeError(f"Refusing symlinked or non-directory OpenCode runtime root: {root}")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Refusing symlinked or non-directory OpenCode runtime root: {root}")
    os.chmod(root, 0o700)

    _link_shared_config_siblings(root)
    _link_runtime_dependency_assets(root)
    data = copy.deepcopy(read_config())
    _refresh_cao_mcp_commands(data)
    _write_runtime_config(root / _RUNTIME_CONFIG_NAME, data)
    return root


def opencode_runtime_config_root(terminal_id: str, *, cao_home_dir: Path | None = None) -> Path:
    """Return the owned runtime root for a safe terminal identifier."""
    if not _TERMINAL_ID_PATTERN.fullmatch(terminal_id):
        raise ValueError(f"Invalid terminal ID for OpenCode runtime config: {terminal_id!r}")
    home_dir = cao_home_dir if cao_home_dir is not None else CAO_HOME_DIR
    return home_dir / "tmp" / f"opencode-{terminal_id}"


def _runtime_config_root(terminal_id: str) -> Path:
    """Return the configured CAO-owned runtime root for *terminal_id*."""
    return opencode_runtime_config_root(terminal_id)


def _link_shared_config_siblings(root: Path) -> None:
    """Expose shared assets in a runtime root without copying user state."""
    if not OPENCODE_CONFIG_DIR.exists():
        return

    for source in OPENCODE_CONFIG_DIR.iterdir():
        if source.name in _CONFIG_FILES_NOT_TO_LINK:
            continue
        destination = root / source.name
        if destination.exists() or destination.is_symlink():
            continue
        destination.symlink_to(source, target_is_directory=source.is_dir())


def promote_opencode_runtime_dependencies(runtime_root: Path) -> None:
    """Persist complete first-launch package state for later private roots.

    OpenCode installs its plugin dependencies beside the runtime config when a
    CAO config is empty.  That runtime root is terminal-scoped and cleaned up,
    so copy a *complete* package set into CAO's private cache after the TUI is
    ready.  A later root only links a completed source; it never consumes a
    partial install.

    The cache is CAO-owned but lives below the shared OpenCode config so it
    survives individual terminal cleanup.  It intentionally never rewrites
    the shared ``opencode.json`` or user-managed package assets.
    """
    if not _has_complete_runtime_dependencies(runtime_root):
        return

    cache_root = _runtime_dependency_cache_root()
    if cache_root.exists() or cache_root.is_symlink():
        if _has_complete_runtime_dependencies(cache_root, require_marker=True):
            return
        if cache_root.is_symlink() or not cache_root.is_dir():
            logger.warning(
                "OpenCode dependency cache is not a real directory; leaving it untouched: %s",
                cache_root,
            )
            return
        # This hidden directory is CAO-owned. A markerless cache is from an
        # interrupted or pre-marker publication and is never linked into a
        # runtime root, so remove it before publishing the known-ready state.
        try:
            shutil.rmtree(cache_root)
        except OSError:
            logger.warning("Unable to clear incomplete OpenCode dependency cache: %s", cache_root)
            return

    try:
        OPENCODE_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temporary_root = Path(
            tempfile.mkdtemp(prefix=f".{_RUNTIME_DEPENDENCY_CACHE_NAME}.", dir=OPENCODE_CONFIG_DIR)
        )
    except OSError:
        logger.warning("Unable to create OpenCode dependency cache", exc_info=True)
        return

    try:
        os.chmod(temporary_root, 0o700)
        for filename in _RUNTIME_DEPENDENCY_FILE_NAMES:
            source = runtime_root / filename
            if source.is_file():
                shutil.copy2(source, temporary_root / filename)
        shutil.copytree(
            runtime_root / _RUNTIME_DEPENDENCY_DIRECTORY_NAME,
            temporary_root / _RUNTIME_DEPENDENCY_DIRECTORY_NAME,
            symlinks=True,
        )
        marker = temporary_root / _RUNTIME_DEPENDENCY_COMPLETE_MARKER_NAME
        marker.write_text(_RUNTIME_DEPENDENCY_COMPLETE_MARKER, encoding="utf-8")
        os.chmod(marker, 0o600)
        try:
            temporary_root.rename(cache_root)
        except OSError:
            if not _has_complete_runtime_dependencies(cache_root, require_marker=True):
                logger.warning(
                    "Unable to publish OpenCode dependency cache: %s", cache_root, exc_info=True
                )
    except OSError:
        logger.warning("Unable to populate OpenCode dependency cache", exc_info=True)
    finally:
        if temporary_root.exists() and not temporary_root.is_symlink():
            shutil.rmtree(temporary_root, ignore_errors=True)


def _link_runtime_dependency_assets(root: Path) -> None:
    """Link a complete shared or CAO-cached package set into *root*."""
    source_root = _runtime_dependency_source()
    if source_root is None:
        return

    for name in _RUNTIME_DEPENDENCY_NAMES:
        source = source_root / name
        if not source.exists():
            continue
        destination = root / name
        if destination.exists() or destination.is_symlink():
            continue
        destination.symlink_to(source, target_is_directory=source.is_dir())


def _runtime_dependency_source() -> Path | None:
    """Return CAO's completed package source without exposing partial state."""
    cache_root = _runtime_dependency_cache_root()
    if _has_complete_runtime_dependencies(cache_root, require_marker=True):
        return cache_root
    return None


def _runtime_dependency_cache_root() -> Path:
    """Return CAO's persistent OpenCode first-launch dependency cache path."""
    return OPENCODE_CONFIG_DIR / _RUNTIME_DEPENDENCY_CACHE_NAME


def _has_complete_runtime_dependencies(root: Path, *, require_marker: bool = False) -> bool:
    """Whether *root* has the minimum safe OpenCode package state."""
    has_dependencies = (
        root.is_dir()
        and not root.is_symlink()
        and (root / "package.json").is_file()
        and (root / _RUNTIME_DEPENDENCY_DIRECTORY_NAME).is_dir()
    )
    if not has_dependencies or not require_marker:
        return has_dependencies

    marker = root / _RUNTIME_DEPENDENCY_COMPLETE_MARKER_NAME
    try:
        return (
            marker.is_file()
            and not marker.is_symlink()
            and marker.read_text(encoding="utf-8") == _RUNTIME_DEPENDENCY_COMPLETE_MARKER
        )
    except OSError:
        return False


def _refresh_cao_mcp_commands(data: Dict[str, Any]) -> None:
    """Refresh only verifiably CAO-owned local OpenCode MCP commands."""
    mcp_servers = data.get("mcp")
    if not isinstance(mcp_servers, dict):
        return

    for server_name, server_config in mcp_servers.items():
        if not isinstance(server_config, dict):
            continue
        profile_args = get_cao_mcp_server_profile_args(server_config.get("command"))
        # CAO owns the reserved entry name.  Versions before runtime snapshots
        # persisted an interpreter-sibling helper here; after that venv is
        # removed we cannot prove the old file's provenance, but the reserved
        # name remains CAO-managed and its trailing profile args are safe to
        # retain.  Other entries stay fail-closed and byte-for-byte untouched.
        if profile_args is None and server_name == CAO_MCP_SERVER_COMMAND:
            command = server_config.get("command")
            if (
                isinstance(command, list)
                and command
                and all(isinstance(part, str) for part in command)
            ):
                profile_args = list(command[1:])
        if profile_args is None:
            continue
        command, args = resolve_cao_mcp_command(CAO_MCP_SERVER_COMMAND, profile_args)
        server_config["command"] = [command, *args]


def _write_runtime_config(path: Path, data: Dict[str, Any]) -> None:
    """Atomically write a private config with owner-only file permissions."""
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as file:
            file.write(json.dumps(data, indent=2) + "\n")
            file.flush()
            os.fchmod(file.fileno(), 0o600)
            os.fsync(file.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def translate_mcp_server_config(cao_config: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a CAO mcpServer entry to OpenCode's ``mcp`` format.

    CAO profiles store MCP servers in Claude/Q CLI format::

        {"type": "stdio", "command": "uvx", "args": ["--from", "...", "cao-mcp-server"]}

    OpenCode ``opencode.json`` uses a different schema::

        {"type": "local", "command": ["uvx", "--from", "...", "cao-mcp-server"], "enabled": true}

    Differences:
    - ``type`` → always ``"local"`` (OpenCode's only supported subprocess type)
    - ``command`` (str) + ``args`` (list) → ``command`` (list, combined)
    - ``"enabled": true`` added
    - ``env`` → ``environment`` (OpenCode's key for process env vars)
    """
    # Keep the bundled helper in its canonical bare form in CAO's durable
    # template.  Every terminal snapshots and resolves it from the active
    # server process, so an upgrade cannot strand this config with an obsolete
    # virtual-environment sibling path.
    command_str = cao_config.get("command", "")
    args = cao_config.get("args", []) or []
    full_command: List[str] = ([command_str] if command_str else []) + list(args)

    result: Dict[str, Any] = {
        "type": "local",
        "command": full_command,
        "enabled": True,
    }
    if "env" in cao_config:
        result["environment"] = cao_config["env"]
    return result


def upsert_mcp_server(name: str, config: Dict[str, Any]) -> None:
    """Add or overwrite the MCP server entry named *name*.

    ``config`` must already be in OpenCode format (use
    ``translate_mcp_server_config`` to convert a CAO profile entry first).

    Also sets a default-deny entry ``"<name>*": false`` under the top-level
    ``tools`` section so new agents do not gain the server's tools by default.

    Name collisions silently overwrite the prior ``mcp`` entry.  The
    ``tools`` default-deny is always (re-)set to ``false``.
    """
    data = read_config()
    data.setdefault("mcp", {})[name] = config
    data.setdefault("tools", {})[f"{name}*"] = False
    write_config(data)


def upsert_agent_tools(agent_name: str, mcp_names: List[str]) -> None:
    """Set ``agent.<agent_name>.tools`` to re-enable the listed MCP servers.

    Creates or replaces the ``tools`` sub-dict for *agent_name*; other keys
    under ``agent.<agent_name>`` (if any) are preserved.
    """
    data = read_config()
    agents_section = data.setdefault("agent", {})
    agent_entry = agents_section.setdefault(agent_name, {})
    agent_entry["tools"] = {f"{name}*": True for name in mcp_names}
    write_config(data)


def remove_agent_tools(agent_name: str) -> None:
    """Remove the ``agent.<agent_name>`` section entirely.

    True no-op when the config file doesn't exist or the agent entry is absent
    — the file is not created just to record a removal.
    """
    if not OPENCODE_CONFIG_FILE.exists():
        return
    data = read_config()
    agents = data.get("agent")
    if not agents or agent_name not in agents:
        return
    agents.pop(agent_name)
    write_config(data)
