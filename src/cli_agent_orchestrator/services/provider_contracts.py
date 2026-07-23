"""Pinned provider session/resume contracts (Codex, Claude, Kimi).

Each provider has exactly one pinned pre-turn native-identity source and
an exact set of accepted resume forms.  No provider may use a "newest"
or implicit-current-session shortcut, a completion-only hint as launch
authority, or a non-resumable mode.

Invariant: resume acceptance requires the exact pinned form for the
exact pinned provider version; installed-version drift fails closed
(outcome 41) until the pinned binary is stage-verified.  Claude's
native id is a canonical UUID and is shape-validated.  Capability
claims derive only from provider-specific, version-checked receipts —
never from caller-supplied booleans.

Failure mode prevented: ``--continue``/``--last``/newest-session forms
bind whatever session happens to be newest — after any other session
activity that is the *wrong* identity, and a resume bound to it
silently resumes the wrong session's context.

Why this guard exists: the native-session ledger and the resume
admission contract are only sound when the resumed identity is the
exact provider-native id captured before the first turn, never an
ambient or recency-derived one.
"""

from __future__ import annotations

import re
import uuid as _uuid_module
from dataclasses import dataclass
from typing import Optional

PROVIDER_CODEX = "codex"
PROVIDER_KIMI = "kimi"
PROVIDER_CLAUDE = "claude"
PROVIDERS = (PROVIDER_CODEX, PROVIDER_KIMI, PROVIDER_CLAUDE)

PINNED_VERSIONS = {
    PROVIDER_CODEX: "0.145.0",
    PROVIDER_KIMI: "0.29.0",
    PROVIDER_CLAUDE: "2.1.218",
}

# The sole accepted pre-turn native-identity source per provider.
NATIVE_ID_SOURCES = {
    PROVIDER_CODEX: "app_server_thread_start",  # app-server thread/start id
    PROVIDER_KIMI: "acp_session_new",  # ACP session/new sessionId
    PROVIDER_CLAUDE: "cli_session_id",  # explicit --session-id <uuid> at start
}

# Resume outcome codes (the public CLI contract).
OUTCOME_RESUMED = 0
OUTCOME_REFUSED_MISMATCH = 40
OUTCOME_CAPABILITY_UNSUPPORTED = 41
OUTCOME_AMBIGUOUS_PRESERVED = 42
OUTCOME_ALREADY_FINALIZED = 43
OUTCOME_NOT_RESUMABLE = 44
OUTCOME_PRIOR_UNPROVEN = 45


class ProviderContractError(ValueError):
    """Base error for provider-contract violations."""


class ProviderVersionDrift(ProviderContractError):
    """Installed provider version differs from the pinned contract."""


class ResumeFormRefused(ProviderContractError):
    """A forbidden or non-exact resume form was requested."""


def check_pinned_version(provider: str, installed_version: str) -> None:
    """Fail closed on installed-version drift against the pinned contract."""
    pinned = PINNED_VERSIONS.get(provider)
    if pinned is None:
        raise ProviderContractError(f"unknown provider: {provider!r}")
    # Accept the exact pin anywhere in a "<name> <version> ..." banner:
    # the first semver-shaped token is the version fact ("codex 0.145.0",
    # "2.1.218 (Claude Code)", "kimi 0.29.0" all parse).
    tokens = installed_version.strip().split()
    normalized = next((token for token in tokens if re.fullmatch(r"\d+\.\d+\.\d+", token)), "")
    if normalized != pinned:
        raise ProviderVersionDrift(
            f"{provider} version drift: pinned {pinned}, installed "
            f"{installed_version.strip()!r}; resume refuses (41) until the pinned "
            "binary is stage-verified"
        )


def native_id_source(provider: str) -> str:
    source = NATIVE_ID_SOURCES.get(provider)
    if source is None:
        raise ProviderContractError(f"unknown provider: {provider!r}")
    return source


@dataclass(frozen=True)
class ResumeForm:
    provider: str
    argv: tuple[str, ...]
    native_id: str


def validate_resume_argv(provider: str, argv: list[str]) -> ResumeForm:
    """Validate one resume invocation against the exact pinned forms.

    Accepted, and only accepted:
      codex:  ``codex resume <id>`` · ``codex exec resume <id>``
      kimi:   ``--session <id>`` · ``-r <id>``
      claude: ``--resume <uuid>``
    Forbidden forms refuse with zero provider I/O.
    """
    if provider not in PROVIDERS:
        raise ResumeFormRefused(f"unknown provider: {provider!r}")
    args = list(argv)
    forbidden = {
        "--continue": "newest-session shortcuts are forbidden (no implicit "
        "current session may ever be resumed)",
        "--last": "newest-session shortcuts are forbidden",
        "-c": "newest-session shortcuts are forbidden",
        "--fork-session": "forked sessions break the exact-identity binding",
        "--ephemeral": "ephemeral sessions are non-resumable by construction",
        "--no-session-persistence": "non-persistent sessions are non-resumable",
    }
    for flag in args:
        if flag in forbidden:
            raise ResumeFormRefused(forbidden[flag])
    if provider == PROVIDER_CODEX:
        if len(args) == 2 and args[0] == "resume" and args[1] and not args[1].startswith("-"):
            return ResumeForm(provider, tuple(args), args[1])
        if (
            len(args) == 3
            and args[0] == "exec"
            and args[1] == "resume"
            and args[2]
            and not args[2].startswith("-")
        ):
            return ResumeForm(provider, tuple(args), args[2])
        raise ResumeFormRefused(
            "codex resume accepts exactly `codex resume <id>` or " "`codex exec resume <id>`"
        )
    if provider == PROVIDER_KIMI:
        if len(args) == 2 and args[0] in ("--session", "-r") and args[1]:
            return ResumeForm(provider, tuple(args), args[1])
        raise ResumeFormRefused("kimi resume accepts exactly `--session <id>` or `-r <id>`")
    # claude
    if len(args) == 2 and args[0] == "--resume" and args[1] and not args[1].startswith("-"):
        native_id = args[1]
        try:
            parsed = _uuid_module.UUID(native_id)
        except ValueError as exc:
            raise ResumeFormRefused(
                "claude resume native id must be a canonical UUID "
                f"(the --session-id form); got {native_id!r}"
            ) from exc
        if str(parsed) != native_id:
            raise ResumeFormRefused("claude resume native id must be a canonical lowercase UUID")
        return ResumeForm(provider, tuple(args), native_id)
    raise ResumeFormRefused("claude resume accepts exactly `--resume <uuid>`")


