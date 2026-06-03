"""Skill management commands for CLI Agent Orchestrator."""

import shutil
from pathlib import Path

import click

from cli_agent_orchestrator.constants import SKILLS_DIR
from cli_agent_orchestrator.utils.skill_injection import refresh_all_cao_managed_agents
from cli_agent_orchestrator.utils.skills import (
    list_skills,
    validate_skill_folder,
    validate_skill_name,
)


def _install_skill_folder(source_dir: Path, force: bool = False) -> Path:
    """Validate and copy a skill folder into the local skill store."""
    metadata = validate_skill_folder(source_dir)
    skill_name = validate_skill_name(metadata.name)

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    destination_dir = SKILLS_DIR / skill_name

    if destination_dir.exists():
        if not force:
            raise FileExistsError(
                f"Skill '{skill_name}' already exists. Use --force to overwrite it."
            )
        shutil.rmtree(destination_dir)

    shutil.copytree(source_dir, destination_dir)
    return destination_dir


def _refresh_installed_agents() -> None:
    """Refresh baked prompts for installed CAO-managed Q/Copilot agents."""
    try:
        refreshed = refresh_all_cao_managed_agents()
    except Exception as exc:
        click.echo(f"Warning: failed to refresh installed agent prompts: {exc}", err=True)
        return

    if refreshed:
        click.echo(f"Refreshed {len(refreshed)} installed agent(s)")


@click.group()
def skills():
    """Manage installed skills."""


@skills.command("add")
@click.argument("folder_path", type=click.Path(exists=True, path_type=Path))
@click.option("--force", is_flag=True, help="Overwrite an existing installed skill.")
def add(folder_path: Path, force: bool) -> None:
    """Install a skill from a local folder path."""
    try:
        destination_dir = _install_skill_folder(folder_path, force=force)
        click.echo(f"Skill '{destination_dir.name}' installed successfully")
        _refresh_installed_agents()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@skills.command("remove")
@click.argument("name")
def remove(name: str) -> None:
    """Remove an installed skill."""
    try:
        skill_name = validate_skill_name(name)
        skill_dir = SKILLS_DIR / skill_name
        if not skill_dir.exists():
            raise FileNotFoundError(f"Skill '{skill_name}' does not exist.")
        if not skill_dir.is_dir():
            raise ValueError(f"Skill path is not a directory: {skill_dir}")

        shutil.rmtree(skill_dir)
        click.echo(f"Skill '{skill_name}' removed successfully")
        _refresh_installed_agents()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


@skills.command("list")
def list_command() -> None:
    """List installed skills."""
    try:
        installed_skills = list_skills()
        if not installed_skills:
            click.echo("No skills found")
            return

        click.echo(f"{'Name':<32} {'Description'}")
        click.echo("-" * 100)
        for skill in installed_skills:
            click.echo(f"{skill.name:<32} {skill.description}")
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
