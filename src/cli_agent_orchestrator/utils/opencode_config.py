"""Read-modify-write helper for the shared ``opencode.json`` config file.

Provides idempotent upsert operations for MCP server declarations and per-agent
tool gating, as described in §6 of docs/feat-opencode-provider-design.md.

No file locking is applied in v1; concurrent ``cao install --provider opencode_cli``
invocations are not a supported scenario (see §6 "Concurrent-write policy").
"""

import json
from pathlib import Path
from typing import Any, Dict, List

from cli_agent_orchestrator.constants import OPENCODE_CONFIG_FILE

_SCHEMA = "https://opencode.ai/config.json"


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


def upsert_mcp_server(name: str, config: Dict[str, Any]) -> None:
    """Add or overwrite the MCP server entry named *name*.

    Also sets a default-deny entry ``"<name>*": false`` under the top-level
    ``tools`` section so new agents do not gain the server's tools by default.

    Per §6: name collisions silently overwrite the prior ``mcp`` entry.  The
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

    No-ops if the agent entry does not exist.
    """
    data = read_config()
    data.get("agent", {}).pop(agent_name, None)
    write_config(data)
