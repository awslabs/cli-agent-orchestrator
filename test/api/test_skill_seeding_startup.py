"""Server startup tests for idempotent builtin skill seeding."""

import logging
from pathlib import Path

from cli_agent_orchestrator.api.main import _seed_default_skills_at_startup


def _write_skill(root: Path, name: str, description: str) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n\n" "# Bundled Skill\n"
    )


def test_startup_seeds_new_builtin_into_existing_older_store(tmp_path, monkeypatch, caplog) -> None:
    """A restart after upgrade adds new builtins and preserves existing edits."""
    bundled_root = tmp_path / "bundled"
    _write_skill(bundled_root, "cao-worker-protocols", "Bundled worker")
    _write_skill(bundled_root, "cao-agent-routing", "New routing skill")

    skill_store = tmp_path / "skill-store"
    existing = skill_store / "cao-worker-protocols"
    existing.mkdir(parents=True)
    existing_text = "---\nname: cao-worker-protocols\ndescription: User edit\n---\n"
    (existing / "SKILL.md").write_text(existing_text)

    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.init.SKILLS_DIR", skill_store)
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.init.resources.files",
        lambda _: bundled_root,
    )

    with caplog.at_level(logging.INFO, logger="cli_agent_orchestrator.api.main"):
        _seed_default_skills_at_startup()
        _seed_default_skills_at_startup()

    assert (existing / "SKILL.md").read_text() == existing_text
    assert (skill_store / "cao-agent-routing" / "SKILL.md").is_file()
    assert caplog.text.count("Seeded 1 new builtin skill(s).") == 1


def test_startup_skill_seeding_failure_does_not_block_server_startup(monkeypatch, caplog) -> None:
    def fail_seeding() -> int:
        raise PermissionError("read-only store")

    monkeypatch.setattr(
        "cli_agent_orchestrator.api.main.seed_default_skills",
        fail_seeding,
    )

    with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.api.main"):
        _seed_default_skills_at_startup()

    assert "automatic builtin skill seeding failed (PermissionError)" in caplog.text
    assert "cao init" in caplog.text
