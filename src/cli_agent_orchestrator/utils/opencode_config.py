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
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_FILE, SKILLS_DIR
from cli_agent_orchestrator.utils.mcp_resolution import resolve_cao_mcp_command

logger = logging.getLogger(__name__)

_SCHEMA = "https://opencode.ai/config.json"

# Per-role allowlists for cao-mcp-server tools, mirrored into OpenCode's
# per-agent ``tools`` config so a worker only carries the MCP tool schemas its
# role actually uses. OpenCode names MCP tools ``<server>_<tool>`` and agent
# ``tools`` entries override the global default-deny (``<server>*: false``),
# so listing the exact tools a role needs drops the other ~20 schemas (several
# KB of context) from that agent's session. Unknown roles fall back to the
# server-wide grant (``<server>*``) so behavior is unchanged for profiles that
# don't declare a role. Tool names are the ``cao-mcp-server`` tool names in
# mcp_server/server.py.
CAO_MCP_TOOLS_BY_ROLE: Dict[str, List[str]] = {
    # A supervisor delegates work: orchestration trio + sibling discovery +
    # memory. It does not need the workflow_run/events machinery (those are
    # for script-tier workers) or emit_ui.
    "supervisor": [
        "handoff",
        "assign",
        "send_message",
        "answer_user_prompt",
        "list_siblings",
        "update_metadata",
        "memory_store",
        "memory_recall",
        "memory_forget",
        "load_skill",
        "find_profiles",
        "delete_terminal",
    ],
    # A worker receives work and reports back: callback + memory + skill load.
    # It must NOT see handoff/assign (it would try to re-delegate), workflow_*
    # (script-tier machinery), or emit_ui.
    "developer": [
        "send_message",
        "memory_store",
        "memory_recall",
        "memory_forget",
        "load_skill",
        "list_siblings",
        "update_metadata",
    ],
    "reviewer": [
        "send_message",
        "memory_store",
        "memory_recall",
        "memory_forget",
        "load_skill",
    ],
}


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


def _agent_tool_grants(mcp_names: List[str], role: Optional[str] = None) -> Dict[str, bool]:
    """Compute the ``agent.<id>.tools`` grants for the given MCP servers.

    With a known role, grant only the cao-mcp-server tools that role uses
    (``<server>_<tool>: true``), so OpenCode only advertises those schemas to
    the agent. With an unknown role, fall back to the server-wide wildcard
    (``<server>*: true``) — the pre-allowlist behavior — so profiles without a
    role keep working unchanged. Non-cao MCP servers are always granted
    server-wide (CAO has no per-tool knowledge of third-party servers).
    """
    grants: Dict[str, bool] = {}
    role_tools = CAO_MCP_TOOLS_BY_ROLE.get(role or "")
    for name in mcp_names:
        if name == "cao-mcp-server" and role_tools is not None:
            for tool in role_tools:
                grants[f"{name}_{tool}"] = True
        else:
            grants[f"{name}*"] = True
    return grants


def upsert_agent_tools(agent_name: str, mcp_names: List[str], role: Optional[str] = None) -> None:
    """Set ``agent.<agent_name>.tools`` to re-enable the listed MCP servers.

    With a known ``role``, the cao-mcp-server grant is per-tool
    (``cao-mcp-server_<tool>: true``) so the agent only sees the schemas its
    role needs; unknown roles get the server-wide wildcard (backward
    compatible). Creates or replaces the ``tools`` sub-dict for *agent_name*;
    other keys under ``agent.<agent_name>`` (if any) are preserved.
    """
    data = read_config()
    agents_section = data.setdefault("agent", {})
    agent_entry = agents_section.setdefault(agent_name, {})
    agent_entry["tools"] = _agent_tool_grants(mcp_names, role)
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
