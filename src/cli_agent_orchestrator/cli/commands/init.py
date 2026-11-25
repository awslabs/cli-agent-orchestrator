"""Init command for CLI Agent Orchestrator CLI."""

import errno
import filecmp
import hashlib
import logging
import shutil
from importlib import resources
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Dict, FrozenSet, List, Optional

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
# This map is the one-shot retirement step that makes a rename possible. It holds
# ``cao-plugin`` to ``cao-event-plugin`` (decision M4), so the event-plugin
# authoring skill no longer competes with agent-plugin requests. Adding a rename
# is a data edit here plus the skill directory, its ``name:`` frontmatter,
# ``SHIPPED_SKILLS`` in ``scripts/sync_skills.py`` and the contributor package's
# allowlist in ``scripts/build_agent_plugin.py``, plus the old skill's entry in
# ``SKILL_RENAME_SHIPPED_DIGESTS`` below.
SKILL_RENAMES: Dict[str, str] = {"cao-plugin": "cao-event-plugin"}

# Digests (``_skill_tree_digest``) of every tree a release shipped under an old
# name in ``SKILL_RENAMES``. ``seed_default_skills`` never refreshes an existing
# folder, so an install keeps the text of the release that first seeded it, and
# that text is usually not today's. A folder whose digest is listed here is a
# copy a release shipped and nobody edited, so it is retired like one that
# matches the current skill. Anything else may hold the operator's edits and is
# kept. Compute the entries with ``python scripts/skill_rename_digests.py``,
# which walks the release tags; ``test_naming_migration.py`` pins them.
SKILL_RENAME_SHIPPED_DIGESTS: Dict[str, FrozenSet[str]] = {
    "cao-plugin": frozenset(
        {
            # v2.3.0, v2.4.0, v2.4.1, v2.5.0, v2.5.1, v2.5.2, v2.5.3, v2.5.4
            # (git tree 99f6dbd)
            "28f1086f18e4f27759d4b0b79a7531c78b857e0f85d665533b381163da68ab42",
            # v2.5.5 (git tree 904a55c)
            "1e795b3964a4b6663526368f6bdfe3f2a02e9df01df4c2a3623e861608c90a81",
        }
    ),
}


def _retire_renamed_skills() -> List[str]:
    """Remove old skill folders superseded by a rename. Never raises.

    Retires an old folder only when the new one exists and the old one is a
    copy nobody edited: it is the same as the new folder, or its digest is one
    a release shipped (``SKILL_RENAME_SHIPPED_DIGESTS``). Both comparisons
    ignore only the frontmatter ``name:`` line. Any other folder may hold the
    operator's edits and is left in place with a warning: silently deleting
    those edits to make a catalog tidier is a worse outcome than two entries in
    it, and the warning tells them exactly what to do.

    Returns the names actually retired, for reporting.
    """
    retired: List[str] = []

    for old_name, new_name in SKILL_RENAMES.items():
        old_dir = SKILLS_DIR / old_name
        new_dir = SKILLS_DIR / new_name
        if not old_dir.is_dir() or not new_dir.is_dir():
            continue

        try:
            shipped = SKILL_RENAME_SHIPPED_DIGESTS.get(old_name, frozenset())
            if _trees_differ(old_dir, new_dir) and _skill_tree_digest(old_dir) not in shipped:
                logger.warning(
                    "Skill '%s' was renamed to '%s', but the old folder matches neither "
                    "the current skill nor any released copy of it, so it may hold "
                    "local edits, and it was left in place. Both will appear in agent "
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
    """``SKILL.md`` text with the frontmatter ``name:`` line removed.

    Only the YAML frontmatter (between the opening ``---`` on the first line and
    the next ``---``) is searched. A ``name:`` line in the body, such as one in
    a YAML example, is content, so an edit to it counts as an edit.

    The text is rejoined from ``splitlines()``, so line-ending style (CRLF or LF)
    and a trailing newline are not differences either.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"<unreadable:{path.name}>"
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return "\n".join(lines)
    kept = [lines[0]]
    in_frontmatter = True
    for line in lines[1:]:
        if in_frontmatter and line.strip() == "---":
            in_frontmatter = False
        elif in_frontmatter and line.startswith("name:"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _skill_tree_digest(folder: Path) -> Optional[str]:
    """SHA-256 over a skill folder's files, with ``SKILL.md`` normalized.

    Each file contributes its POSIX relative path and its bytes, in sorted path
    order; ``SKILL.md`` contributes ``_normalized_skill_md`` instead, so the
    digest does not depend on the folder's name. ``None`` when a file cannot be
    read, which matches no recorded digest. ``scripts/skill_rename_digests.py``
    computes the recorded digests with this same function.
    """
    digest = hashlib.sha256()
    try:
        files = sorted(
            (p.relative_to(folder).as_posix(), p) for p in folder.rglob("*") if p.is_file()
        )
        for rel, path in files:
            if rel == "SKILL.md":
                data = _normalized_skill_md(path).encode("utf-8")
            else:
                data = path.read_bytes()
            digest.update(rel.encode("utf-8") + b"\0")
            digest.update(str(len(data)).encode("ascii") + b"\0")
            digest.update(data)
    except OSError:
        return None
    return digest.hexdigest()


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
