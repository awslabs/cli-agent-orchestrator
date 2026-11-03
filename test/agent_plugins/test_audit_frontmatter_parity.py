"""The audit validator and the creator's snippet find frontmatter as CAO does.

CAO reads SKILL.md with python-frontmatter (``utils/skills.py``): the text is
stripped, then split on ``^-{3,}\\s*$`` in multiline mode. The audit validator
(``cao-agent-plugin-audit/scripts/validate_agent_plugin.py``) and the
measurement snippet in ``cao-agent-plugin-creator/SKILL.md`` port that rule
rather than importing the library. Each shape below is run through all three, so
a verdict that disagrees with CAO's own ``validate_skill_folder`` fails here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.utils.skills import _load_skill_folder, validate_skill_folder

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = (
    REPO_ROOT / "skills" / "cao-agent-plugin-audit" / "scripts" / "validate_agent_plugin.py"
)
CREATOR_SKILL = REPO_ROOT / "skills" / "cao-agent-plugin-creator" / "SKILL.md"
DESCRIPTION_MAX = 1024

# An indented '---' inside a block scalar is part of the value for CAO. The
# filler takes the description past the limit only if the audit reads past it.
_FILLER = "\n".join(f"  Line {n:03d} of a long description that keeps going." for n in range(24))
SHAPES = {
    "indented-rule": (
        "---\n"
        "name: indented-rule\n"
        "description: |\n"
        "  Starts here.\n"
        "  ---\n"
        f"{_FILLER}\n"
        "---\n"
        "# Body\n"
        "\n"
        "Text.\n"
    ),
    "leading-blank": (
        "\n---\nname: leading-blank\ndescription: Loads after a blank first line.\n---\n# Body\n"
    ),
    "four-dashes": (
        "----\nname: four-dashes\ndescription: Loads with four-dash delimiters.\n----\n# Body\n"
    ),
    "control": ("---\nname: control\ndescription: A normal skill.\n---\n# Body\n\nText.\n"),
}


def _skill_dir(tmp_path: Path, name: str) -> Path:
    skill = tmp_path / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SHAPES[name], encoding="utf-8")
    return skill


def _run_audit(skills_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(AUDIT_SCRIPT), "--skills-dir", str(skills_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        stdin=subprocess.DEVNULL,
    )


def _creator_snippet() -> str:
    text = CREATOR_SKILL.read_text(encoding="utf-8")
    match = re.search(r"<<'PY'\n(.*?)\nPY\n", text, re.S)
    assert match, "the creator's measurement snippet was not found"
    return match.group(1)


def _run_creator(skill: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _creator_snippet()],
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "SKILL": str(skill)},
    )


def _cao(skill: Path):
    """CAO's verdict: the metadata it loads, plus its body line count."""
    metadata = validate_skill_folder(skill)
    _, content = _load_skill_folder(skill)
    return metadata, len(content.splitlines())


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_cao_loads_every_shape(tmp_path, name):
    """The premise: CAO's own loader accepts all four, so none may be SKIPPED."""
    metadata, _ = _cao(_skill_dir(tmp_path, name))
    assert metadata.name == name


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_audit_measures_what_cao_loads(tmp_path, name):
    skill = _skill_dir(tmp_path, name)
    metadata, cao_body_lines = _cao(skill)

    result = _run_audit(skill.parent)

    assert "skill.unparseable" not in result.stdout, result.stdout
    expected = (
        f"skill {name}: name {len(name)} chars, description {len(metadata.description)} "
        f"chars, body {cao_body_lines} lines"
    )
    assert expected in result.stdout, result.stdout
    over_limit = len(metadata.description) > DESCRIPTION_MAX
    assert ("skill.description_length" in result.stdout) is over_limit, result.stdout
    assert result.returncode == (1 if over_limit else 0), result.stdout + result.stderr


def test_the_indented_rule_is_a_limit_finding(tmp_path):
    """The false pass the review found: the description is over the limit."""
    skill = _skill_dir(tmp_path, "indented-rule")
    metadata, _ = _cao(skill)
    assert len(metadata.description) > DESCRIPTION_MAX

    result = _run_audit(skill.parent)

    assert result.returncode == 1
    assert re.search(r"^LIMIT\s+skill\.description_length \[indented-rule\]", result.stdout, re.M)


@pytest.mark.parametrize("name", sorted(SHAPES))
def test_creator_snippet_measures_what_cao_loads(tmp_path, name):
    skill = _skill_dir(tmp_path, name)
    metadata, cao_body_lines = _cao(skill)

    result = _run_creator(skill)

    assert result.stdout.strip() == (
        f"name={name} description_chars={len(metadata.description)} " f"body_lines={cao_body_lines}"
    ), (result.stdout + result.stderr)
    assert result.returncode == (1 if len(metadata.description) > DESCRIPTION_MAX else 0)
