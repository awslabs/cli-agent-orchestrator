"""Agent profile models."""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "bypassPermissions"]


class McpServer(BaseModel):
    """MCP server configuration."""

    type: Optional[str] = None
    command: str
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    timeout: Optional[int] = None


class AgentProfile(BaseModel):
    """Agent profile configuration with Q CLI agent fields."""

    name: str
    description: str
    provider: Optional[str] = None  # Provider override (e.g. "claude_code", "kiro_cli")
    system_prompt: Optional[str] = None  # The markdown content
    role: Optional[str] = None  # "supervisor", "developer", "reviewer"

    # Q CLI agent fields (all optional, will be passed through to JSON)
    prompt: Optional[str] = None
    mcpServers: Optional[Dict[str, Any]] = None
    tools: Optional[List[str]] = Field(default=None)
    toolAliases: Optional[Dict[str, str]] = None
    allowedTools: Optional[List[str]] = None
    toolsSettings: Optional[Dict[str, Any]] = None
    resources: Optional[List[str]] = None
    hooks: Optional[Dict[str, Any]] = None
    useLegacyMcpJson: Optional[bool] = None
    model: Optional[str] = None
    permissionMode: Optional[PermissionMode] = None
    native_agent: Optional[str] = None  # Claude Code native agent name (thin-wrapper mode)

    # Codex-only. Names a [profiles.<name>] block in ~/.codex/config.toml.
    # Used as --profile <name> when yolo mode is not active; unrestricted
    # allowed tools still force --yolo. min_length=1 prevents an explicit
    # empty string from silently degrading to --yolo, since this is a
    # permission-floor knob.
    codexProfile: Optional[str] = Field(default=None, min_length=1)
