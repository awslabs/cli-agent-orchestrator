"""Validated Kiro Agent System profile models."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class KASPermissions(BaseModel):
    """Released-wrapper permission envelope for KAS agent profiles."""

    model_config = ConfigDict(extra="forbid")

    rules: List[str] = Field(default_factory=list)
    includePowers: bool = False
    excludedTools: List[str] = Field(default_factory=list)


class KASAgentConfig(BaseModel):
    """KAS-compatible agent profile using only evidence-confirmed fields."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    tools: List[str]
    permissions: KASPermissions
    resources: List[str] = Field(default_factory=list)
    prompt: Optional[str] = None
    mcpServers: Optional[Dict[str, Any]] = None
    hooks: Optional[Dict[str, Any]] = None
    model: Optional[str] = None
