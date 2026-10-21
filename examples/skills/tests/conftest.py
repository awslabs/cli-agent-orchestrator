"""Fixtures for the skills-lifecycle example's deterministic tests.

Mirrors the isolation used by ``test/utils/test_skills.py``: ``SKILLS_DIR`` is
a module-level import in both ``utils.skills`` and ``cli.commands.skills``, so
both bindings must be patched, and ``extra_skill_dirs`` must be pinned to
``[]`` so the operator's real ``settings.json`` cannot leak into the run.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    """A fake CAO home whose ``skills/`` replaces every skill surface.

    Returned as the home directory (not the store) so tests can point a
    subprocess at it via ``CAO_HOME_DIR`` — constants.py reads that env var
    once at import, which is exactly how run.sh isolates the example too.
    """
    home = tmp_path / "cao-home"
    store = home / "skills"
    store.mkdir(parents=True)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", store)
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", store)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs", lambda: []
    )
    return home


@pytest.fixture()
def isolated_skill_store(isolated_home):
    """The isolated skill store inside :func:`isolated_home`."""
    return isolated_home / "skills"
