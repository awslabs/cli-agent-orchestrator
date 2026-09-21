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
from typing import Any, Dict, Iterable, List, Mapping, Set

from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_FILE, SKILLS_DIR
from cli_agent_orchestrator.utils.mcp_resolution import (
    resolve_cao_mcp_command,
    shared_endpoint_child_env,
)
from cli_agent_orchestrator.utils.path_validation import flatten_path_separators

logger = logging.getLogger(__name__)

_SCHEMA = "https://opencode.ai/config.json"

#: Sidecar that records which ``agent.<id>.tools`` keys CAO granted.
#:
#: Kept *beside* ``opencode.json`` rather than inside it because OpenCode owns
#: that file's schema: an ``x-cao-grants`` key under ``agent.<id>`` would put CAO
#: bookkeeping into a user-visible, user-editable entry, and the sibling ``mcp``
#: entries forbid extra properties outright (``additionalProperties: false``), so
#: there is no consistent in-file place to put it. Kept beside the config rather
#: than in the plugin store because the two must be deleted together: a wiped
#: ``CAO_HOME_DIR`` with a surviving ``opencode.json`` is exactly the state in
#: which stale grants would otherwise become unremovable.
#:
#: What the sidecar buys that plugin-store containment does not: a grant for a
#: server the *profile* declares — ``cao-mcp-server*`` above all — whose ``mcp``
#: entry never resolves inside the store and so can never be proven CAO's from
#: the config bytes alone.
GRANT_RECORD_FILENAME = "cao-grants.json"

_EMPTY_GRANT_RECORD: Dict[str, Any] = {"version": 1, "agents": {}}


def to_opencode_agent_id(profile_name: str) -> str:
    """Derive the OpenCode agent ID from a CAO profile name.

    OpenCode treats the filename stem of an agent ``.md`` file as its agent ID
    (used for ``--agent <id>`` and keyed by the same value under
    ``agent.<id>`` in ``opencode.json``).

    Since the id becomes the ``<id>.md`` filename, any path separator left in it
    would traverse out of ``OPENCODE_AGENTS_DIR``, so both ``/`` and ``\\`` are
    flattened to ``__`` (backslash included because it is a separator on
    Windows). ``install_service`` now *rejects* a resolved profile ``name:``
    containing a separator outright, so this flatten is defence in depth rather
    than the primary guard — and a namespaced ``name:`` is no longer installable.

    The output is the single source of truth for:

    - the installed ``<id>.md`` filename under ``OPENCODE_AGENTS_DIR``
    - the ``agent.<id>.tools`` key written to ``opencode.json``
    - the value passed to ``opencode --agent <id>`` at runtime

    Idempotent: inputs that contain no separator are returned unchanged.
    """
    return flatten_path_separators(profile_name)


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
    - ``cwd`` → ``cwd`` (OpenCode ``McpLocalConfig.cwd``; agent-plugin servers
      default to their ``PLUGIN_ROOT``)

    Reproduced by review 3 on #584: ``cwd`` was dropped here, so a plugin whose
    ``command``/``args`` are relative to its working directory started in
    OpenCode's workspace directory instead. The mapper always supplies an
    absolute, contained ``cwd`` for plugin servers
    (``agent_plugins/mcp_mapping.py``), so OpenCode's "relative paths resolve from
    the workspace directory" rule never applies to those. A profile-declared
    ``cwd`` is passed through as the author wrote it — ``AgentProfile.mcpServers``
    is ``Dict[str, Any]``, so CAO does not own that value.
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
    environment = dict(cao_config.get("env") or {})
    # The resolver may have swapped in the forwarding shim (#745), which
    # needs the endpoint and token here. Empty when none is configured, so
    # an entry that had no "env" still gets no "environment" key.
    environment.update(shared_endpoint_child_env())
    if environment:
        result["environment"] = environment
    # Emitted only when the source actually has one: an invented `cwd` would
    # change where a profile-declared server runs, and an empty string is not a
    # directory. OpenCode's `mcp` entries are `additionalProperties: false`, so
    # `cwd` is the one legal place for this and no marker key may join it.
    cwd = cao_config.get("cwd")
    if isinstance(cwd, str) and cwd:
        result["cwd"] = cwd
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


