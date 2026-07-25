"""One service-layer entry point for Kiro profile render / install / lint.

Introduced by FR-105 so ``install_service`` and the CLI depend on a single
interface instead of reaching into three modules.

**Delegation contract (ADR-004, BR-U6-1): this facade delegates and never
reimplements.** Every method body is a dispatch plus a wrap — no policy,
rendering, or lint logic is copied, forked, or rewritten. That is what keeps the
pre-existing suite a genuine regression net (it keeps exercising the *live*
path, not dead code) and what preserves NFR-105's byte-identical v2 render: the
v2 arm calls the same ``render_kiro_v2``, so byte-identity is inherited rather
than re-achieved.

There is deliberately **no** ``try``/``except`` around the delegated calls
(BR-U6-5): ``KiroPolicyError`` must propagate unchanged because downstream
attribution (FR-104) needs its diagnostic code. "Add error handling" is a review
reflex, and here it would be the regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.services.kiro_profile_lint import (
    KiroProfileLintResult,
    lint_kiro_profile,
)
from cli_agent_orchestrator.services.kiro_profiles import (
    atomic_write_text,
    kiro_artifact_path,
    kiro_summary_path,
    redacted_policy_summary_json,
    render_kiro_kas,
    render_kiro_v2,
)
from cli_agent_orchestrator.utils.kiro_policy import CompiledKiroPolicy


@dataclass(frozen=True)
class RenderedProfile:
    """A rendered artifact plus the policy that produced it.

    ``policy`` is ``None`` for v2: only the KAS arm compiles a Cedar policy.
    (The design sketched this field as non-optional; the v2 arm makes that
    impossible, so it is typed optional and the KAS-only nature is documented
    here rather than papered over with a placeholder object.)
    """

    text: str
    policy: Optional[CompiledKiroPolicy]
    engine: KiroEngine


@dataclass(frozen=True)
class InstallOutcome:
    """Where an artifact landed, its sidecar, and the policy behind it."""

    artifact_path: Path
    summary_path: Optional[Path]
    policy: Optional[CompiledKiroPolicy]


def render_profile(
    profile: AgentProfile,
    *,
    engine: KiroEngine,
    resources: list[str],
    mcp_servers: Optional[dict[str, object]],
    allowed_tools: Optional[list[str]] = None,
) -> RenderedProfile:
    """Render one profile for the selected engine (FR-105).

    Dispatch and wrap only. The behavioural change over the previous call sites
    is that ``CompiledKiroPolicy`` is **returned** rather than dropped —
    ``install_service`` used to do ``rendered, _ =``, which is precisely what
    FR-105 exists to stop (BR-U6-3).

    ``allowed_tools`` is required by the v2 renderer and ignored by the KAS arm,
    whose grant is derived from the compiled policy instead.
    """
    if engine is KiroEngine.KAS:
        text, policy = render_kiro_kas(profile, resources, mcp_servers)
        return RenderedProfile(text=text, policy=policy, engine=engine)

    text = render_kiro_v2(profile, allowed_tools or [], resources, mcp_servers)
    return RenderedProfile(text=text, policy=None, engine=engine)


def install_profile(
    profile: AgentProfile,
    *,
    directory: Path,
    engine: KiroEngine,
    resources: list[str],
    mcp_servers: Optional[dict[str, object]],
    allowed_tools: Optional[list[str]] = None,
    artifact_name: Optional[str] = None,
) -> InstallOutcome:
    """Render, then write the artifact and (for KAS) its redacted sidecar.

    Statement order is the mechanism, not a check (BR-U6-4): the render — and
    therefore the policy compile and FR-103 shape validation — completes before
    any write is attempted, so a profile that cannot translate never reaches
    disk. On failure nothing is written and any pre-existing artifact is
    byte-for-byte intact, for two independent reasons: the write is never
    reached, and the write itself is atomic (BR-U6-6).

    ``artifact_name`` lets a caller supply the already-sanitised filename stem
    when it differs from ``profile.name``; the path itself always comes from
    ``kiro_artifact_path``, which owns the traversal defense (BR-U6-8).
    """
    rendered = render_profile(
        profile,
        engine=engine,
        resources=resources,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
    )

    name = artifact_name or profile.name
    artifact_path = kiro_artifact_path(directory, name, engine)
    atomic_write_text(artifact_path, rendered.text)

    # Sidecar after the artifact, and KAS only (BR-U6-7 / SEC-U7-8): v2 has no
    # compiled Cedar policy to summarise, so a v2 install must produce no
    # `.summary.json`.
    summary_path: Optional[Path] = None
    if engine is KiroEngine.KAS and rendered.policy is not None:
        summary_path = kiro_summary_path(directory, name, engine)
        atomic_write_text(summary_path, redacted_policy_summary_json(rendered.policy))

    return InstallOutcome(
        artifact_path=artifact_path,
        summary_path=summary_path,
        policy=rendered.policy,
    )


def lint_profile(
    profile: AgentProfile,
    *,
    artifact_directory: Optional[Path] = None,
) -> KiroProfileLintResult:
    """Pure passthrough to ``lint_kiro_profile`` (BR-U6-9).

    Read-only and redaction-safe: nothing is added here that could write, and
    the report's shape is unchanged. This is the single translatability oracle
    the launch guard consults (FR-104).
    """
    if artifact_directory is None:
        return lint_kiro_profile(profile)
    return lint_kiro_profile(profile, artifact_directory)
