"""Claude Code agent configuration model.

Converts AgentProfile fields to Claude Code CLI flags at runtime.
Used by ClaudeCodeProvider._build_claude_command() to extend the
base 'claude --agent <name>' command with additional flags like
--model, --allowedTools, --tools, and --settings (for hooks).
"""

import json
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ClaudeAgentConfig(BaseModel):
    """Claude Code agent configuration for CLI flag generation.

    Fields map to Claude Code CLI flags. MCP servers are excluded from
    to_cli_flags() because _inject_mcp_terminal_id in claude_code.py
    already handles --mcp-config separately.
    """

    name: str
    description: str

    # Optional pass-through fields mapped to CLI flags
    model: Optional[str] = None
    allowedTools: Optional[List[str]] = None
    tools: Optional[List[str]] = None
    mcpServers: Optional[Dict[str, Any]] = None
    hooks: Optional[Dict[str, Any]] = None

    def to_cli_flags(self, terminal_id: Optional[str] = None) -> List[str]:
        """Convert config fields to a list of Claude Code CLI flag pairs.

        Returns a flat list like ["--model", "sonnet", "--tools", "Bash,Edit"].
        When terminal_id is provided and mcpServers is set, injects CAO_TERMINAL_ID
        into each server's env and emits --mcp-config.
        """
        flags: List[str] = []

        if self.model:
            flags.extend(["--model", self.model])

        if self.allowedTools:
            flags.extend(["--allowedTools"] + self.allowedTools)

        if self.tools:
            flags.extend(["--tools", ",".join(self.tools)])

        if self.hooks:
            settings_json = json.dumps({"hooks": self.hooks})
            flags.extend(["--settings", settings_json])

        if self.mcpServers and terminal_id:
            mcp_config = {}
            for server_name, server_config in self.mcpServers.items():
                if isinstance(server_config, dict):
                    server = dict(server_config)
                else:
                    server = server_config.model_dump(exclude_none=True)
                env = server.get("env", {})
                if "CAO_TERMINAL_ID" not in env:
                    env["CAO_TERMINAL_ID"] = terminal_id
                    server["env"] = env
                mcp_config[server_name] = server
            flags.extend(["--mcp-config", json.dumps({"mcpServers": mcp_config})])

        return flags

    class Config:
        # Exclude None values when serializing to JSON
        exclude_none = True