def _grant_record_path() -> Path:
    """Where the grant sidecar lives, derived from ``OPENCODE_CONFIG_FILE`` at call time.

    Deliberately *not* a module constant. ``OPENCODE_CONFIG_FILE`` is fixed at
    ``~/.aws/opencode/opencode.json`` in production (there is no override for it),
    but every test fixture isolates OpenCode state by rebinding this module's
    global — and a path computed at import time would ignore that rebind and write
    the developer's real home. Deriving it per call is what keeps the sidecar
    following the config file it belongs to.
    """
    return OPENCODE_CONFIG_FILE.with_name(GRANT_RECORD_FILENAME)


def read_grant_record() -> Dict[str, Any]:
    """Load the grant sidecar, degrading to an empty record.

    A missing sidecar is the normal pre-fix / first-install state and is silent.
    A corrupt or wrongly-shaped one is *warned about and ignored* rather than
    raised on: the sidecar exists to let CAO withdraw its own grants, so a
    damaged one must degrade to "CAO can only prove ownership from the plugin
    store" — never to a failed install.
    """
    path = _grant_record_path()
    if not path.exists():
        return dict(_EMPTY_GRANT_RECORD, agents={})
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("opencode grant record at %s is unreadable (%s) — ignoring", path, exc)
        return dict(_EMPTY_GRANT_RECORD, agents={})
    if not isinstance(loaded, dict) or not isinstance(loaded.get("agents"), dict):
        logger.warning("opencode grant record at %s has an unexpected shape — ignoring", path)
        return dict(_EMPTY_GRANT_RECORD, agents={})
    return loaded


def _write_grant_record(record: Dict[str, Any]) -> None:
    """Persist the grant sidecar atomically (temp file + ``replace``).

    Same discipline as ``InstalledPluginStore.write_record``: a torn write here
    would look like a corrupt record on the next read and silently downgrade CAO
    to plugin-store-only ownership, leaking grants.

    Best effort, symmetrically with :func:`read_grant_record`. By the time this
    runs ``opencode.json`` has already been updated, so raising would report the
    install as *failed* after the user-visible half succeeded — and the cost of a
    lost sidecar is only that CAO falls back to the containment proof, which is
    exactly the pre-sidecar behaviour. An unwritable sidecar must not be worse
    than an absent one.
    """
    path = _grant_record_path()
    temp_path = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)
    except OSError as exc:
        logger.warning(
            "Could not record OpenCode tool grants at %s (%s); CAO will fall back to "
            "plugin-store containment when withdrawing them",
            path,
            exc,
        )


def _recorded_grants(agent_name: str) -> Set[str]:
    """The ``tools`` keys the sidecar says CAO granted to *agent_name*."""
    agents = read_grant_record().get("agents", {})
    recorded = agents.get(agent_name) if isinstance(agents, dict) else None
    if not isinstance(recorded, list):
        return set()
    return {key for key in recorded if isinstance(key, str)}


def _record_grants(agent_name: str, keys: Iterable[str]) -> None:
    """Set (or clear) the sidecar row for *agent_name*, writing only on change."""
    record = read_grant_record()
    agents = record.setdefault("agents", {})
    if not isinstance(agents, dict):  # pragma: no cover - read_grant_record guarantees dict
        agents = {}
        record["agents"] = agents
    desired = sorted(set(keys))
    if desired:
        if agents.get(agent_name) == desired:
            return
        agents[agent_name] = desired
    else:
        if agent_name not in agents:
            return
        agents.pop(agent_name)
    record["version"] = 1
    _write_grant_record(record)


