"""The conformance corpus — one fixture directory per failure-isolation row.

**Validates: Requirements 23.3, 23.4**

design.md calls this "the artifact that makes conformance reviewable rather than
asserted". The expectations live in ``fixtures/corpus/cases.json`` as data — exact
finding codes *and* the specification clause each one cites — so a behaviour
change surfaces as a readable diff in that file rather than as edits scattered
through assertion code.

The upstream ``agent-plugins-example`` package is included as the known-good
positive fixture (Requirement 23.4), and installing it end to end is the AC4
check: its skill is delivered, an intentionally invalid sibling is skipped with a
report, and a fatal manifest violation rejects a plugin before any component
loads.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

from .conftest import CANONICAL_EXAMPLE_DIR

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_DIR = Path(__file__).parent / "fixtures" / "corpus"
CASES = json.loads((CORPUS_DIR / "cases.json").read_text(encoding="utf-8"))
CASE_NAMES = sorted(name for name in CASES if not name.startswith("_"))


def test_every_corpus_directory_is_tracked_by_git():
    """A fixture git cannot store is a test that passes locally and lies in CI.

    Git stores no empty directories. The ``skill-missing-skill-md`` case needs a
    skill folder with **no** ``SKILL.md`` in it, which is empty by construction —
    so on a fresh clone the directory simply did not exist, the validator found
    nothing to skip, and the case passed vacuously. It failed in CI and passed
    on the machine that wrote it, which is the worst shape a fixture bug can
    take.

    Asserting every corpus directory has at least one tracked file underneath it
    catches the whole class, not just the one instance.
    """
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "--", str(CORPUS_DIR.relative_to(REPO_ROOT))],
        check=True,
        capture_output=True,
        encoding="utf-8",
    ).stdout.split()
    tracked_paths = {Path(entry) for entry in tracked}
    tracked_dirs = {parent for path in tracked_paths for parent in path.parents}

    for entry in CORPUS_DIR.rglob("*"):
        relative = entry.relative_to(REPO_ROOT)

        # Order matters: a symlink to a directory answers True to `is_dir()`,
        # but git stores it as a single blob, not as a directory with contents.
        # Checking `is_dir()` first would demand tracked files *underneath* a
        # link that legitimately has none of its own.
        if entry.is_symlink():
            assert relative in tracked_paths, f"{relative} is an untracked symlink"
        elif entry.is_dir():
            assert relative in tracked_dirs, (
                f"{relative} exists on disk but git tracks nothing under it, so it "
                f"will not survive a clone. Add a tracked file inside it."
            )
        else:
            assert relative in tracked_paths, f"{relative} is an untracked file"


def test_every_corpus_directory_has_an_expectation():
    """A fixture nobody asserts against is dead weight; catch it here."""
    on_disk = {
        entry.name
        for entry in CORPUS_DIR.iterdir()
        if entry.is_dir() and not entry.name.startswith("_")
    }
    assert on_disk == set(CASE_NAMES)


@pytest.mark.parametrize("case_name", CASE_NAMES)
def test_corpus_case(case_name):
    """Each case asserts the exact findings and spec refs its row prescribes."""
    expected = CASES[case_name]
    report = validate_plugin(CORPUS_DIR / case_name)

    assert report.loadable is expected["loadable"], expected["row"]
    assert sorted(report.skill_names) == expected["skills"], expected["row"]
    assert report.mcp_present is expected["mcp_present"], expected["row"]

    actual_codes = Counter(finding.code for finding in report.findings)
    expected_codes = expected["findings"]

    if expected.get("exact_findings", True):
        assert set(actual_codes) == set(expected_codes), (
            f"{case_name}: findings {sorted(actual_codes)} != " f"{sorted(expected_codes)}"
        )

    for code, spec_ref in expected_codes.items():
        assert actual_codes[code] == 1, f"{case_name}: expected exactly one {code}"
        finding = next(f for f in report.findings if f.code == code)
        assert (
            finding.spec_ref == spec_ref
        ), f"{case_name}: {code} cites {finding.spec_ref}, expected {spec_ref}"


def test_every_finding_in_the_corpus_cites_a_clause():
    """A finding without a ``spec_ref`` is not auditable against the spec."""
    for case_name in CASE_NAMES:
        for finding in validate_plugin(CORPUS_DIR / case_name).findings:
            assert finding.spec_ref, f"{case_name}: {finding.code} cites no clause"
            assert finding.message, f"{case_name}: {finding.code} has no message"


class TestCanonicalExample:
    """Requirement 23.4 / 23.5 and #573's AC4."""

    def test_the_canonical_example_is_a_known_good_positive_fixture(self):
        report = validate_plugin(CANONICAL_EXAMPLE_DIR)
        assert report.loadable
        assert report.findings == ()
        assert report.skill_names == ("migrate-agent-plugin",)

    def test_installing_it_delivers_its_skill_to_a_provider(self, store, skills_dir, monkeypatch):
        outcome = install(
            PluginSource(kind="path", location=str(CANONICAL_EXAMPLE_DIR)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        assert outcome.installed

        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
        from cli_agent_orchestrator.utils.skills import build_skill_catalog

        assert "migrate-agent-plugin" in build_skill_catalog()

    def test_a_fatal_violation_rejects_before_any_component_loads(self, store, skills_dir):
        """AC4's last clause, checked against a corpus fixture that has skills."""
        outcome = install(
            PluginSource(kind="path", location=str(CORPUS_DIR / "manifest-unsupported-schema")),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        assert not outcome.installed
        assert outcome.report.skills == ()
        assert list(skills_dir.iterdir()) == []
