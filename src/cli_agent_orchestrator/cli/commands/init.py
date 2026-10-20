"""Init command for CLI Agent Orchestrator CLI."""

import errno
import filecmp
import logging
import shutil
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, List

import click

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.constants import SKILLS_DIR
from cli_agent_orchestrator.services.memory_reconciliation import reconcile_memory_startup

logger = logging.getLogger(__name__)


# Renaming a shipped skill is not migratable on its own. ``seed_default_skills``
# skips a destination that already exists and never removes one, so after a
# rename an upgraded installation would keep the stale folder alongside the new
# one — two skills with near-identical descriptions in every agent's catalog,
# competing for activation.
#
# This map is the one-shot retirement step that makes a rename possible, and it
# is **deliberately empty**. The rename it exists for — ``cao-plugin`` to
# ``cao-event-plugin``, so the event-plugin authoring skill stops competing with
# agent-plugin requests — is unresolved maintainer decision **M4**. Activating it
# is two data edits and no code change:
#
#   1. add ``"cao-plugin": "cao-event-plugin"`` here;
#   2. rename the skill directory and its ``name:`` frontmatter, and update
#      ``SHIPPED_SKILLS`` in ``scripts/sync_skills.py`` plus the contributor
#      package's allowlist in ``scripts/build_agent_plugin.py``.
#
# The mechanism is built and tested now so that M4, when it lands, is a rename
# rather than a migration project.
SKILL_RENAMES: Dict[str, str] = {}


def _retire_renamed_skills() -> List[str]:
    """Remove old skill folders superseded by a rename. Never raises.

    Retires an old folder only when the new one exists **and the two are
    byte-identical**, i.e. the operator never customized the old copy. A
    modified folder is left in place with a warning: silently deleting an
    operator's edits to make a catalog tidier is a worse outcome than two
    entries in it, and the warning tells them exactly what to do.

    Returns the names actually retired, for reporting.
    """
    retired: List[str] = []

    for old_name, new_name in SKILL_RENAMES.items():
        old_dir = SKILLS_DIR / old_name
        new_dir = SKILLS_DIR / new_name
        if not old_dir.is_dir() or not new_dir.is_dir():
            continue

        try:
            if _trees_differ(old_dir, new_dir):
                logger.warning(
                    "Skill '%s' was renamed to '%s', but the old folder has local "
                    "modifications and was left in place. Both will appear in agent "
                    "catalogs until you remove %s.",
                    old_name,
                    new_name,
                    old_dir,
                )
                continue
            shutil.rmtree(old_dir)
            retired.append(old_name)
        except OSError as exc:
            logger.warning("Could not retire renamed skill '%s': %s", old_name, exc)

    return retired


def _trees_differ(left: Path, right: Path) -> bool:
    """Whether two skill folders differ in any file, recursively.

    The ``name:`` frontmatter necessarily differs after a rename (a skill's
    folder name must equal its frontmatter name), so ``SKILL.md`` is compared
    with that one line normalized away — otherwise every rename would look like
    a local modification and nothing would ever be retired.
    """
    left_files = {p.relative_to(left) for p in left.rglob("*") if p.is_file()}
    right_files = {p.relative_to(right) for p in right.rglob("*") if p.is_file()}
    if left_files != right_files:
        return True

    for rel in left_files:
        if rel == Path("SKILL.md"):
            if _normalized_skill_md(left / rel) != _normalized_skill_md(right / rel):
                return True
        elif not filecmp.cmp(left / rel, right / rel, shallow=False):
            return True
    return False


def _normalized_skill_md(path: Path) -> str:
    """``SKILL.md`` text with the frontmatter ``name:`` line removed."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"<unreadable:{path.name}>"
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("name:"))


def seed_default_skills() -> int:
    """Seed packaged builtin skills into the local skill store."""
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    bundled_skills = resources.files("cli_agent_orchestrator.skills")
    seeded_count = 0

    for skill_dir in bundled_skills.iterdir():
        if not skill_dir.is_dir() or not (skill_dir / "SKILL.md").is_file():
            continue

        destination_dir = SKILLS_DIR / skill_dir.name
        if destination_dir.exists():
            continue

        with resources.as_file(skill_dir) as source_dir:
            with TemporaryDirectory(
                prefix=f".{skill_dir.name}.",
                dir=SKILLS_DIR,
            ) as staging_root:
                staged_dir = Path(staging_root) / skill_dir.name
                shutil.copytree(Path(source_dir), staged_dir)
                try:
                    staged_dir.rename(destination_dir)
                except OSError as exc:
                    if exc.errno in (errno.EEXIST, errno.ENOTEMPTY) and destination_dir.exists():
                        continue
                    raise
        seeded_count += 1

    # After seeding, not before: a rename is retired only once its replacement
    # is actually present.
    _retire_renamed_skills()

    return seeded_count


@click.command()
def init():
    """Initialize CLI Agent Orchestrator database."""
    try:
        init_db()
        repair_report = reconcile_memory_startup()
        seeded_count = seed_default_skills()
        if repair_report is not None:
            click.echo(repair_report.summary_text())
        click.echo(
            f"CLI Agent Orchestrator initialized successfully. "
            f"Seeded {seeded_count} builtin skills."
        )
    except Exception as e:
        raise click.ClickException(str(e))
