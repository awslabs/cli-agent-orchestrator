"""Skill management commands for CLI Agent Orchestrator."""

import shutil
from pathlib import Path

import click

from cli_agent_orchestrator.agent_plugins.projection import (
    ProjectionClaimError,
    release_projection_claim,
)
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

    if destination_dir.exists() or destination_dir.is_symlink():
        if not force:
            raise FileExistsError(
                f"Skill '{skill_name}' already exists. Use --force to overwrite it."
            )

        # A user install that replaces a plugin's projected skill takes ownership
        # of the name. Without this the install record kept claiming it, so a
        # later `cao plugin remove` deleted the user's directory and any
        # projection rebuild overwrote it with the plugin's copy.
        #
        # Ordering is load-bearing: the transfer must be *committed* before
        # anything on disk is touched. A release that failed used to be
        # indistinguishable from "no plugin held this name", so the install went
        # ahead, unlinked the projection and copied the user's folder into place
        # while the record still claimed the name — and the next rebuild's sweep
        # deleted that folder. `ProjectionClaimError` is the third state; on it
        # the install aborts with nothing changed.
        try:
            released = release_projection_claim(skill_name)
        except ProjectionClaimError as exc:
            raise RuntimeError(
                f"Refusing to install skill '{skill_name}': it is currently provided by "
                f"an agent plugin, and that plugin's install record could not be updated "
                f"to give up its claim ({exc}). Nothing was changed. Fix the underlying "
                f"problem — most often an unwritable or full agent-plugin state directory "
                f"— and retry."
            ) from exc
        if released:
            click.echo(
                f"Skill '{skill_name}' was provided by agent plugin '{released}'; "
                f"it is now user-owned and will win future collisions."
            )

        # `shutil.rmtree` refuses a symbolic link, which is exactly what a
        # symlink-mode projection is — so the two cases are removed differently.
        if destination_dir.is_symlink():
            destination_dir.unlink()
        else:
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
