"""Pinned provider session/resume contracts (Codex, Claude, Kimi).

Each provider has exactly one pinned pre-turn native-identity source and
an exact set of accepted resume forms.  No provider may use a "newest"
or implicit-current-session shortcut, a completion-only hint as launch
authority, or a non-resumable mode.

Invariant: resume acceptance requires the exact pinned form for the
exact pinned provider version; installed-version drift fails closed
(outcome 41) until the pinned binary is stage-verified.

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
    # Accept an exact match or a "<name> <version>" banner tail match.
    normalized = installed_version.strip().split()[-1] if installed_version.strip() else ""
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
        return ResumeForm(provider, tuple(args), args[1])
    raise ResumeFormRefused("claude resume accepts exactly `--resume <uuid>`")


@dataclass(frozen=True)
class ProviderResumeStatus:
    """The honest per-provider resume capability (§7.3)."""

    provider: str
    identity_available: bool
    authority_supported: bool
    reason: str


def resume_status(
    provider: str,
    *,
    kimi_acp_proof_green: bool = False,
    route_receipt_proven: bool = False,
) -> ProviderResumeStatus:
    """Report the pinned resume support for one provider, truthfully.

    With PF-2 red for every enabled provider, no provider carries
    authority-bearing automated recovery: Codex and Kimi have resumable
    *identity* but unsupported authority; Claude is unsupported by
    default (no pre-input effort surface); Kimi's identity mechanics
    additionally require the installed-CLI ACP session/load proof.
    """
    if provider == PROVIDER_CODEX:
        return ProviderResumeStatus(
            provider=provider,
            identity_available=True,
            authority_supported=route_receipt_proven,
            reason=(
                "resume identity available (app-server thread id); automated "
                "recovery/strongest-route authority unsupported until a "
                "model-input-bound non-echo route receipt is proven"
            ),
        )
    if provider == PROVIDER_CLAUDE:
        return ProviderResumeStatus(
            provider=provider,
            identity_available=True,
            authority_supported=False,
            reason=(
                "resume identity available (--session-id/--resume); unsupported "
                "by default: no pre-input effort surface exists"
            ),
        )
    if provider == PROVIDER_KIMI:
        return ProviderResumeStatus(
            provider=provider,
            identity_available=kimi_acp_proof_green,
            authority_supported=False,
            reason=(
                "identity requires the installed-CLI ACP session/new→kill→"
                "session/load proof; effort authority unproven"
                if kimi_acp_proof_green
                else "identity disabled until the installed-CLI ACP "
                "session/new→kill→session/load proof passes"
            ),
        )
    raise ProviderContractError(f"unknown provider: {provider!r}")
