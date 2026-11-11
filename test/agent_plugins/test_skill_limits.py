"""``validate_plugin`` reports the Agent Skills limits CAO's loader does not enforce (#1154).

A skill that breaks the name pattern, the 64-character name bound, the
1024-character description bound or the 500-line body bound still loads in CAO,
and may be rejected by another client. Each limit is reported as a WARNING with
the same code the audit skill's validator uses, and the skill is still
discovered. The boundary values are tested on both sides.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource, Severity
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin
from cli_agent_orchestrator.cli.commands.agent_plugin import agent_plugin
from cli_agent_orchestrator.utils.skills import (
    AGENT_SKILLS_BODY_MAX_LINES,
    AGENT_SKILLS_DESCRIPTION_MAX,
    AGENT_SKILLS_LIMIT_CODES,
    AGENT_SKILLS_NAME_MAX,
    AGENT_SKILLS_NAME_PATTERN,
)

from .conftest import build_plugin, write_skill

REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_SCRIPT = (
    REPO_ROOT / "skills" / "cao-agent-plugin-audit" / "scripts" / "validate_agent_plugin.py"
)
LIMIT_CODES = AGENT_SKILLS_LIMIT_CODES


def _plugin_with(tmp_path: Path, name: str, description: str = "d", body: str = "Body."):
    root = build_plugin(tmp_path / "p", "demo")
    (root / "skills").mkdir()
    write_skill(root / "skills" / name, name, description=description, body=body)
    return root


def _limit_findings(report):
    return [f for f in report.findings if f.code in LIMIT_CODES]


class TestEachLimit:
    @pytest.mark.parametrize(
        "name, description, body, code",
        [
            ("Bad_Name", "d", "Body.", "skill.name_pattern"),
            ("a" * (AGENT_SKILLS_NAME_MAX + 1), "d", "Body.", "skill.name_length"),
            ("ok", "d" * (AGENT_SKILLS_DESCRIPTION_MAX + 1), "Body.", "skill.description_length"),
            (
                "ok",
                "d",
                "\n".join(["x"] * (AGENT_SKILLS_BODY_MAX_LINES + 1)),
                "skill.body_length",
            ),
        ],
        ids=["name-pattern", "name-length", "description-length", "body-length"],
    )
    def test_a_broken_limit_is_a_warning_and_the_skill_still_loads(
        self, tmp_path, name, description, body, code
    ):
        report = validate_plugin(_plugin_with(tmp_path, name, description, body))

        assert report.loadable
        assert report.skill_names == (name,)
        found = _limit_findings(report)
        assert [(f.code, f.severity) for f in found] == [(code, Severity.WARNING)]
        assert found[0].path == f"skills/{name}/SKILL.md"

    def test_values_exactly_at_each_limit_produce_no_finding(self, tmp_path):
        name = "a" * AGENT_SKILLS_NAME_MAX
        report = validate_plugin(
            _plugin_with(
                tmp_path,
                name,
                description="d" * AGENT_SKILLS_DESCRIPTION_MAX,
                body="\n".join(["x"] * AGENT_SKILLS_BODY_MAX_LINES),
            )
        )
        assert report.loadable
        assert report.findings == ()

    def test_every_limit_is_reported_for_one_skill(self, tmp_path):
        name = "A" * (AGENT_SKILLS_NAME_MAX + 1)
        report = validate_plugin(
            _plugin_with(
                tmp_path,
                name,
                description="d" * (AGENT_SKILLS_DESCRIPTION_MAX + 1),
                body="\n".join(["x"] * (AGENT_SKILLS_BODY_MAX_LINES + 1)),
            )
        )
        assert {f.code for f in _limit_findings(report)} == LIMIT_CODES
        assert report.skill_names == (name,)

    @pytest.mark.parametrize("name", ["a1", "a-b-c", "x9-y8"])
    def test_names_in_the_pattern_are_accepted(self, tmp_path, name):
        assert _limit_findings(validate_plugin(_plugin_with(tmp_path, name))) == []

    @pytest.mark.parametrize("name", ["-a", "a-", "a--b", "a.b", "aB"])
    def test_names_outside_the_pattern_are_reported(self, tmp_path, name):
        found = _limit_findings(validate_plugin(_plugin_with(tmp_path, name)))
        assert [f.code for f in found] == ["skill.name_pattern"]

    def test_an_invalid_skill_reports_its_own_reason_not_a_limit(self, tmp_path):
        """A skill CAO skips is not also measured against the limits."""
        root = build_plugin(tmp_path / "p", "demo")
        (root / "skills" / "Bad_Name").mkdir(parents=True)
        (root / "skills" / "Bad_Name" / "SKILL.md").write_text(
            "---\nname: other\ndescription: d\n---\n\nx\n", encoding="utf-8"
        )
        report = validate_plugin(root)
        assert [(f.code, f.severity) for f in report.findings] == [
            ("skill.invalid", Severity.SKIPPED)
        ]
        assert report.skill_names == ()


class TestInstall:
    def test_an_over_limit_skill_is_installed_and_projected(self, tmp_path, store, skills_dir):
        """The WARNING is not a gate: `cao plugin add` still delivers the skill."""
        root = _plugin_with(
            tmp_path, "long-skill", body="\n".join(["x"] * (AGENT_SKILLS_BODY_MAX_LINES + 1))
        )
        outcome = install(
            PluginSource(kind="path", location=str(root)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        assert outcome.installed
        assert outcome.record.projected_skill_names == ("long-skill",)
        assert (skills_dir / "long-skill" / "SKILL.md").is_file()
        assert "skill.body_length" in [f.code for f in outcome.record.findings]


class TestCli:
    def test_validate_prints_the_limit_warning_and_exits_zero(self, tmp_path):
        root = _plugin_with(tmp_path, "Bad_Name")
        result = CliRunner().invoke(agent_plugin, ["validate", str(root)])
        assert result.exit_code == 0, result.output
        assert "skill.name_pattern" in result.output
        assert "WARNING" in result.output


class TestParityWithTheAuditSkill:
    """The audit skill ships its own copy of the limits; the two must not drift."""

    def test_constants_match_the_audit_validator(self):
        spec = importlib.util.spec_from_file_location("_audit_validator", AUDIT_SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.SKILL_NAME_RE.pattern == AGENT_SKILLS_NAME_PATTERN.pattern
        assert module.NAME_MAX == AGENT_SKILLS_NAME_MAX
        assert module.DESCRIPTION_MAX == AGENT_SKILLS_DESCRIPTION_MAX
        assert module.BODY_MAX == AGENT_SKILLS_BODY_MAX_LINES
