"""The release repin must commit everything the build writes (#1409).

``scripts/build_agent_plugin.py`` rewrites the two marketplace catalogs along
with ``agent-plugin/`` because both carry the pinned version. The
``regenerate-agent-plugin`` job diffs and commits only what it names, so a path
missing from its ``git diff --quiet --`` or ``git add`` line leaves that catalog
stale on ``main`` and turns ``make check-agent-plugin`` red on every PR.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-to-pypi.yml"

# Everything the build writes, as the directories the job must name.
BUILD_OUTPUTS = ("agent-plugin/", ".claude-plugin/", ".agents/plugins/")


def _commit_step() -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index("- name: Commit the regenerated packages if they changed")
    end = text.index("\n      - name:", start + 1)
    return text[start:end]


def _line(step: str, prefix: str) -> str:
    lines = [ln.strip() for ln in step.splitlines() if ln.strip().startswith(prefix)]
    assert len(lines) == 1, f"expected one line starting {prefix!r}, got {lines}"
    return lines[0]


def test_the_repin_diff_names_every_build_output():
    line = _line(_commit_step(), "if git diff --quiet --")
    for path in BUILD_OUTPUTS:
        assert path in line, f"{path} missing from the repin's `git diff --quiet --`: {line}"


def test_the_repin_commit_adds_every_build_output():
    line = _line(_commit_step(), "git add ")
    for path in BUILD_OUTPUTS:
        assert path in line, f"{path} missing from the repin's `git add`: {line}"


def test_the_build_writes_nothing_outside_the_named_paths():
    import sys

    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    import build_agent_plugin as builder

    for path in builder.catalog_targets("0.0.0"):
        rel = path.relative_to(builder.PACKAGES_DIR.parent).as_posix()
        assert any(rel.startswith(out) for out in BUILD_OUTPUTS), rel


def _ignored(rel: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", rel], cwd=REPO_ROOT, check=False
    )
    return result.returncode == 0


def test_only_the_catalog_is_unignored_under_agents():
    assert not _ignored(".agents/plugins/marketplace.json")
    assert _ignored(".agents/plugins/other.json")
    assert _ignored(".agents/skills/x/SKILL.md")
    assert _ignored(".agents/config.toml")
