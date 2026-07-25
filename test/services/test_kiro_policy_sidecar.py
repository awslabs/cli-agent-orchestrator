"""U7: the redacted policy sidecar.

Traces to FR-105, ADR-006, NFR-103/104, SEC-U7-1..9.

Sentinel fixtures deliberately contain values *resembling* secrets. They are
synthetic and committed; never replace them with real credentials.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services.kiro_profile_service import install_profile
from cli_agent_orchestrator.services.kiro_profiles import (
    kiro_artifact_path,
    kiro_summary_path,
    redacted_policy_summary,
)
from cli_agent_orchestrator.utils.kiro_policy import compile_kiro_policy

#: Pinned cross-unit contract — asserted as an exact set, never a subset, so a
#: future key carrying arbitrary profile content fails purely by existing.
_SUMMARY_KEYS = {
    "policy_source",
    "unrestricted",
    "visible_tools",
    "excluded_tools",
    "allow_rule_count",
    "deny_rule_count",
}

_SENTINEL_PROMPT = "SENTINEL-PROMPT-do-not-leak-a71f"
_SENTINEL_ENV_VALUE = "SENTINEL-MCP-CREDENTIAL-do-not-leak-c04e"


def _profile(**kwargs) -> AgentProfile:
    base = {
        "name": "sidecar",
        "description": "Sidecar",
        "engine": KiroEngine.KAS,
        "allowedTools": ["fs_*", "@service/query"],
        "deniedTools": ["fs_write"],
        "prompt": _SENTINEL_PROMPT,
        "mcpServers": {
            "service": {"command": "service-mcp", "env": {"API_TOKEN": _SENTINEL_ENV_VALUE}}
        },
    }
    base.update(kwargs)
    return AgentProfile(**base)


def test_summary_key_set_equals_the_pinned_six_exactly() -> None:
    """SEC-U7-1: equality, not subset — a whitelist literal, not a projection."""
    summary = redacted_policy_summary(compile_kiro_policy(_profile()))
    assert set(summary) == _SUMMARY_KEYS


def test_excluded_tools_sources_denied_tools_not_the_rendered_envelope() -> None:
    """The pinned key/source mapping: `excluded_tools` <- `denied_tools`."""
    policy = compile_kiro_policy(_profile())
    summary = redacted_policy_summary(policy)
    assert summary["excluded_tools"] == list(policy.denied_tools)
    assert "excludedTools" not in summary
    assert "denied_tools" not in summary


def test_counts_and_source_match_the_compiled_policy() -> None:
    policy = compile_kiro_policy(_profile())
    summary = redacted_policy_summary(policy)
    assert summary["allow_rule_count"] == policy.allow_rule_count
    assert summary["deny_rule_count"] == policy.deny_rule_count
    assert summary["policy_source"] == "allowedTools"
    assert summary["unrestricted"] is False
    assert summary["visible_tools"] == list(policy.visible_tools)


def test_unrestricted_policy_summary_reports_the_wildcard_grant() -> None:
    policy = compile_kiro_policy(
        AgentProfile(
            name="sidecar-open",
            description="Open",
            engine=KiroEngine.KAS,
            allowedTools=["*"],
        )
    )
    summary = redacted_policy_summary(policy)
    assert summary["unrestricted"] is True
    assert summary["visible_tools"] == ["*"]
    assert summary["allow_rule_count"] == 1


def test_sidecar_file_leaks_no_prompt_no_mcp_env_and_no_rule_bodies(tmp_path: Path) -> None:
    """SEC-U7-2/3/4: sentinel absence plus no `permit(`/`forbid(` substring."""
    outcome = install_profile(
        _profile(), directory=tmp_path, engine=KiroEngine.KAS, resources=[], mcp_servers=None
    )
    assert outcome.summary_path is not None
    raw = outcome.summary_path.read_text(encoding="utf-8")

    assert _SENTINEL_PROMPT not in raw
    assert _SENTINEL_ENV_VALUE not in raw
    assert "permit(" not in raw
    assert "forbid(" not in raw
    assert "API_TOKEN" not in raw
    assert set(json.loads(raw)) == _SUMMARY_KEYS


def test_summary_path_shares_the_stem_but_not_the_extension(tmp_path: Path) -> None:
    """SEC-U7-7: distinct extension, so it cannot be mistaken for engine input."""
    artifact = kiro_artifact_path(tmp_path, "sidecar", KiroEngine.KAS)
    summary = kiro_summary_path(tmp_path, "sidecar", KiroEngine.KAS)
    assert artifact == tmp_path / "sidecar.kas.json"
    assert summary == tmp_path / "sidecar.kas.summary.json"
    assert artifact != summary


def test_escape_profile_name_raises_before_any_path_is_produced(tmp_path: Path) -> None:
    """SEC-U7-6: the same traversal defense as kiro_artifact_path."""
    with pytest.raises(ValueError, match=r"\[A-Za-z0-9_-\]"):
        kiro_summary_path(tmp_path, "../escape", KiroEngine.KAS)


def test_summary_path_refuses_a_v2_engine(tmp_path: Path) -> None:
    """SEC-U7-8: v2 has no compiled Cedar policy, so it has no sidecar."""
    with pytest.raises(ValueError, match="KAS"):
        kiro_summary_path(tmp_path, "sidecar", KiroEngine.V2)


def test_replace_failure_leaves_the_artifact_intact_with_no_tmp_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SEC-U7-5/NFR-103: the sidecar cannot be left half-written."""
    import os

    artifact = tmp_path / "sidecar.kas.json"
    artifact.write_text('{"pre": "existing"}', encoding="utf-8")

    real_replace = os.replace

    def fail_on_summary(source, target):
        if str(target).endswith(".summary.json"):
            raise OSError("synthetic summary replace failure")
        return real_replace(source, target)

    monkeypatch.setattr("cli_agent_orchestrator.services.kiro_profiles.os.replace", fail_on_summary)

    with pytest.raises(OSError, match="synthetic summary replace failure"):
        install_profile(
            _profile(), directory=tmp_path, engine=KiroEngine.KAS, resources=[], mcp_servers=None
        )

    # The artifact write completed before the sidecar attempt, so it holds the
    # newly rendered profile — the durability claim is that no *partial* file and
    # no temp residue survive.
    assert json.loads(artifact.read_text(encoding="utf-8"))["name"] == "sidecar"
    assert not (tmp_path / "sidecar.kas.summary.json").exists()
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_no_module_reads_the_sidecar_back() -> None:
    """SEC-U7-9: write-only by construction, so there is no staleness surface.

    Scans for a read applied to the sidecar path — a mere mention (a docstring,
    or the write itself) is fine; a read is what would create the staleness
    surface ADR-003 exists to avoid.
    """
    import pathlib as _pathlib
    import re

    import cli_agent_orchestrator

    read_verb = r"(?:read_text|read_bytes|open|json\.load|loads)"
    patterns = (
        re.compile(rf"kiro_summary_path\([^\n]*\)\s*\.\s*{read_verb}"),
        re.compile(rf"summary_path\s*\.\s*{read_verb}"),
        re.compile(rf"{read_verb}\([^\n]*summary_path"),
        re.compile(rf"{read_verb}\([^\n]*summary\.json"),
    )

    src = _pathlib.Path(cli_agent_orchestrator.__file__).resolve().parent
    readers = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if any(pattern.search(path.read_text(encoding="utf-8")) for pattern in patterns)
    ]
    assert readers == [], f"the sidecar must never be read back as input; read in {readers}"
