"""Kiro CLI agent configuration model."""

from typing import Any, Dict, List, Optional

from pydantic import AliasChoices, BaseModel, Field


class KiroAgentConfig(BaseModel):
    """Kiro CLI agent configuration."""

    name: str
    description: str
    tools: List[str] = Field(default_factory=lambda: ["*"])
    allowedTools: List[str] = Field(default_factory=list)
    # Serialized under the name Kiro CLI documents today. ``useLegacyMcpJson``
    # is the spelling Kiro CLI retired in favour of ``includeMcpJson``; it still
    # accepts the old one as an alias of the same field, so a spec that carries
    # BOTH is rejected with ``duplicate field includeMcpJson`` (#1026). This
    # field is non-Optional with a ``False`` default, so the writer's
    # ``model_dump_json(exclude_none=True)`` cannot drop it: every spec CAO
    # installs carries the key, and emitting the retired spelling made those
    # specs unloadable the moment any other writer added the canonical one.
    #
    # ``validation_alias`` keeps INPUT compatible both ways: a profile, fixture
    # or raw spec that says ``useLegacyMcpJson`` still loads, and so does the
    # canonical name. Only the OUTPUT spelling changes. The alias is
    # validation-only on purpose -- a ``serialization_alias`` would re-emit the
    # retired name, which is the defect itself. test/models/test_kiro_agent.py
    # pins both halves and keeps the retired spelling out of the field set.
    includeMcpJson: bool = Field(
        default=False,
        validation_alias=AliasChoices("includeMcpJson", "useLegacyMcpJson"),
    )
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
