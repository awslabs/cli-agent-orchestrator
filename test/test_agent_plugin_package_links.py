"""Markdown links are checked inside each package as ``build_agent_plugin.py`` builds it (#1384).

``scripts/validate_markdown_links.py`` already rejects a link that leaves its skill's
directory in ``skills/``. It does not see the generated package files (``README.md``,
``CHANGELOG.md``), and it checks the tree as a whole rather than one package at a time.
A package is installed on its own, so the question is whether each link resolves inside
the package. #1343 and #1365 each found such links by hand.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as bap  # noqa: E402

CONFIG = bap.PackageConfig(
    name="cao-fixture",
    description="fixture",
    keywords=["fixture"],
    skills=["linky"],
    excluded_note="",
)


def _skill(tmp_path: Path, body: str) -> Path:
    skills = tmp_path / "skills"
    (skills / "linky" / "references").mkdir(parents=True)
    (skills / "linky" / "SKILL.md").write_text(
        f"---\nname: linky\ndescription: x\n---\n# Linky\n\n{body}\n", encoding="utf-8"
    )
    (skills / "linky" / "references" / "a.md").write_text("# A\n\n## Part\n", encoding="utf-8")
    (skills / "other").mkdir()
    (skills / "other" / "SKILL.md").write_text("# Other\n", encoding="utf-8")
    return skills


def _problems(tmp_path: Path, monkeypatch, body: str):
    monkeypatch.setattr(bap, "CANONICAL_SKILLS_DIR", _skill(tmp_path, body))
    return bap.package_link_problems(CONFIG, "1.2.3", tmp_path / "scratch")


class TestEveryBuiltPackageIsLinkClean:
    @pytest.mark.parametrize("config", bap.PACKAGES, ids=lambda c: c.name)
    def test_the_real_package(self, config, tmp_path):
        assert bap.package_link_problems(config, "0.0.0", tmp_path) == []

    def test_the_command_covers_every_package_and_exits_zero(self, capsys):
        assert bap.main(["--check-links"]) == 0
        out = capsys.readouterr().out
        for config in bap.PACKAGES:
            assert f"ok   {config.name}" in out, out


class TestABrokenLinkIsReported:
    def test_a_link_inside_the_skill_resolves(self, tmp_path, monkeypatch):
        """The positive: a link and a heading fragment that do resolve are not findings."""
        assert _problems(tmp_path, monkeypatch, "[a](references/a.md#part)") == []

    def test_a_link_out_of_the_package_is_reported(self, tmp_path, monkeypatch):
        problems = _problems(tmp_path, monkeypatch, "[docs](../../../../docs/agent-plugins.md)")
        assert len(problems) == 1, problems
        assert "skills/linky/SKILL.md" in problems[0]
        assert "escapes" in problems[0]

    def test_a_link_to_a_sibling_skill_is_reported(self, tmp_path, monkeypatch):
        """Resolves inside this package, but a skill is installed on its own."""
        problems = _problems(tmp_path, monkeypatch, "[o](../other/SKILL.md)")
        assert len(problems) == 1, problems
        assert "outside the skill directory" in problems[0]

    def test_a_link_to_the_package_root_is_reported(self, tmp_path, monkeypatch):
        problems = _problems(tmp_path, monkeypatch, "[r](../../README.md)")
        assert len(problems) == 1, problems
        assert "outside the skill directory" in problems[0]

    def test_a_missing_target_is_reported(self, tmp_path, monkeypatch):
        problems = _problems(tmp_path, monkeypatch, "[m](references/missing.md)")
        assert len(problems) == 1 and "does not exist" in problems[0], problems

    def test_the_command_fails_and_names_the_package(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(bap, "CANONICAL_SKILLS_DIR", _skill(tmp_path, "[m](nope.md)"))
        monkeypatch.setattr(bap, "PACKAGES", [CONFIG])
        monkeypatch.setattr(bap, "committed_package_version", lambda: "1.2.3")
        assert bap.main(["--check-links"]) == 1
        captured = capsys.readouterr()
        assert "FAIL cao-fixture" in captured.out
        assert "skills/linky/SKILL.md" in captured.err


class TestCheckLinksIsItsOwnMode:
    @pytest.mark.parametrize(
        "flag",
        [
            "--check",
            "--keep-committed-pin",
            "--require-bounded-client",
            "--require-current-pin",
            "--skip-publish-check",
        ],
    )
    def test_every_other_flag_is_rejected(self, flag, capsys):
        with pytest.raises(SystemExit) as exc:
            bap.main(["--check-links", flag])
        assert exc.value.code == 2
        assert "--check-links is its own mode" in capsys.readouterr().err


class TestTheContributingSkillNamesTheStep:
    def test_the_checklist_and_the_gate_map_mention_the_package_link_check(self):
        text = (REPO_ROOT / "skills" / "cao-contributing" / "SKILL.md").read_text(encoding="utf-8")
        assert text.count("make check-agent-plugin-links") >= 2, "checklist item 7 and the gate map"
        assert "Check links inside each built agent plugin package" in text


class TestOnePackageAtATime:
    def test_a_package_is_not_satisfied_by_another_packages_files(self, tmp_path, monkeypatch):
        """Each package is built into its own directory, so a sibling package cannot resolve a link."""
        skills = _skill(tmp_path, "[x](../../../cao-other/README.md)")
        monkeypatch.setattr(bap, "CANONICAL_SKILLS_DIR", skills)
        scratch = tmp_path / "scratch"
        (scratch / "cao-other").mkdir(parents=True)
        (scratch / "cao-other" / "README.md").write_text("# other\n", encoding="utf-8")
        problems = bap.package_link_problems(CONFIG, "1.2.3", scratch)
        assert len(problems) == 1 and "escapes" in problems[0], problems