@dataclass(frozen=True)
class ProviderResumeStatus:
    """The honest per-provider resume capability (§7.3)."""

    provider: str
    identity_available: bool
    authority_supported: bool
    reason: str


ROUTE_PROOF_SCHEMA = "cao-route-receipt-v1"

# The §3.1 PF-2 fields a model-input-bound non-echo route receipt must
# carry before any provider may bear automated-recovery/strongest-route
# authority.
ROUTE_PROOF_REQUIRED_FIELDS = (
    "native_session_id",
    "native_turn_id",
    "observed_model",
    "observed_effort",
    "protocol_version",
    "event_sequence",
    "model_input_digest",
)


def validate_route_proof(provider: str, route_proof: Optional[dict]) -> bool:
    """True only for a validated provider-specific observed-route proof.

    Identity availability alone never satisfies this: the receipt must be
    the pinned ``cao-route-receipt-v1`` schema for THIS provider, carry
    every §3.1 field (native session/thread id, turn id, resolved model,
    effective effort, protocol version, event sequence), be bound to the
    model input, and be explicitly non-echo. Unknown, missing, malformed,
    or unsupported route evidence validates to False, which must expose no
    automated path.
    """
    if not isinstance(route_proof, dict):
        return False
    if route_proof.get("schema") != ROUTE_PROOF_SCHEMA:
        return False
    if route_proof.get("provider") != provider:
        return False
    for field in ROUTE_PROOF_REQUIRED_FIELDS:
        value = route_proof.get(field)
        if value is None or (isinstance(value, str) and not value):
            return False
    if route_proof.get("non_echo") is not True:
        return False
    return True


def _version_matches(provider: str, installed_version: Optional[str]) -> bool:
    """True only when the installed version is known and matches the pin."""
    if installed_version is None:
        return False
    try:
        check_pinned_version(provider, installed_version)
    except ProviderVersionDrift:
        return False
    return True


def resume_status(
    provider: str,
    *,
    installed_version: Optional[str] = None,
    kimi_acp_proof: Optional[dict] = None,
    route_proof: Optional[dict] = None,
) -> ProviderResumeStatus:
    """Report the pinned resume support for one provider, truthfully.

    Every claim derives from provider-specific, version-checked,
    generation-bound receipts: ``installed_version`` is the live
    ``--version`` fact for the pinned binary (drift or absence removes
    the capability), ``kimi_acp_proof`` is the validated durable ACP
    new→kill→load receipt, and ``route_proof`` is the provider-specific
    model-input-bound non-echo route receipt.  With PF-2 red and no
    receipts, every provider reports unproven/unsupported — caller
    booleans can no longer promote anything.
    """
    if provider == PROVIDER_CODEX:
        version_ok = _version_matches(PROVIDER_CODEX, installed_version)
        return ProviderResumeStatus(
            provider=provider,
            identity_available=version_ok,
            authority_supported=version_ok and validate_route_proof(provider, route_proof),
            reason=(
                "resume identity available (app-server thread id); automated "
                "recovery/strongest-route authority unsupported until a "
                "model-input-bound non-echo route receipt is proven"
                if version_ok
                else "identity unavailable: pinned Codex binary is absent or "
                "version-drifted (fail closed, outcome 41)"
            ),
        )
    if provider == PROVIDER_CLAUDE:
        version_ok = _version_matches(PROVIDER_CLAUDE, installed_version)
        return ProviderResumeStatus(
            provider=provider,
            identity_available=version_ok,
            authority_supported=False,
            reason=(
                "resume identity available (--session-id/--resume); unsupported "
                "by default: no pre-input effort surface exists"
                if version_ok
                else "identity unavailable: pinned Claude binary is absent or "
                "version-drifted (fail closed, outcome 41)"
            ),
        )
    if provider == PROVIDER_KIMI:
        version_ok = _version_matches(PROVIDER_KIMI, installed_version)
        proven = version_ok and kimi_acp_proof is not None
        return ProviderResumeStatus(
            provider=provider,
            identity_available=proven,
            authority_supported=False,
            reason=(
                "identity requires the installed-CLI ACP session/new→kill→"
                "session/load proof; effort authority unproven"
                if proven
                else "identity disabled until the pinned binary is "
                "version-verified and the installed-CLI ACP "
                "session/new→kill→session/load proof passes"
            ),
        )
    raise ProviderContractError(f"unknown provider: {provider!r}")
