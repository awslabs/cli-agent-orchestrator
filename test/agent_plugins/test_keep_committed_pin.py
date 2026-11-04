"""``build_agent_plugin.py --keep-committed-pin`` (#1152).

A contributor who edits a packaged skill must be able to rebuild every package
without moving the version pin the release workflow owns. These tests cover the
issue's acceptance criteria 1-4 against a staged copy of the committed packages,
so nothing under ``agent-plugin/`` is written.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as builder  # noqa: E402


def _relative_bytes(root: Path) -> dict:
    return {rel: (root / rel).read_bytes() for rel in builder._relative_files(root)}


# pyproject's version as the fixture reports it, and a version for one package to
# disagree with. Neither may equal the committed pin.
AHEAD = "9.9.9"
OTHER = "0.0.1"


@pytest.fixture
def staged(tmp_path, monkeypatch):
    """A copy of the committed packages and of skills/, wired into the builder.

    Returns ``(packages, skills, pin)``. ``pin`` is read from the staged copy
    rather than written into the tests, so the release job's repin of ``main``
    cannot turn these tests red.

    ``package_version()`` is made to run AHEAD of the committed pin, which is the
    situation the flag exists for, and every PyPI entry point is made to fail, so
    any network use by the flag shows up as a failure.
    """
    packages = tmp_path / "agent-plugin"
    shutil.copytree(builder.PACKAGES_DIR, packages, ignore=shutil.ignore_patterns("__pycache__"))
    skills = tmp_path / "skills"
    shutil.copytree(
        builder.CANONICAL_SKILLS_DIR, skills, ignore=shutil.ignore_patterns("__pycache__")
    )
    monkeypatch.setattr(builder, "PACKAGES_DIR", packages)
    monkeypatch.setattr(builder, "CANONICAL_SKILLS_DIR", skills)
    monkeypatch.setattr(builder, "package_version", lambda: AHEAD)

    def no_pypi(*args, **kwargs):
        raise AssertionError("--keep-committed-pin must not contact PyPI")

    monkeypatch.setattr(builder, "verify_published", no_pypi)
    monkeypatch.setattr(builder, "verify_bounded_client", no_pypi)
    monkeypatch.setattr(builder.urllib.request, "urlopen", no_pypi)

    pin, problems = builder.committed_pin_report()
    assert pin and not problems, problems
    assert pin not in (AHEAD, OTHER)
    return packages, skills, pin


def _set_version(packages: Path, name: str, value) -> None:
    manifest = packages / name / "plugin.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["version"] = value
    manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class TestRebuildsAtTheCommittedVersion:
    """Criterion 1."""

    def test_a_clean_rebuild_changes_nothing_and_check_passes(self, staged, capsys):
        packages, _, pin = staged
        before = _relative_bytes(packages)

        assert builder.main(["--keep-committed-pin"]) == 0
        assert _relative_bytes(packages) == before
        assert builder.main(["--check"]) == 0
        assert f"at the version they were built at, {pin}" in capsys.readouterr().out

    def test_a_skill_edit_changes_only_that_skills_copies(self, staged):
        packages, skills, pin = staged
        before = _relative_bytes(packages)
        skill_md = skills / "cao-contributing" / "SKILL.md"
        skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")

        assert builder.main(["--keep-committed-pin"]) == 0

        after = _relative_bytes(packages)
        changed = {rel for rel in after if after[rel] != before.get(rel)}
        assert changed == {Path("cao-contributor/skills/cao-contributing/SKILL.md")}
        assert set(after) == set(before)
        for config in builder.PACKAGES:
            assert (
                json.loads((packages / config.name / "plugin.json").read_text())["version"] == pin
            )
        assert builder.main(["--check"]) == 0


class TestRefusesAnUnknowablePin:
    """Criterion 2: non-zero, naming what is wrong, and nothing written."""

    def test_disagreeing_packages_are_each_named_with_their_version(self, staged, capsys):
        packages, _, pin = staged
        _set_version(packages, "cao-contributor", OTHER)
        before = _relative_bytes(packages)

        assert builder.main(["--keep-committed-pin"]) == 1

        error = capsys.readouterr().err
        assert "disagree" in error
        for config in builder.PACKAGES:
            expected = OTHER if config.name == "cao-contributor" else pin
            assert f"{config.name}={expected}" in error
        assert _relative_bytes(packages) == before

    def test_a_missing_manifest_names_its_package(self, staged, capsys):
        packages, _, _ = staged
        (packages / "cao-agent-plugin-lifecycle" / "plugin.json").unlink()

        assert builder.main(["--keep-committed-pin"]) == 1

        error = capsys.readouterr().err
        assert "cao-agent-plugin-lifecycle" in error
        assert "plugin.json" in error

    def test_a_disagreement_is_reported_beside_a_missing_manifest(self, staged, capsys):
        packages, _, pin = staged
        _set_version(packages, "cao-contributor", OTHER)
        (packages / "cao-agent-plugin-lifecycle" / "plugin.json").unlink()

        assert builder.main(["--keep-committed-pin"]) == 1

        error = capsys.readouterr().err
        assert "cao-agent-plugin-lifecycle/plugin.json is missing" in error
        assert f"cao={pin}" in error and f"cao-contributor={OTHER}" in error

    def test_a_non_string_version_names_its_package(self, staged, capsys):
        packages, _, _ = staged
        _set_version(packages, "cao", 253)

        assert builder.main(["--keep-committed-pin"]) == 1

        error = capsys.readouterr().err
        assert "cao:" in error
        assert "253" in error
        assert "not a non-empty string" in error

    def test_a_manifest_that_is_not_an_object_names_its_package(self, staged, capsys):
        packages, _, _ = staged
        (packages / "cao" / "plugin.json").write_text("[]\n", encoding="utf-8")

        assert builder.main(["--keep-committed-pin"]) == 1

        assert "cao:" in capsys.readouterr().err


class TestNeverContactsPypi:
    """Criterion 3."""

    def test_the_rebuild_succeeds_with_every_pypi_entry_point_failing(self, staged):
        # The fixture makes verify_published, verify_bounded_client and urlopen raise.
        assert builder.main(["--keep-committed-pin"]) == 0

    def test_combining_with_require_bounded_client_is_an_error(self, staged, capsys):
        with pytest.raises(SystemExit) as raised:
            builder.main(["--keep-committed-pin", "--require-bounded-client"])
        assert raised.value.code == 2
        assert "--keep-committed-pin cannot be combined with --require-bounded-client" in (
            capsys.readouterr().err
        )


class TestCheckNamesTheDisagreement:
    """Criterion 4: --check says why the committed pin is unknowable."""

    def test_check_reports_disagreeing_versions_directly(self, staged, capsys):
        packages, _, pin = staged
        _set_version(packages, "cao-contributor", OTHER)

        assert builder.main(["--check"]) == 1

        error = capsys.readouterr().err
        assert "committed packages disagree" in error
        assert f"cao-contributor={OTHER}" in error
        assert f"cao={pin}" in error

    def test_check_reports_a_missing_manifest_directly(self, staged, capsys):
        packages, _, _ = staged
        shutil.rmtree(packages / "cao-agent-plugin-lifecycle")

        assert builder.main(["--check"]) == 1

        error = capsys.readouterr().err
        assert "committed pin unknown" in error
        assert "cao-agent-plugin-lifecycle" in error


class TestTheReportBehindCommittedPackageVersion:
    """The public ``committed_package_version()`` keeps its contract."""

    def test_agreeing_packages_give_the_version_and_no_problems(self, staged):
        packages, _, pin = staged
        # The fixture read the pin through this same report, so check it
        # independently against every manifest first.
        for config in builder.PACKAGES:
            manifest = packages / config.name / "plugin.json"
            assert json.loads(manifest.read_text(encoding="utf-8"))["version"] == pin
        assert builder.committed_pin_report() == (pin, [])
        assert builder.committed_package_version() == pin

    def test_a_problem_makes_the_version_none(self, staged):
        packages, _, _ = staged
        _set_version(packages, "cao", "")
        version, problems = builder.committed_pin_report()
        assert version is None and problems
        assert builder.committed_package_version() is None


def _hint(error: str) -> str:
    lines = [line for line in error.splitlines() if line.startswith("Run: ")]
    assert len(lines) == 1, error
    return lines[0]


PLAIN_HINT = "Run: python scripts/build_agent_plugin.py  (then commit the result)"
KEEP_PIN_HINT = (
    "Run: python scripts/build_agent_plugin.py --keep-committed-pin  (then commit the result)"
)


class TestCheckHintFitsTheFailure:
    """--check advises the keep-pin build only where it can fix the failure."""

    def test_plain_drift_at_a_known_pin_gets_the_keep_pin_hint(self, staged, capsys):
        _, skills, _ = staged
        skill_md = skills / "cao-contributing" / "SKILL.md"
        skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\nEdited.\n", encoding="utf-8")

        assert builder.main(["--check"]) == 1
        assert _hint(capsys.readouterr().err) == KEEP_PIN_HINT

    def test_require_current_pin_gets_the_plain_build_hint(self, staged, capsys):
        # Committed pin behind pyproject's AHEAD: the keep-pin build would keep it
        # behind, so following that hint could never make this check pass.
        assert builder.main(["--check", "--require-current-pin"]) == 1
        assert _hint(capsys.readouterr().err) == PLAIN_HINT

    def test_a_pin_ahead_of_pyproject_gets_the_plain_build_hint(self, staged, monkeypatch, capsys):
        monkeypatch.setattr(builder, "package_version", lambda: OTHER)

        assert builder.main(["--check"]) == 1
        error = capsys.readouterr().err
        assert "make agent-plugin" in error
        assert _hint(error) == PLAIN_HINT