def _reconcile_agent_grants(
    agent_name: str,
    mcp_names: Iterable[str],
    plugin_store_roots: Iterable[Path],
    *,
    prune_empty_agent: bool,
) -> None:
    """Bring ``agent.<agent_name>.tools`` in line with *mcp_names*, touching only CAO's keys.

    The set CAO may take away is the union of two provable sources — never "every
    key in the map":

    * what the sidecar records CAO having granted to this agent, and
    * ``<server>*`` for any ``mcp`` entry whose paths resolve inside the plugin
      store, which is CAO-managed by construction (this is what keeps a
      pre-sidecar install, or one whose sidecar was lost, still cleanable).

    Everything else in ``tools`` is the user's and is copied through untouched.
    """
    roots = list(plugin_store_roots)
    owned_now = {f"{name}*": True for name in mcp_names}

    data = read_config()
    before = json.dumps(data, sort_keys=True)

    owned_before: Set[str] = _recorded_grants(agent_name)
    if not owned_now and not owned_before and _agent_entry_absent(data, agent_name):
        # Nothing to grant, nothing recorded to withdraw, and no entry to prune.
        # Returning here keeps the removal path a *true* no-op: the reconcile below
        # would otherwise materialise an empty `"agent": {}` section into a config
        # that never had one, which the pre-fix `remove_agent_tools` did not do.
        return
    servers = data.get("mcp")
    if isinstance(servers, dict):
        for server_name, entry in servers.items():
            if isinstance(entry, Mapping) and entry_within_roots(entry, roots):
                owned_before.add(f"{server_name}*")

    agents_section = data.get("agent")
    if not isinstance(agents_section, dict):
        agents_section = {}
        data["agent"] = agents_section
    agent_entry = agents_section.get(agent_name)
    if not isinstance(agent_entry, dict):
        agent_entry = {}
        agents_section[agent_name] = agent_entry
    tools = agent_entry.get("tools")
    if not isinstance(tools, dict):
        tools = {}
    else:
        tools = dict(tools)

    for withdrawn in owned_before - set(owned_now):
        tools.pop(withdrawn, None)
    tools.update(owned_now)
    agent_entry["tools"] = tools

    if prune_empty_agent and set(agent_entry) <= {"tools"} and not tools:
        # Nothing of the user's left to keep alive: the entry existed only to
        # carry CAO's grant. Dropping it restores the pre-install shape instead
        # of leaving `{"tools": {}}` litter behind.
        agents_section.pop(agent_name, None)

    if json.dumps(data, sort_keys=True) != before:
        write_config(data)
    _record_grants(agent_name, owned_now)


def upsert_agent_tools(
    agent_name: str,
    mcp_names: List[str],
    *,
    plugin_store_roots: Iterable[Path] = (),
) -> None:
    """Grant *agent_name* the listed MCP servers by merging into ``agent.<agent_name>.tools``.

    Reproduced by review 3 on #584: this replaced the whole ``tools`` map with
    ``{f"{name}*": True for name in mcp_names}``, so any tool policy the user had
    written for a CAO-installed agent — ``"bash": false``, a ``"user*": true`` for
    their own server — was destroyed on every install, refresh and reinstall.
    Keys CAO did not put there are now preserved; only keys CAO can *prove* are
    its own (``plugin_store_roots`` containment, or the ``cao-grants.json``
    sidecar) are withdrawn when they leave the desired set.

    ``plugin_store_roots`` defaults to empty so a caller that has no store handy
    still gets correct merge behaviour — it simply relies on the sidecar alone
    for withdrawal, which is the conservative direction (a grant CAO cannot prove
    it owns is left in place, never deleted).
    """
    _reconcile_agent_grants(agent_name, mcp_names, plugin_store_roots, prune_empty_agent=False)


def remove_agent_tools(agent_name: str, *, plugin_store_roots: Iterable[Path] = ()) -> None:
    """Withdraw every grant CAO gave *agent_name*, leaving the user's keys alone.

    Reproduced by review 3 on #584: this popped the entire ``agent.<agent_name>``
    entry, discarding a user's ``model``, ``prompt``, ``temperature`` and tool
    policy for an agent CAO merely installed. The entry is now dropped only when
    withdrawing CAO's grants empties it completely — i.e. when it never held
    anything but CAO's ``tools`` keys.

    True no-op when the config file doesn't exist — the file is not created just
    to record a removal.
    """
    if not OPENCODE_CONFIG_FILE.exists():
        return
    _reconcile_agent_grants(agent_name, (), plugin_store_roots, prune_empty_agent=True)


def _agent_entry_absent(data: Mapping[str, Any], agent_name: str) -> bool:
    """Whether ``agent.<agent_name>`` is not present in a parsed config."""
    agents = data.get("agent")
    return not isinstance(agents, dict) or agent_name not in agents


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
