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

#: The single *current* pin per provider: the version a fresh mint/proof
#: is expected to run, and the one a receipt records when it cannot read a
#: more specific fact.  ``SUPPORTED_VERSIONS`` below is the acceptance
#: authority; this map is the representative head of each accepted tuple.
PINNED_VERSIONS = {
    PROVIDER_CODEX: "0.145.0",
    PROVIDER_KIMI: "0.29.1",
    PROVIDER_CLAUDE: "2.1.220",
}

#: Every exact version accepted for a provider, current first.  A tuple of
#: exact strings, never a range: which builds are proven is a fact about
#: each specific build, and a range would silently assert something about
#: builds nobody has read.  Kimi retains ``0.29.0`` so a session minted
#: under it still validates after the binary is stage-verified up to
#: ``0.29.1`` (installed bundle ``main.mjs`` sha256
#: cba31835395ff75fa6b5bc9b81a7907c7d933e7e6a7d8ba53afac23dd0f5ab04).
#:
#: Claude accepts only ``2.1.220``, the stage-verified installed build
#: (``versions/2.1.220`` sha256
#: 8addc857f3fe64d5a0368af9ee50321b50afb4a6918ba3ef018ab84f5dbbe081).
#: ``2.1.218`` is *not* retained: unlike Kimi, no managed Claude session
#: has ever been minted, so there is no existing receipt that retaining it
#: would keep valid — it would only assert that a build nobody has read
#: the composer behaviour of is acceptable.
SUPPORTED_VERSIONS: dict[str, tuple[str, ...]] = {
    PROVIDER_CODEX: ("0.145.0",),
    PROVIDER_KIMI: ("0.29.1", "0.29.0"),
    PROVIDER_CLAUDE: ("2.1.220",),
}
# The current pin must always be an accepted version — asserted here so the
# two maps cannot silently drift apart.
assert all(
    PINNED_VERSIONS[provider] in versions for provider, versions in SUPPORTED_VERSIONS.items()
)

# The sole accepted pre-turn native-identity source per provider.
NATIVE_ID_SOURCES = {
    PROVIDER_CODEX: "app_server_thread_start",  # app-server thread/start id
    PROVIDER_KIMI: "acp_session_new",  # ACP session/new sessionId
    PROVIDER_CLAUDE: "cli_session_id",  # explicit --session-id <uuid> at start
}

#: The reserved ``expected_effort`` meaning "this route selects no effort;
#: use whatever the provider does by default, and attest none".
#:
#: An explicit string rather than null or an omitted field, agreed with the
#: conductor side: the breaker's failure domain hashes effort as a string,
#: so a null would both weaken a deterministic domain key and read as
#: "unspecified" — which is a different claim from "this model has no
#: effort to specify". Callers echo it back byte-identically, so every
#: existing ``expected_effort`` identity comparison keeps matching.
EFFORT_PROVIDER_DEFAULT = "provider-default"

#: Models that expose no thinking-effort surface at all, so *any* concrete
#: effort is a protocol error rather than a preference the provider will
#: approximate.
#:
#: Read from the installed Kimi 0.29.1: ``kimi-code/kimi-for-coding`` (the
#: K2.7 route) advertises no ``support_efforts``, and both ``max`` and
#: ``high`` come back ``Invalid params`` — from the ACP probe and from a
#: real managed launch alike. An exact set rather than a capability probe,
#: for the same reason the version pins are exact: this is a fact about
#: specific builds that were read, and guessing at others would assert
#: something nobody verified.
EFFORTLESS_MODELS = frozenset({"kimi-code/kimi-for-coding"})


def route_selects_effort(effort: Optional[str]) -> bool:
    """Whether this route names an effort the provider should be told about.

    The single question every materialization point asks, so that "does an
    effort get sent?" has one answer rather than one per call site. Gating
    inside any individual probe would leave the others as traps: the first
    launch down an ungated path silently reinstates the override, and the
    failure surfaces as a provider protocol error far from its cause.
    """
    return bool(effort) and effort != EFFORT_PROVIDER_DEFAULT


def validate_route_effort(model: Optional[str], effort: Optional[str]) -> None:
    """Refuse a concrete effort for a model that has no effort surface.

    Fails here, with the sentinel named, rather than part-way through a
    provider probe with ``Invalid params`` — which says only that some
    parameter was wrong, in a response the caller has to map back to a
    field it did not know was unsupported.
    """
    if model in EFFORTLESS_MODELS and route_selects_effort(effort):
        raise ProviderContractError(
            f"model {model!r} exposes no thinking-effort surface, so effort "
            f"{effort!r} cannot be honored; route it with "
            f"expected_effort={EFFORT_PROVIDER_DEFAULT!r} to run at the "
            "provider default and attest no effort"
        )


def kimi_effort_env(effort: Optional[str]) -> dict:
    """The Kimi effort environment for a route — empty when it selects none.

    Empty rather than absent-keyed-to-a-default: the override is *omitted*,
    never translated into some other value, so the provider applies its own
    default and nothing here claims to know what that is.
    """
    return {"KIMI_MODEL_THINKING_EFFORT": effort} if route_selects_effort(effort) else {}


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


