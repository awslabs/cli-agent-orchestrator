"""Unit coverage for Phase 0 Kiro engine resolution."""

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine


@pytest.mark.parametrize(
    ("explicit", "profile", "expected"),
    [
        (None, None, KiroEngine.V2),
        ("v2", None, KiroEngine.V2),
        (None, "kas", KiroEngine.KAS),
        ("kas", "kas", KiroEngine.KAS),
    ],
)
def test_resolve_kiro_engine_uses_creation_precedence(explicit, profile, expected):
    assert resolve_kiro_engine(explicit=explicit, profile=profile) == expected


def test_resolve_kiro_engine_rejects_conflicting_creation_values():
    with pytest.raises(ValueError, match="conflict"):
        resolve_kiro_engine(explicit="v2", profile="kas")


def test_persisted_engine_is_authoritative_during_restore():
    assert resolve_kiro_engine(explicit="v2", profile="v2", persisted="kas") == KiroEngine.KAS


def test_agent_profile_rejects_unknown_engine_before_creation():
    with pytest.raises(ValidationError, match="engine"):
        AgentProfile(name="bad", description="bad", engine="v3")
