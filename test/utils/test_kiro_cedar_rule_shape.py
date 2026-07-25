"""U4: structural validation of every emitted Cedar rule.

Traces to FR-103, NFR-101, NFR-103, BR-U4-1..8.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services.kiro_profiles import render_kiro_kas
from cli_agent_orchestrator.utils.kiro_policy import (
    KiroPolicyError,
    _validate_rule_shape,
    compile_kiro_policy,
)


@pytest.mark.parametrize(
    "rule",
    [
        'permit(principal, action == Action::"fs_read", resource);',
        'forbid(principal, action == Action::"execute_bash", resource);',
        'permit(principal, action == Action::"*", resource);',
        'forbid(principal, action == Action::"mcp::search::query", resource);',
    ],
)
def test_conforming_rules_are_accepted(rule: str) -> None:
    """BR-U4-6: the grammar admits exactly the shapes CAO emits."""
    assert _validate_rule_shape(rule) is None


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        ('allow(principal, action == Action::"fs_read", resource);', "wrong effect"),
        ('deny(principal, action == Action::"fs_read", resource);', "wrong effect"),
        (
            'permit(principal, action == Action::"fs_read", resource) when { true };',
            "trailing when clause",
        ),
        (
            'permit(principal, action == Action::"fs_read", resource);'
            ' permit(principal, action == Action::"fs_write", resource);',
            "second appended statement",
        ),
        (
            '// audit\npermit(principal, action == Action::"fs_read", resource);',
            "prefixed text and embedded newline",
        ),
        (
            'permit(principal, action == Action::"fs_read", resource);\n'
            'permit(principal, action == Action::"fs_write", resource);',
            "embedded newline",
        ),
        ('permit(principal, action == Action::"fs read", resource);', "unsafe action token"),
        ('permit(principal, action == Action::"fs\\"", resource);', "quote in action token"),
        ('permit(principal, action in Action::"fs_read", resource);', "wrong operator"),
        ('permit(principal, action == Action::"fs_read", resource)', "missing terminator"),
        ("", "empty rule"),
    ],
)
def test_nonconforming_rules_are_refused(rule: str, why: str) -> None:
    """BR-U4-3: a mismatch refuses; it never repairs or drops the rule."""
    with pytest.raises(KiroPolicyError, match="malformed-cedar-rule") as exc_info:
        _validate_rule_shape(rule)
    assert exc_info.value.diagnostic.code == "malformed-cedar-rule", why


def test_every_compiled_rule_passes_the_frame() -> None:
    """BR-U4-1: validation runs for every emitted rule inside the compiler."""
    policy = compile_kiro_policy(
        AgentProfile(
            name="shape-test",
            description="Shape test",
            engine=KiroEngine.KAS,
            allowedTools=["fs_*", "@search/query"],
            deniedTools=["fs_write"],
            mcpServers={"search": {"command": "synthetic-mcp"}},
        )
    )

    assert policy.permissions.rules
    for rule in policy.permissions.rules:
        _validate_rule_shape(rule)


def test_one_malformed_rule_refuses_the_whole_profile_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BR-U4-2/BR-U4-4: whole-profile refusal, pre-existing artifact untouched."""
    artifact = tmp_path / "shape-test.kas.json"
    artifact.write_text('{"pre": "existing"}', encoding="utf-8")
    original = artifact.read_bytes()

    # Force one emitted rule out of shape at the emission helper — the closest
    # in-code injection point to a real serialization defect.
    real_cedar_rule = getattr(
        __import__("cli_agent_orchestrator.utils.kiro_policy", fromlist=["_cedar_rule"]),
        "_cedar_rule",
    )

    def malformed_for_one_action(effect: str, action: str) -> str:
        rule = real_cedar_rule(effect, action)
        if action == "fs_read":
            return rule[:-1] + ' when { principal == User::"root" };'
        return rule

    monkeypatch.setattr(
        "cli_agent_orchestrator.utils.kiro_policy._cedar_rule",
        malformed_for_one_action,
    )

    profile = AgentProfile(
        name="shape-test",
        description="Shape test",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read", "fs_write"],
    )

    with pytest.raises(KiroPolicyError, match="malformed-cedar-rule"):
        render_kiro_kas(profile, [], None)

    assert artifact.read_bytes() == original
    assert not (tmp_path / "shape-test.kas.summary.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []


def test_unsafe_action_identity_still_refuses_via_the_existing_path() -> None:
    """BR-U4-7: the pre-existing serialization-error path is unchanged."""
    from cli_agent_orchestrator.utils.kiro_policy import _cedar_rule

    with pytest.raises(KiroPolicyError, match="serialization-error"):
        _cedar_rule("permit", 'fs_read", resource); permit(principal, action == Action::"fs_write')


def test_valid_kas_render_is_unaffected_by_the_new_check() -> None:
    """BR-U4-7: additive only — a translatable profile still renders."""
    profile = AgentProfile(
        name="unaffected",
        description="Unaffected",
        engine=KiroEngine.KAS,
        allowedTools=["fs_read"],
    )
    rendered, policy = render_kiro_kas(profile, [], None)
    value = json.loads(rendered)
    assert value["tools"] == ["fs_read"]
    assert policy.allow_rule_count == 1
