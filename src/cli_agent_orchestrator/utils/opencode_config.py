"""Read-modify-write helper for the shared ``opencode.json`` config file.

Provides idempotent upsert operations for MCP server declarations and per-agent
tool gating, plus the ``to_opencode_agent_id`` helper that derives a single
slash-safe identifier used consistently for the installed ``.md`` filename,
the runtime ``--agent`` argument, and the ``agent.<id>.tools`` key.

No file locking is applied; concurrent ``cao install --provider opencode_cli``
invocations are not a supported scenario.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_FILE, SKILLS_DIR
from cli_agent_orchestrator.utils.mcp_resolution import resolve_cao_mcp_command

logger = logging.getLogger(__name__)

_SCHEMA = "https://opencode.ai/config.json"


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
    # Resolve the bundled cao-mcp-server console script to a PATH-independent
    # invocation before flattening into OpenCode's command list.
    # persisted=True: OpenCode reads this from opencode.json at launch, so prefer
    # the stable PATH launcher over a versioned venv path that upgrades relocate.
    command_str, args = resolve_cao_mcp_command(
        cao_config.get("command", ""), cao_config.get("args", []) or [], persisted=True
    )
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


# ── removal + install-side ownership guard (design.md §10a) ──────────────────
#
# OpenCode's shared ``opencode.json`` is edited in place: ``upsert_mcp_server``
# is upsert-only and there is no delete. Two consequences the helpers below fix:
#
#   * Removal (Finding 1). A plugin server no longer delivered must be DISABLED,
#     not deleted (CAO must never delete a key it may not own) and not left
#     ``enabled: true`` (its ``command`` points at a ``PLUGIN_ROOT`` that removal
#     just deleted, so OpenCode would try to spawn a missing executable on every
#     launch). ``disable_mcp_server`` writes a JSON boolean ``false`` — OpenCode
#     1.18.15 gates the spawn on a strict ``enabled === false``, so ``"false"``,
#     ``0`` or a missing ``type`` would NOT stop it (see
#     ``docs/issues/573-agent-plugins/opencode-verification.md``).
#
#   * Install-side clobber (Finding 2). Writing a plugin-derived server whose
#     name collides with a user's hand-written entry would silently destroy the
#     user's config. ``is_cao_owned_mcp_entry`` decides whether an existing entry
#     is safe to overwrite.
#
# PROVENANCE IS A HEURISTIC, NOT A FACT. ``opencode.json`` records no owner, so
# the only signal available without persisted state (install-record / marker
# key — design.md §10a options 1 and 2, deliberately out of scope here) is that
# a CAO-delivered plugin server's ``command``/``environment`` were expanded from
# ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` and therefore point INSIDE the plugin
# store. A *stale* CAO entry written under a different ``CAO_HOME_DIR`` points at
# the OLD store and is thus indistinguishable from a user's entry; it is treated
# conservatively (reported as a collision, never overwritten).


def _entry_path_strings(config: Mapping[str, Any]) -> List[str]:
    """Every string in an OpenCode ``mcp`` entry that may be a filesystem path.

    The ``command`` list elements, the ``environment`` values (this is where a
    CAO-delivered plugin server carries ``PLUGIN_ROOT``/``PLUGIN_DATA``), and
    ``cwd``. Non-string members are ignored rather than coerced.
    """
    paths: List[str] = []
    command = config.get("command")
    if isinstance(command, list):
        paths.extend(part for part in command if isinstance(part, str))
    environment = config.get("environment")
    if isinstance(environment, dict):
        paths.extend(value for value in environment.values() if isinstance(value, str))
    cwd = config.get("cwd")
    if isinstance(cwd, str):
        paths.append(cwd)
    return paths


def entry_within_roots(config: Mapping[str, Any], roots: Iterable[Path]) -> bool:
    """Whether any path in an OpenCode ``mcp`` entry is located inside one of ``roots``.

    Purely lexical (``Path.relative_to``): the path need not still exist on disk,
    which is exactly the post-uninstall state — the ``PLUGIN_ROOT`` directory is
    gone but the recorded command still textually points into the plugin store.
    ``roots`` are passed in rather than imported so this stays decoupled from the
    plugin store and testable against a scratch tree.
    """
    root_list = [Path(root) for root in roots]
    if not root_list:
        return False
    for raw in _entry_path_strings(config):
        # No try/except around `Path(raw)`: `_entry_path_strings` already filters
        # to `str`, and `Path(str)` does not raise on any supported platform, so a
        # guard here would be unreachable code pretending to be defensive.
        candidate = Path(raw)
        for root in root_list:
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
    return False


def is_cao_owned_mcp_entry(
    existing: Mapping[str, Any],
    candidate: Mapping[str, Any],
    *,
    plugin_store_roots: Iterable[Path],
) -> bool:
    """Whether CAO may overwrite ``existing`` with ``candidate`` without data loss.

    Three ways an existing entry is provably CAO's, and therefore safe to replace:

    1. It is byte-for-byte what CAO would write now (idempotent replay — a
       reinstall/refresh re-writes CAO's own entry every time, and that must not
       be misreported as a user collision or the server could never be delivered
       twice). Overwriting an entry equal to the candidate is a no-op anyway.
    2. Its command resolves **inside the plugin store**, whatever its
       ``enabled`` state. The plugin store is a CAO-managed directory that only
       CAO ever writes into, so a command rooted there is CAO's own delivery by
       construction — this is the recorded-ownership signal, derived from the
       very bytes being compared rather than from a side table that can go stale
       or be lost while ``opencode.json`` persists.
    3. (Subsumed by 2, kept explicit for readers) a CAO-*disabled* entry inside
       the plugin store — a server CAO delivered and then disabled on a prior
       uninstall. Re-enabling it on reinstall is safe.

    Anything else — an entry that differs and does not resolve into the plugin
    store — is treated as user-owned and must NOT be overwritten.

    Reproduced by review on #584: condition 2 was previously gated on
    ``enabled is False``, so a force update that changed a plugin server's
    command, args or env made the entry differ from both the candidate and the
    disabled shape. CAO then misclassified **its own** enabled entry as
    user-owned, left the stale v1 command in ``opencode.json`` and dropped the
    agent's tool grant because the v2 entry was skipped. The same path broke a
    lexicographic winner transition between two plugins. Widening the ownership
    test to any in-store command lets CAO update its own entries while leaving
    the guard at full strength for genuinely user-authored ones, which by
    definition do not point into CAO's plugin store.
    """
    if dict(existing) == dict(candidate):
        return True
    if entry_within_roots(existing, plugin_store_roots):
        return True
    return False


def disable_mcp_server(name: str) -> None:
    """Set ``mcp.<name>.enabled`` to a JSON boolean ``false``, in place.

    Option 3 of design.md §10a: removal disables a CAO-delivered server rather
    than deleting it, so CAO never removes a key it may not own. Every other
    field of the entry is left untouched.

    Symmetric with ``upsert_mcp_server`` in read/modify/write discipline, and a
    true no-op — matching ``remove_agent_tools`` — when the config file is
    absent, the ``mcp`` section or the named entry is absent, or the entry is
    already disabled. The file is not created just to record a disable.
    """
    if not OPENCODE_CONFIG_FILE.exists():
        return
    data = read_config()
    servers = data.get("mcp")
    if not isinstance(servers, dict) or name not in servers:
        return
    entry = servers[name]
    if not isinstance(entry, dict) or entry.get("enabled") is False:
        return
    entry["enabled"] = False
    write_config(data)
