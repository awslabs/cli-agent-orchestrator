"""The single decision point for whether a KAS process may launch.

This module is the authorization boundary FR-101 exists to create: the seven
Phase 0 refusal sites are collapsed into one auditable function here. It lives
in ``utils`` (ADR-001) because ``utils`` sits below both ``providers`` and
``services``, so all four consuming modules import downward.

Two load-bearing properties, both structural rather than disciplinary:

* **Default refuses.** The opt-in flag is checked before any allowing path
  except the non-KAS passthrough (BR-U3-2).
* **No retained state.** Translatability is recomputed on every lint-gated call
  (ADR-003, BR-U3-3/4). There is no instance, no module-level cache, no
  ``@lru_cache``, and no cache parameter — there is nothing to go stale. A
  profile edited between install and launch is seen as it is *now*. A future
  "optimisation" that adds caching would be a security regression, not a
  speedup.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Mapping, Optional

from cli_agent_orchestrator import constants
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.kiro_launch import KiroLaunchRefusedError
from cli_agent_orchestrator.utils.kiro_policy import (
    CompiledKiroPolicy,
    KiroPolicyError,
    compile_kiro_policy,
)

GuardMode = Literal["lint-gated", "flag-only"]

#: Diagnostic code -> the ``AgentProfile`` field that produced it (ADR-009).
#:
#: Best-effort by design and typed ``str | None``: three codes are *genuinely*
#: ambiguous because ``_validate_unique_strings`` is called for ``allowedTools``,
#: ``deniedTools``, and ``tools`` alike and the compiler does not propagate which
#: one raised. Those map to ``None`` rather than to a guess (BR-U5-3) — a guess
#: presented as fact misdirects an operator on a security boundary. The
#: diagnostic *message* remains the primary actionable channel (BR-U5-6); it
#: already quotes the offending token.
#:
#: Values are hardcoded field *names*, never runtime values, so this table cannot
#: leak profile content (SEC-U5-1). Every code raised anywhere in ``src/`` must
#: appear here — mapped or explicitly ``None`` — and a drift test scanning all of
#: ``src/`` fails the build when one does not (BR-U5-1/BR-U5-7).
_CODE_TO_FIELD: Mapping[str, Optional[str]] = {
    "unsafe-aliases": "toolAliases",
    "unsupported-settings": "toolsSettings",
    "unknown-mcp-server": "mcpServers",
    "unsafe-mcp-grant": "mcpServers",
    "malformed-mcp": "mcpServers",
    "unsupported-mcp-field": "mcpServers",
    "unknown-role": "role",
    "malformed-role-policy": "role",
    # Install-path only: raised in services/kiro_profiles.render_kiro_kas during
    # resource merging, which lint_kiro_profile -> compile_kiro_policy never
    # runs. They are therefore never reached through the guard's lint-gated
    # lookup, but they are real codes surfaced to an operator at install time and
    # they benefit from attribution — do not delete them as dead entries, and do
    # not build a guard-path test that cannot reach them.
    "unknown-resource": "resources",
    "contradictory-resource": "resources",
    # Internal invariants, not attributable to any profile field.
    "malformed-cedar-rule": None,
    "serialization-error": None,
    # Genuinely ambiguous: allowedTools | deniedTools | tools (BR-U5-3).
    "unknown-capability": None,
    "contradictory-policy": None,
    "malformed-policy": None,
}


@dataclass(frozen=True)
class LaunchVerdict:
    """One launch-admissibility decision.

    ``mode`` records which gate ran so callers and tests can assert it rather
    than infer it. It reflects the *kind* of call site (creation vs
    restore/reuse/usage), which is determined solely by whether a profile was
    supplied — never by a flag, caller identity, or configuration.
    """

    allowed: bool
    engine: KiroEngine
    mode: GuardMode
    reason_code: Optional[str] = None
    profile_field: Optional[str] = None
    message: Optional[str] = None
    policy: Optional[CompiledKiroPolicy] = None


def _mode(profile: Optional[AgentProfile]) -> GuardMode:
    """Derive the gate kind from whether a profile is in scope (ADR-008)."""
    return "lint-gated" if profile is not None else "flag-only"


def check_kas_launch(
    *,
    engine: KiroEngine,
    profile: Optional[AgentProfile] = None,
) -> LaunchVerdict:
    """Decide whether a KAS launch is admissible (FR-101, FR-102, FR-104).

    Returns a verdict; it does **not** raise for a policy refusal (BR-U3-10) so
    a caller can inspect a decision without exception handling. Only
    :func:`assert_kas_launch_allowed` raises.

    Order is load-bearing:

    1. A non-KAS engine passes through untouched, before any flag or lint work,
       so v2 cannot be affected by a KAS regression (BR-U3-6).
    2. The opt-in flag is checked next, so KAS can never become reachable by
       omission (BR-U3-2) and a flag-off refusal costs no policy compile.
    3. No profile in scope means **flag-only** mode: five of the seven call
       sites have no parsed profile (ADR-008), and refusing them would break
       restore paths. Fail-closed is preserved by topology — creation is the
       only way a KAS terminal comes into existence, and creation is lint-gated.
    4. Otherwise **lint-gated**: translatability is recomputed now (ADR-003).
    """
    if engine is not KiroEngine.KAS:
        return LaunchVerdict(allowed=True, engine=engine, mode=_mode(profile))

    # Read through the module so ``monkeypatch.setattr(constants,
    # "ENABLE_KAS_LAUNCH", ...)`` is observable. The value is still resolved
    # exactly once, at ``constants`` import (ADR-007) — mutating the environment
    # after import has no effect on it.
    if not constants.ENABLE_KAS_LAUNCH:
        return LaunchVerdict(
            allowed=False,
            engine=engine,
            mode=_mode(profile),
            reason_code="launch-not-enabled",
            message=(
                "Kiro engine 'kas' launch is not enabled. KAS is experimental in "
                "this release; set CAO_ENABLE_KAS_LAUNCH=true to opt in, or retry "
                "with engine 'v2'."
            ),
        )

    if profile is None:
        return LaunchVerdict(allowed=True, engine=engine, mode="flag-only")

    # Lazy import: lint_kiro_profile lives in ``services``, which sits *above*
    # ``utils``. Importing it inside the function confines the upward edge to the
    # one call that needs it and preserves the module-level layering convention
    # (BR-U3-9; precedent at utils/terminal.py's provider_manager import).
    from cli_agent_orchestrator.services.kiro_profile_lint import lint_kiro_profile

    # Recomputed on every call — no cache, no memoisation (BR-U3-3). Deliberately
    # not wrapped in try/except: an error here must never resolve to allowed=True.
    result = lint_kiro_profile(profile)
    if not result.generation_safe:
        code = result.unsupported[0] if result.unsupported else None
        return LaunchVerdict(
            allowed=False,
            engine=engine,
            mode="lint-gated",
            reason_code="profile-untranslatable",
            profile_field=_CODE_TO_FIELD.get(code) if code else None,
            message=(
                f"Profile {profile.name!r} cannot be translated to a KAS Cedar "
                f"policy ({code or 'unknown'}). "
                f"{result.diagnostic or 'Run: cao profile lint ' + profile.name}"
            ),
        )

    # FR-105: surface the compiled policy on the verdict so a caller can inspect
    # the grant it was admitted under. The profile has just passed
    # ``generation_safe`` on the same in-process compile, so this cannot raise;
    # the narrow catch keeps the informational field from ever turning an
    # allowed launch into an error.
    try:
        policy: Optional[CompiledKiroPolicy] = compile_kiro_policy(profile)
    except KiroPolicyError:  # pragma: no cover - unreachable after generation_safe
        policy = None

    return LaunchVerdict(allowed=True, engine=engine, mode="lint-gated", policy=policy)


def assert_kas_launch_allowed(
    *,
    engine: KiroEngine,
    profile: Optional[AgentProfile] = None,
) -> None:
    """Raise :class:`KiroLaunchRefusedError` unless the launch is admissible.

    The one-liner used at all seven former raise sites (FR-101). Called after
    engine resolution and before any allocation, so a refusal leaves zero
    residue: no terminal row, no backend session, no provider process
    (BR-U3-11).
    """
    verdict = check_kas_launch(engine=engine, profile=profile)
    if verdict.allowed:
        return
    raise KiroLaunchRefusedError(
        code=verdict.reason_code or "launch-refused",
        message=verdict.message,
        profile_field=verdict.profile_field,
        engine=verdict.engine,
    )
