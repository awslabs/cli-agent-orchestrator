"""Agent profile models."""

from typing import Dict, List, Optional
from pydantic import BaseModel


class McpServer(BaseModel):
    """MCP server configuration."""
    type: str
    command: str
    args: List[str]


class AgentProfile(BaseModel):
    """Agent profile configuration."""
    name: str
    description: str
    model: str
    system_prompt: str  # The markdown content
    mcpServers: Optional[Dict[str, McpServer]] = None
