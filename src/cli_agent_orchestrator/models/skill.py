"""Skill metadata models."""

from pydantic import BaseModel, field_validator


class SkillMetadata(BaseModel):
    """Metadata describing an installed skill."""

    name: str
    description: str

    @field_validator("name", "description")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """Ensure required string fields are present and not blank."""
        stripped_value = value.strip()
        if not stripped_value:
            raise ValueError("Field must not be empty")
        return stripped_value
