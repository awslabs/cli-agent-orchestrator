"""Read-only lint and migration-readiness reporting for Kiro profiles."""

from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field

from cli_agent_orchestrator.constants import KIRO_AGENTS_DIR
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine, resolve_kiro_engine
from cli_agent_orchestrator.services.kiro_profiles import kiro_artifact_path
from cli_agent_orchestrator.utils.kiro_policy import (
    KiroPolicyError,
    PolicySource,
    compile_kiro_policy,
)


class CedarPolicySummary(BaseModel):
    """Non-sensitive summary of generated KAS rules."""

    allow_rules: int = 0
    hard_deny_rules: int = 0


class KiroProfileLintResult(BaseModel):
    """Machine-readable, prompt-free Kiro profile lint result."""

    profile: str
    resolved_engine: KiroEngine
    policy_source: PolicySource
    unsupported: List[str] = Field(default_factory=list)
    v2_artifact: str
    kas_artifact: str
    kas_visible_tools: List[str] = Field(default_factory=list)
    cedar: CedarPolicySummary = Field(default_factory=CedarPolicySummary)
    generation_safe: bool
    diagnostic: Optional[str] = None


def lint_kiro_profile(
    profile: AgentProfile,
    artifact_directory: Path = KIRO_AGENTS_DIR,
) -> KiroProfileLintResult:
    """Compile without writing and return a redacted migration report."""
    engine = resolve_kiro_engine(profile=profile.engine)
    source: PolicySource = (
        "allowedTools"
        if profile.allowedTools is not None
        else "role" if profile.role else "default"
    )
    unsupported: list[str] = []
    visible: list[str] = []
    cedar = CedarPolicySummary()
    diagnostic: Optional[str] = None
    safe = False

    try:
        policy = compile_kiro_policy(profile)
        source = policy.source
        visible = list(policy.visible_tools)
        cedar = CedarPolicySummary(
            allow_rules=policy.allow_rule_count,
            hard_deny_rules=policy.deny_rule_count,
        )
        safe = True
    except KiroPolicyError as exc:
        unsupported.append(exc.diagnostic.code)
        diagnostic = exc.diagnostic.message

    return KiroProfileLintResult(
        profile=profile.name,
        resolved_engine=engine,
        policy_source=source,
        unsupported=unsupported,
        v2_artifact=str(kiro_artifact_path(artifact_directory, profile.name, KiroEngine.V2)),
        kas_artifact=str(kiro_artifact_path(artifact_directory, profile.name, KiroEngine.KAS)),
        kas_visible_tools=visible,
        cedar=cedar,
        generation_safe=safe,
        diagnostic=diagnostic,
    )
