"""Truthful pre-activation assessment of installed exact-resume cells."""

from __future__ import annotations

from dataclasses import dataclass

from cli_agent_orchestrator.services import (
    codex_native_bootstrap,
    kimi_native_launch,
    muse_native_launch,
    provider_contracts,
)


@dataclass(frozen=True)
class CellAssessment:
    runnable: bool
    reason: str
    session_proof: str | None = None


def assess_cell(
    provider: str,
    *,
    normalized_version: str,
    executable_path: str | None = None,
    executable_sha256: str | None = None,
    version_banner: str | None = None,
) -> CellAssessment:
    if provider == "codex":
        runnable = codex_native_bootstrap.is_bootstrap_capable_build(normalized_version)
        return CellAssessment(
            runnable,
            "exact installed bootstrap gate" if runnable else "bootstrap build is not proven",
            "argv" if runnable else None,
        )
    if provider == "kimi_cli":
        runnable = kimi_native_launch.rendered_session_proof_for(normalized_version) is not None
        return CellAssessment(
            runnable,
            "exact rendered-session proof" if runnable else "rendered session proof is absent",
            kimi_native_launch.RULE_KIMI_NATIVE_HEADER if runnable else None,
        )
    if provider == "muse_cli":
        if executable_path is None or version_banner is None:
            return CellAssessment(False, "profile carrier is unproven")
        carrier = muse_native_launch.profile_carrier_capability(
            wrapper_executable=executable_path,
            full_banner=version_banner,
        )
        runnable = carrier.supported
        return CellAssessment(
            runnable,
            "exact profile-carrier cell" if runnable else "profile carrier is unproven",
            "argv" if runnable else None,
        )
    if provider == "claude_code":
        # Under the unpinned policy the SessionStart hook is the runtime
        # proof, so any build whose version was observed is runnable; a
        # failed observation is not.
        runnable = bool(normalized_version)
        return CellAssessment(
            runnable,
            (
                "observed native-identity build"
                if runnable
                else "the installed build's version could not be observed"
            ),
            "argv" if runnable else None,
        )
    return CellAssessment(False, f"no B3 exact-resume path for {provider!r}")


def assess_variation(provider: str, *, execution_mode: str) -> CellAssessment:
    return CellAssessment(
        False,
        f"{provider}/{execution_mode} variations remain typed-disabled until M3-F publication",
    )
