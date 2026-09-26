"""Skill projection mode settings — the ``symlink``/``copy`` accessor pair.

``copy`` mode exists for environments where symlink creation is unsupported
(Windows without Developer Mode or elevation). It is the mode in which the
projection-ownership defect reproduced, so its accessors being exercised is not
merely a coverage concern: an unreadable or hand-edited ``settings.json`` must
degrade to ``symlink`` rather than raising, because raising here would take plugin
installation down with it.
"""

from __future__ import annotations

import json

import pytest

from cli_agent_orchestrator.services import settings_service


@pytest.fixture
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_service, "SETTINGS_FILE", path, raising=False)
    for attr in ("SETTINGS_PATH", "_SETTINGS_FILE"):
        if hasattr(settings_service, attr):
            monkeypatch.setattr(settings_service, attr, path, raising=False)
    return path


class TestGetSkillProjectionMode:
    def test_defaults_to_symlink_when_unset(self, settings_file):
        assert settings_service.get_skill_projection_mode() == "symlink"

    @pytest.mark.parametrize("stored,expected", [("copy", "copy"), ("symlink", "symlink")])
    def test_returns_a_valid_stored_mode(self, settings_file, stored, expected):
        settings_file.write_text(json.dumps({"skills": {"projection_mode": stored}}))
        assert settings_service.get_skill_projection_mode() == expected

    @pytest.mark.parametrize("stored", ["  COPY  ", "Symlink", "SYMLINK"])
    def test_case_and_whitespace_are_normalized(self, settings_file, stored):
        settings_file.write_text(json.dumps({"skills": {"projection_mode": stored}}))
        assert settings_service.get_skill_projection_mode() == stored.strip().lower()

    @pytest.mark.parametrize("stored", ["hardlink", "", 42, None, True, ["copy"], {"a": 1}])
    def test_an_unrecognized_value_falls_back_instead_of_raising(self, settings_file, stored):
        """A hand-edited settings file must not be able to break installation."""
        settings_file.write_text(json.dumps({"skills": {"projection_mode": stored}}))
        assert settings_service.get_skill_projection_mode() == "symlink"

    def test_a_non_object_skills_section_falls_back(self, settings_file):
        """`skills` is shared with extra_dirs; tolerate a wrong shape."""
        settings_file.write_text(json.dumps({"skills": ["not", "an", "object"]}))
        assert settings_service.get_skill_projection_mode() == "symlink"


class TestSetSkillProjectionMode:
    @pytest.mark.parametrize("value,expected", [("copy", "copy"), ("  SYMLINK ", "symlink")])
    def test_persists_a_normalized_mode(self, settings_file, value, expected):
        assert settings_service.set_skill_projection_mode(value) == expected
        # Round-trips through the reader, not just through the return value.
        assert settings_service.get_skill_projection_mode() == expected

    @pytest.mark.parametrize("value", ["hardlink", "", None, "sym link"])
    def test_rejects_an_invalid_mode(self, settings_file, value):
        with pytest.raises(ValueError, match="projection_mode"):
            settings_service.set_skill_projection_mode(value)

    def test_a_rejected_mode_leaves_the_stored_value_untouched(self, settings_file):
        settings_service.set_skill_projection_mode("copy")
        with pytest.raises(ValueError):
            settings_service.set_skill_projection_mode("nonsense")
        assert settings_service.get_skill_projection_mode() == "copy"

    def test_it_replaces_a_non_object_skills_section(self, settings_file):
        settings_file.write_text(json.dumps({"skills": "wrong-shape"}))
        assert settings_service.set_skill_projection_mode("copy") == "copy"
        assert settings_service.get_skill_projection_mode() == "copy"

    def test_it_preserves_sibling_settings(self, settings_file):
        """The write must not clobber extra_dirs or unrelated top-level keys."""
        settings_file.write_text(
            json.dumps({"skills": {"extra_dirs": ["/a/b"]}, "unrelated": {"keep": True}})
        )
        settings_service.set_skill_projection_mode("copy")

        data = json.loads(settings_file.read_text())
        assert data["skills"]["extra_dirs"] == ["/a/b"]
        assert data["skills"]["projection_mode"] == "copy"
        assert data["unrelated"] == {"keep": True}
