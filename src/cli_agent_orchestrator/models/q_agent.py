"""Q CLI agent configuration model."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class QAgentConfig(BaseModel):
    """Q CLI agent configuration."""

    name: str
    description: str
    tools: List[str] = Field(default_factory=lambda: ["*"])
    allowedTools: List[str] = Field(default_factory=list)
    useLegacyMcpJson: bool = False
    resources: List[str] = Field(default_factory=list)

    # Optional pass-through fields
    prompt: Optional[str] = None
    mcpServers: Optional[Dict[str, Any]] = None
    toolAliases: Optional[Dict[str, str]] = None
    toolsSettings: Optional[Dict[str, Any]] = None
    hooks: Optional[Dict[str, Any]] = None
    model: Optional[str] = None

    class Config:
        # Exclude None values when serializing to JSON
        exclude_none = True
