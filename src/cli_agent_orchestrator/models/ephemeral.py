"""Closed creator inputs; resolved launch values never replace declared spec fields."""

import unicodedata
from typing import Any, Literal, Optional, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cli_agent_orchestrator.models.provider import ProviderType

ToolAtom = Literal["fs_read", "fs_list", "fs_write", "execute_bash", "web_fetch"]
ModelTier = Literal["small", "medium", "large", "auto"]
Effort = Literal["low", "medium", "high", "auto"]
TOOL_ATOMS = ("fs_read", "fs_list", "fs_write", "execute_bash", "web_fetch")


class EphemeralSpec(BaseModel):
    """Untrusted creator text plus a closed set of capability choices."""

    model_config = ConfigDict(extra="forbid", strict=True)

    spec_version: Literal[1] = 1
    purpose: str = Field(pattern=r"^[a-z][a-z0-9_]{2,31}$")
    brief: str
    description: Optional[str] = Field(default=None, max_length=280)
    provider: Optional[ProviderType] = None
    tools: Optional[list[ToolAtom]] = None
    model_tier: Optional[ModelTier] = None
    effort: Optional[Effort] = None

    @field_validator("provider", mode="before")
    @classmethod
    def provider_is_known(cls, value: Any) -> Optional[ProviderType]:
        if value is None or isinstance(value, ProviderType):
            return value
        if isinstance(value, str):
            return ProviderType(value)
        raise ValueError("provider must be a known provider id")

    @field_validator("spec_version", mode="before")
    @classmethod
    def version_is_an_integer(cls, value: Any) -> int:
        if type(value) is not int:
            raise ValueError("version must be an integer")
        return cast(int, value)

    @field_validator("purpose", "description")
    @classmethod
    def text_has_no_controls(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("control characters are not permitted")
        return value
