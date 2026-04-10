"""Init command for CLI Agent Orchestrator CLI."""

import shutil
from importlib import resources
from pathlib import Path

import click

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.constants import SKILLS_DIR


def seed_default_skills() -> int:
    """Seed builtin skills (cao-supervisor-protocols, cao-worker-protocols) into the local skill store."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    bundled_skills = resources.files("cli_agent_orchestrator.skills")
    seeded_count = 0

    for skill_dir in bundled_skills.iterdir():
        if not skill_dir.is_dir():
            continue

        destination_dir = SKILLS_DIR / skill_dir.name
        if destination_dir.exists():
            continue

        with resources.as_file(skill_dir) as source_dir:
            shutil.copytree(Path(source_dir), destination_dir)
        seeded_count += 1

    return seeded_count


@click.command()
def init():
    """Initialize CLI Agent Orchestrator database."""
    try:
        init_db()
        seeded_count = seed_default_skills()
        click.echo(
            f"CLI Agent Orchestrator initialized successfully. "
            f"Seeded {seeded_count} builtin skills."
        )
    except Exception as e:
        raise click.ClickException(str(e))
