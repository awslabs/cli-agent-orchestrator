"""Tests for skill metadata models."""

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.skill import SkillMetadata


class TestSkillMetadata:
    """Tests for the SkillMetadata model."""

    def test_constructs_with_valid_name_and_description(self):
        """Model should accept non-empty name and description fields."""
        skill = SkillMetadata(name="python-testing", description="Pytest conventions")

        assert skill.name == "python-testing"
        assert skill.description == "Pytest conventions"

    @pytest.mark.parametrize(
        ("payload", "field_name"),
        [
            ({"description": "Missing name"}, "name"),
            ({"name": "missing-description"}, "description"),
            ({"name": "", "description": "desc"}, "name"),
            ({"name": "   ", "description": "desc"}, "name"),
            ({"name": "skill", "description": ""}, "description"),
            ({"name": "skill", "description": "   "}, "description"),
        ],
    )
    def test_rejects_missing_or_empty_fields(self, payload, field_name):
        """Model should reject missing or blank required fields."""
        with pytest.raises(ValidationError) as exc_info:
            SkillMetadata(**payload)

        assert field_name in str(exc_info.value)

    def test_strips_surrounding_whitespace(self):
        """Model should normalize surrounding whitespace for required fields."""
        skill = SkillMetadata(name="  code-style  ", description="  Shared conventions  ")

        assert skill.name == "code-style"
        assert skill.description == "Shared conventions"
