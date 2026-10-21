"""Deterministic tests for the skills-lifecycle example (issue #593).

Cover the lifecycle ``run.sh`` drives — install, discovery, full-body
retrieval, profile scoping, the reinstall guard, the fixture task, and
removal — against the same public surfaces the example scripts call, with the
skill store isolated per test (see conftest.py). No cao-server, no provider
credentials, no network.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text
from cli_agent_orchestrator.utils.skills import build_skill_catalog, load_skill_content

EXAMPLE_DIR = Path(__file__).resolve().parent.parent
BUNDLED_SKILL_DIR = EXAMPLE_DIR / "python-review-checklist"
BUNDLED_PROFILE = EXAMPLE_DIR / "profiles" / "skills_lifecycle_reviewer.md"
PROFILE_NAME = "skills_lifecycle_reviewer"
SKILL_NAME = "python-review-checklist"
DECOY_NAME = "skills-lifecycle-decoy"


@pytest.fixture(autouse=True)
def _no_agent_refresh():
    """Keep ``skills add`` from refreshing the operator's installed agents.

    The CLI module binds ``refresh_all_cao_managed_agents`` at import; on a
    developer machine with CAO-managed Copilot agents installed, the real one
    would rewrite those files as a side effect of this example's tests.
    """
    with patch(
        "cli_agent_orchestrator.cli.commands.skills.refresh_all_cao_managed_agents",
        return_value=[],
    ):
        yield


@pytest.fixture()
def installed_skill(isolated_skill_store):
    """Install the bundled skill through the real CLI command."""
    from cli_agent_orchestrator.cli.commands.skills import add

    result = CliRunner().invoke(add, [str(BUNDLED_SKILL_DIR)])
    assert result.exit_code == 0, result.output
    return isolated_skill_store / SKILL_NAME


def test_install_and_list_reports_bundled_skill(isolated_skill_store):
    from cli_agent_orchestrator.cli.commands.skills import add, list_command

    runner = CliRunner()
    result = runner.invoke(add, [str(BUNDLED_SKILL_DIR)])
    assert result.exit_code == 0, result.output
    assert (isolated_skill_store / SKILL_NAME / "SKILL.md").is_file()

    listing = runner.invoke(list_command)
    assert listing.exit_code == 0, listing.output
    assert SKILL_NAME in listing.output


def test_load_skill_content_returns_complete_body(installed_skill):
    content = load_skill_content(SKILL_NAME)

    assert content.startswith("# Python Review Checklist")
    assert "Checklist applied:" in content
    assert content.rstrip().endswith("proof that this skill's guidance engaged.")


def test_profile_scopes_catalog_to_bundled_skill(installed_skill, isolated_skill_store):
    from cli_agent_orchestrator.cli.commands.skills import add

    decoy_dir = isolated_skill_store.parent / "decoy-source" / DECOY_NAME
    decoy_dir.mkdir(parents=True)
    (decoy_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {DECOY_NAME}\n"
        "description: A decoy skill used to prove catalog filtering\n"
        "---\n\n# Decoy\n",
        encoding="utf-8",
    )
    installed = CliRunner().invoke(add, [str(decoy_dir)])
    assert installed.exit_code == 0, installed.output

    profile = parse_agent_profile_text(BUNDLED_PROFILE.read_text(encoding="utf-8"), PROFILE_NAME)

    assert profile.skills == [SKILL_NAME]

    catalog = build_skill_catalog(profile.skills)
    assert f"- **{SKILL_NAME}**:" in catalog
    assert DECOY_NAME not in catalog
    # The negative direction: an explicitly empty profile skills list
    # advertises nothing at all.
    assert build_skill_catalog([]) == ""


def test_reinstall_refused_without_force_then_force_overwrites(installed_skill):
    from cli_agent_orchestrator.cli.commands.skills import add

    runner = CliRunner()
    refused = runner.invoke(add, [str(BUNDLED_SKILL_DIR)])
    assert refused.exit_code != 0
    assert "Use --force to overwrite it" in refused.stderr

    forced = runner.invoke(add, [str(BUNDLED_SKILL_DIR), "--force"])
    assert forced.exit_code == 0, forced.output


def test_remove_leaves_store_clean(installed_skill, isolated_skill_store):
    from cli_agent_orchestrator.cli.commands.skills import list_command, remove

    runner = CliRunner()
    removed = runner.invoke(remove, [SKILL_NAME])
    assert removed.exit_code == 0, removed.output

    assert not any(isolated_skill_store.iterdir())
    listing = runner.invoke(list_command)
    assert "No skills found" in listing.output


def test_apply_checklist_reports_contract_line(installed_skill, isolated_home):
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "fixture" / "apply_checklist.py")],
        capture_output=True,
        text=True,
        env={**os.environ, "CAO_HOME_DIR": str(isolated_home)},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "'re' is used but never imported" in proc.stdout
    assert "mutable default argument" in proc.stdout
    assert "bare 'except:' clause" in proc.stdout
    assert "missing type hints" in proc.stdout
    assert "Checklist applied: 4/4 items" in proc.stdout


def test_apply_checklist_clean_fixture_has_no_findings(installed_skill, isolated_home, tmp_path):
    clean_fixture = tmp_path / "clean.md"
    clean_fixture.write_text(
        "```python\n"
        "def add(left: int, right: int) -> int:\n"
        "    return left + right\n"
        "```\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "fixture" / "apply_checklist.py"), str(clean_fixture)],
        capture_output=True,
        text=True,
        env={**os.environ, "CAO_HOME_DIR": str(isolated_home)},
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "Checklist applied: 4/4 items"


def test_apply_checklist_fails_without_skill(tmp_path):
    empty_home = tmp_path / "empty-home"
    empty_home.mkdir()
    proc = subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / "fixture" / "apply_checklist.py")],
        capture_output=True,
        text=True,
        env={**os.environ, "CAO_HOME_DIR": str(empty_home)},
        timeout=120,
    )
    assert proc.returncode == 1
    assert "not available" in proc.stderr


def test_run_sh_full_lifecycle_smoke():
    """run.sh drives the whole lifecycle inside its own temporary home."""
    venv_bin = str(Path(sys.executable).parent)
    proc = subprocess.run(
        ["bash", str(EXAMPLE_DIR / "run.sh")],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
        timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK: full skill lifecycle verified" in proc.stderr