def normalized_version(installed_version: str) -> str:
    """The first semver-shaped token in a ``<name> <version> ...`` banner.

    ``"codex 0.145.0"``, ``"2.1.220 (Claude Code)"``, ``"kimi 0.29.1"`` all
    yield the bare version; an absent one yields ``""``.  Exposed so a
    caller can record the *actual* validated version rather than a pin
    constant that may name the wrong one of several accepted builds.
    """
    for token in installed_version.strip().split():
        if re.fullmatch(r"\d+\.\d+\.\d+", token):
            return token
    return ""


def check_pinned_version(provider: str, installed_version: str) -> None:
    """Fail closed unless the installed version is an accepted exact pin.

    Acceptance is exact-set membership, never a range: a provider may
    accept more than one build (Kimi retains ``0.29.0`` alongside the
    current ``0.29.1``), but every accepted version is a build that has
    been read and proven, not a bound guessing at ones that have not.
    """
    accepted = SUPPORTED_VERSIONS.get(provider)
    if accepted is None:
        raise ProviderContractError(f"unknown provider: {provider!r}")
    normalized = normalized_version(installed_version)
    if normalized not in accepted:
        raise ProviderVersionDrift(
            f"{provider} version drift: accepted {list(accepted)}, installed "
            f"{installed_version.strip()!r}; resume refuses (41) until a "
            "stage-verified pinned binary is installed"
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
    "generation",
    "observed_model",
    "observed_effort",
    "protocol_version",
    "event_sequence",
    "model_input_digest",
)

_ROUTE_PROOF_TEXT_FIELDS = (
    "native_session_id",
    "native_turn_id",
    "generation",
    "observed_model",
    "observed_effort",
    "protocol_version",
)

_DIGEST_64_HEX = re.compile(r"[0-9a-f]{64}")


def validate_route_proof(
    provider: str,
    route_proof: Optional[dict],
    *,
    expected_model: Optional[str] = None,
    expected_effort: Optional[str] = None,
    expected_model_input_digest: Optional[str] = None,
) -> bool:
    """True only for a validated provider-specific observed-route proof.

    Identity availability alone never satisfies this: the receipt must be
    the pinned ``cao-route-receipt-v1`` schema for THIS provider, carry
    every §3.1 field with the correct TYPE (typed native session/turn/
    generation identity, a positive integer event sequence, a 64-hex
    model-input digest), and be explicitly non-echo.  The provider-observed
    resolved model/effort must equal the pinned expectation — supplied by
    the authority boundary (``expected_model``/``expected_effort``) or
    embedded in an authenticated receipt as ``expected_model``/
    ``expected_effort``; with no pinned expectation there is no authority.
    When the authority boundary supplies the expected model-input digest,
    the receipt's digest must match it exactly.  Unknown, missing,
    malformed, drifted, or unsupported route evidence validates to False,
    which must expose no automated path.
    """
    if not isinstance(route_proof, dict):
        return False
    if route_proof.get("schema") != ROUTE_PROOF_SCHEMA:
        return False
    if route_proof.get("provider") != provider:
        return False
    for field in _ROUTE_PROOF_TEXT_FIELDS:
        value = route_proof.get(field)
        if not isinstance(value, str) or not value:
            return False
    sequence = route_proof.get("event_sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        return False
    digest = route_proof.get("model_input_digest")
    if not isinstance(digest, str) or _DIGEST_64_HEX.fullmatch(digest) is None:
        return False
    if route_proof.get("non_echo") is not True:
        return False
    pinned_model = (
        expected_model if expected_model is not None else route_proof.get("expected_model")
    )
    if (
        not isinstance(pinned_model, str)
        or not pinned_model
        or route_proof["observed_model"] != pinned_model
    ):
        return False
    pinned_effort = (
        expected_effort if expected_effort is not None else route_proof.get("expected_effort")
    )
    if (
        not isinstance(pinned_effort, str)
        or not pinned_effort
        or route_proof["observed_effort"] != pinned_effort
    ):
        return False
    if expected_model_input_digest is not None and digest != expected_model_input_digest:
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
    expected_model: Optional[str] = None,
    expected_effort: Optional[str] = None,
    expected_model_input_digest: Optional[str] = None,
) -> ProviderResumeStatus:
    """Report the pinned resume support for one provider, truthfully.

    Every claim derives from provider-specific, version-checked,
    generation-bound receipts: ``installed_version`` is the live
    ``--version`` fact for the pinned binary (drift or absence removes
    the capability), ``kimi_acp_proof`` is the validated durable ACP
    new→kill→load receipt, and ``route_proof`` is the provider-specific
    model-input-bound non-echo route receipt, validated against the
    pinned route expectation supplied by the authority boundary
    (``expected_model``/``expected_effort``/``expected_model_input_digest``).
    With PF-2 red and no receipts, every provider reports
    unproven/unsupported — caller booleans can no longer promote anything.
    """
    if provider == PROVIDER_CODEX:
        version_ok = _version_matches(PROVIDER_CODEX, installed_version)
        return ProviderResumeStatus(
            provider=provider,
            identity_available=version_ok,
            authority_supported=version_ok
            and validate_route_proof(
                provider,
                route_proof,
                expected_model=expected_model,
                expected_effort=expected_effort,
                expected_model_input_digest=expected_model_input_digest,
            ),
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
