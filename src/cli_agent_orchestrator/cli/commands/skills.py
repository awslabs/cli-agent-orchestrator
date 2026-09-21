"""Skill management commands for CLI Agent Orchestrator."""

import contextlib
import shutil
from pathlib import Path

import click

from cli_agent_orchestrator.agent_plugins import installer
from cli_agent_orchestrator.agent_plugins.projection import (
    MARKER_FILENAME,
    ProjectionClaimError,
    projection_owner,
    release_projection_claim,
)
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.constants import SKILLS_DIR
from cli_agent_orchestrator.utils.skill_injection import refresh_all_cao_managed_agents
from cli_agent_orchestrator.utils.skills import (
    list_skills,
    validate_skill_folder,
    validate_skill_name,
)

#: How long an operator waits behind an in-flight plugin operation before being
#: told to retry. Named rather than inlined so the timeout the guard uses is the
#: one thing a test can shorten without replacing the real ``flock``.
_LIFECYCLE_LOCK_TIMEOUT_SECONDS = 60.0


@contextlib.contextmanager
def _lifecycle_guard():
    """Hold the agent-plugin lifecycle lock, or fail with a retryable message.

    Routed through ``installer._lifecycle`` rather than ``store.lifecycle_lock``,
    which is the whole of R6: the store raises its OWN ``PluginBusyError``, and the
    installer's class of the same name is that error's *sibling* (one subclasses
    ``PluginInstallError``, the other ``RuntimeError``) rather than its parent.
    Catching the installer's class around a direct store call therefore never
    matched, and the operator got the store's bare "operation in progress" through
    the command's catch-all instead of being told the skill was left untouched.
    ``_lifecycle`` exists for exactly this translation.

    ``InstalledPluginStore()`` with no arguments deliberately: this is the CLI, so
    the real store IS the target and the lock must be the same file the installer
    takes.
    """
    store = InstalledPluginStore()
    acquired = False
    try:
        with installer._lifecycle(store, _LIFECYCLE_LOCK_TIMEOUT_SECONDS):
            acquired = True
            yield
    except installer.PluginBusyError as exc:
        # Only a failure to ACQUIRE means nothing was changed. Once the guard is
        # held the body has begun replacing bytes, so re-labelling a busy error
        # raised in there as "Nothing was changed" would be a false claim -- and now
        # that the copy runs inside the guard, a half-written destination is exactly
        # what it would be lying about.
        if acquired:
            raise
        raise RuntimeError(
            f"Refusing to replace the skill: {exc}. Nothing was changed; retry when "
            f"the agent-plugin operation finishes."
        ) from exc


def _install_skill_folder(source_dir: Path, force: bool = False) -> Path:
    """Validate and copy a skill folder into the local skill store."""
    metadata = validate_skill_folder(source_dir)
    skill_name = validate_skill_name(metadata.name)

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    destination_dir = SKILLS_DIR / skill_name

    # One `copytree` call, entered under the guard exactly when there is something
    # to replace. Previously the copy sat outside the `with` block, so the bytes
    # landed with no lock held (the round-5 residual, R6.3) -- a concurrent
    # `cao plugin remove` of the claiming plugin could sweep the name mid-copy.
    with contextlib.ExitStack() as stack:
        if destination_dir.exists() or destination_dir.is_symlink():
            if not force:
                raise FileExistsError(
                    f"Skill '{skill_name}' already exists. Use --force to overwrite it."
                )

            # Serialized against install/uninstall (review 4 item 1 on #584). This
            # span reads which plugin claims the name, rewrites that record, then
            # replaces the projection on disk -- the same read-then-write shape
            # whose staleness the reviewer's interleaving exploits. A concurrent
            # `cao plugin remove` of the claiming plugin between the release and
            # the copytree would otherwise sweep the name while the user's own
            # bytes were landing on it.
            stack.enter_context(_lifecycle_guard())

            # A user install that replaces a plugin's projected skill takes
            # ownership of the name. Without this the install record kept claiming
            # it, so a later `cao plugin remove` deleted the user's directory and
            # any projection rebuild overwrote it with the plugin's copy.
            #
            # Ordering is load-bearing: the transfer must be *committed* before
            # anything on disk is touched. A release that failed used to be
            # indistinguishable from "no plugin held this name", so the install
            # went ahead, unlinked the projection and copied the user's folder into
            # place while the record still claimed the name — and the next
            # rebuild's sweep deleted that folder. `ProjectionClaimError` is the
            # third state; on it the install aborts with nothing changed.
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

        # A user may well point `cao skills add` at a copied plugin projection. Its
        # `.cao-projection.json` would still verify against the copied bytes, so
        # carrying it over would make the user's own skill look like a CAO
        # projection and therefore sweepable. Excluded so what they add is
        # unambiguously theirs.
        shutil.copytree(source_dir, destination_dir, ignore=shutil.ignore_patterns(MARKER_FILENAME))
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

        # `exists()` follows symlinks, so a projection whose target is gone read as
        # absent and the operator was told the skill did not exist while the link
        # sat right there. `is_symlink()` is the lstat question, and it has to come
        # first for the same reason it does below.
        if not skill_dir.is_symlink() and not skill_dir.exists():
            raise FileNotFoundError(f"Skill '{skill_name}' does not exist.")

        # Refuse rather than unlink (design.md §3.2). The skill belongs to a plugin,
        # so deleting it here is the category confusion the projection-ownership
        # work exists to prevent: in symlink mode it silently drops content the
        # plugin still claims, and in copy mode the next `rebuild_projection` puts
        # it straight back, which is a removal the operator was told succeeded and
        # which then quietly undid itself.
        owner = projection_owner(skill_name, skills_dir=SKILLS_DIR)
        if owner is not None:
            raise RuntimeError(
                f"Skill '{skill_name}' is provided by agent plugin '{owner}', so it is not "
                f"yours to remove. Nothing was changed. Run `cao plugin remove {owner}` to "
                f"uninstall the plugin, or `cao skills add <folder> --force` to replace the "
                f"skill with your own copy and take ownership of the name."
            )

        # `is_symlink()` before `is_dir()`, because `is_dir()` follows the link and
        # reports True for a projection -- which then reached `shutil.rmtree`, whose
        # refusal to act on a symbolic link surfaced as a bare
        # `[Errno None] None: <path>` with the explanation stripped off.
        if skill_dir.is_symlink():
            skill_dir.unlink()
        elif not skill_dir.is_dir():
            raise ValueError(f"Skill path is not a directory: {skill_dir}")
        else:
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
