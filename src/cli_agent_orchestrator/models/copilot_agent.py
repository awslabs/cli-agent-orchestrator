"""Copilot CLI agent configuration model."""

from pydantic import BaseModel


class CopilotAgentConfig(BaseModel):
    """Copilot CLI agent configuration."""

    name: str
    description: str
    prompt: str

    class Config:
        # Keep model config style consistent with other provider models.
        exclude_none = True
