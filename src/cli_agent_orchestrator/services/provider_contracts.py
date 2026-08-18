"""Provider version and native-session contracts.

Each provider has one attested pre-turn native-identity source and an exact
set of accepted resume forms. Routine launch admission is open by default so
an ordinary CLI update does not freeze a campaign, and capability is
unpinned: an installed build receives full capability through per-surface
conservative defaults and runtime reads of its own bundle, without being
listed anywhere first.  ``SUPPORTED_VERSIONS`` survives only as the
strict-mode quarantine set, consulted when an operator forces
``CAO_PROVIDER_VERSION_ENFORCEMENT_<PROVIDER>=strict`` after a reproduced
regression.  Unparseable version banners fail closed in every mode:
unparseable is a failed observation, not merely an unlisted build.

No provider may use a "newest" or implicit-current-session shortcut, a
completion-only hint as launch authority, or a non-resumable mode. Claude's
native id is a canonical UUID and is shape-validated. Capability claims derive
only from provider-specific, version-checked receipts — never from
caller-supplied booleans.

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

import os
import re
import uuid as _uuid_module
from dataclasses import dataclass
from typing import Optional

PROVIDER_CODEX = "codex"
PROVIDER_KIMI = "kimi"
PROVIDER_CLAUDE = "claude"
PROVIDER_MUSE = "muse"
PROVIDERS = (PROVIDER_CODEX, PROVIDER_KIMI, PROVIDER_CLAUDE, PROVIDER_MUSE)

#: The *canonical wire keys*, which are a different namespace from the
#: three above. Those name the executable and key the version-pin tables;
#: these are what a reservation, a launch surface and a capability
#: response call the provider. The two are deliberately not merged: the
#: recovery-capability surface is closed over the short names and the v2
#: launch surface is closed over these, and a helper that accepted either
#: would let a caller satisfy one contract with the other's vocabulary.
PROVIDER_CODEX_WIRE = "codex"
PROVIDER_KIMI_CLI = "kimi_cli"
PROVIDER_CLAUDE_CODE = "claude_code"
PROVIDER_MUSE_CLI = "muse_cli"

#: Version-enforcement modes for the provider-version policy.
#:
#: ``strict``  exact-set membership in ``SUPPORTED_VERSIONS``: the version
#:             must be listed to launch at all.  This is the quarantine
#:             mode — an explicit operator choice for a reproduced
#:             regression or a rollout that must remain on a known-good
#:             binary, never the normal update policy.
#: ``open``    any non-empty semver-shaped version is accepted at the
#:             launch identity boundary, with full capability: the
#:             per-surface conservative defaults and runtime bundle reads
#:             cover unlisted builds.  This is the default for every
#:             provider, so routine CLI updates freeze neither admission
#:             nor capability.
VERSION_ENFORCEMENT_STRICT = "strict"
VERSION_ENFORCEMENT_OPEN = "open"

#: The default enforcement mode per provider.  All providers are open by
#: default: routine CLI updates should not freeze admission merely because a
#: version table has not caught up.  Exact pins remain an explicit, reversible
#: operator choice for a reproduced regression.
#: The mode can be overridden per-provider at runtime with
#: ``CAO_PROVIDER_VERSION_ENFORCEMENT_<PROVIDER>=strict|open``.
VERSION_ENFORCEMENT_MODE: dict[str, str] = {
    PROVIDER_CODEX: VERSION_ENFORCEMENT_OPEN,
    PROVIDER_KIMI: VERSION_ENFORCEMENT_OPEN,
    PROVIDER_CLAUDE: VERSION_ENFORCEMENT_OPEN,
    PROVIDER_MUSE: VERSION_ENFORCEMENT_OPEN,
}

# Runtime configuration is documented in the short provider vocabulary
# (``KIMI``, ``CLAUDE``, ``MUSE``), while managed-launch requests use the wire
# vocabulary (``kimi_cli``, ``claude_code``, ``muse_cli``).  Keep one explicit
# mapping at this boundary so a strict override cannot be silently ignored when
# a launch reaches the wire-facing path.
_VERSION_ENV_SUFFIX: dict[str, str] = {
    PROVIDER_CODEX: "CODEX",
    PROVIDER_KIMI: "KIMI",
    PROVIDER_KIMI_CLI: "KIMI",
    PROVIDER_CLAUDE: "CLAUDE",
    PROVIDER_CLAUDE_CODE: "CLAUDE",
    PROVIDER_MUSE: "MUSE",
    PROVIDER_MUSE_CLI: "MUSE",
}

# The launch layer uses wire identifiers, while the default policy table is
# intentionally keyed by the short provider vocabulary.  Normalize both at
# this boundary so a wire launch inherits the same open-by-default policy
# when no temporary environment override is present.
_VERSION_POLICY_KEY: dict[str, str] = {
    PROVIDER_KIMI_CLI: PROVIDER_KIMI,
    PROVIDER_CLAUDE_CODE: PROVIDER_CLAUDE,
    PROVIDER_MUSE_CLI: PROVIDER_MUSE,
}

#: The pre-task identity launch markers shared by every admission surface.
#
#: One simple launch/readiness marker, not a claim or lease: an activated
#: ordinary (unmanaged) launch stamps its terminal row with ``PENDING`` at
#: row creation so the row is fail-closed from its first durable
#: visibility, keeps the roster lineage on ``PENDING``/``CAPTURED`` while
#: the pre-task identity is resolved and the provider warms up, and
#: transitions to ``READY`` only after provider/TUI initialization
#: succeeds.  The markers live here (a dependency-free contract module) so
#: the row writer (``clients/database``), the roster, and the admission
#: seam can all name the same vocabulary without an import cycle.
PRE_TASK_IDENTITY_PENDING = "pre-task native identity pending"
PRE_TASK_IDENTITY_CAPTURED = "pre-task native identity captured"
PRE_TASK_IDENTITY_READY = "pre-task native identity ready"

#: The single *current* pin per provider: the version a fresh mint/proof
#: is expected to run, and the one a receipt records when it cannot read a
#: more specific fact.  This map is advisory everywhere — the representative
#: head of each provider's accepted tuple, naming the build that is
#: known-good today.  It is not a ceiling, not an equality check, and not a
#: capability authority: an unlisted build launches with full capability.
PINNED_VERSIONS = {
    PROVIDER_CODEX: "0.146.0",
    PROVIDER_KIMI: "0.34.0",
    PROVIDER_CLAUDE: "2.1.233",
    PROVIDER_MUSE: "0.1.0",
}

#: Every exact version accepted for a provider, current first.  A tuple of
#: exact strings, never a range: which builds are proven is a fact about
#: each specific build, and a range would silently assert something about
#: builds nobody has read.
#:
#: Kimi accepts ``0.34.0`` (cond-0331: the generic provider-version policy
#: moves Kimi to open launch admission so normal future updates do not trip
#: stale launch breakers before task bytes. The installed 0.34.0 bundle was
#: inspected at SHA-256
#: ``d3e781774e7a95f71e9d813e2cda95486d15db73712b3e821dd4a357b0511d8c``;
#: source inspection found the observed composer, paste, steer, process-title
#: and session-option facts, and a bounded private-TUI probe rendered its
#: exact version/session header. A durable ACP kill/load proof was *not*
#: established, so resume and route authority remain gated by the exact proof
#: table rather than inferred from this observation), retains ``0.33.0``
#: (cond-0315: the provider's
#: supported background updater installed it over 0.32.0 mid-verification
#: — ``~/.kimi-code/updates/`` ``rollout.log``
#: ``startup-cache reason:"eligible"`` at 2026-08-05T09:54:47Z,
#: ``install.json.lastSuccess`` at 09:54:49Z — and every gate refused it
#: fail-closed; the installed 0.33.0 bundle ``main.mjs`` sha256
#: 0e77b9c64e67a4eecb96aae011750668aab11bd781564fe3e4855513812247b2,
#: matching the npm-published digest, declares the composer-newline/submit,
#: paste-burst, steer-chord, process-title-rewrite, resume-option, and
#: native-header facts byte-identical to the attested 0.32.0 bundle.  Its
#: ACP surface is natively reimplemented, so it was proven live rather than
#: by bytes: bounded ``--version`` (0.38–0.47 s warm), zero-prompt ACP
#: K3/max select+read-back with ``agentInfo.version`` agreement, the
#: durable ACP session/new→kill→session/load proof on the installed
#: binary, and a private-tmux resume rendering the strict boot header with
#: the kernel argv rewritten to ``['kimi-code','','','']``), accepts
#: ``0.32.0`` (cond-0315: the
#: operator's binary auto-updated again and the managed launcher refused
#: it fail-closed — run cond-0303-pr74-review-k3-r5, zero task bytes; the
#: installed 0.32.0 bundle ``main.mjs`` sha256
#: b02ebfe77dda7d9f38cf61c5a923567eb7ff4f3bc914dff24b02b5fd22b4ff79,
#: matching the npm-published digest, declares the composer-newline/submit,
#: paste-burst, steer-chord, process-title-rewrite, resume-option, and
#: native-header facts byte-identical to the attested 0.31.0 bundle — and
#: the build was live-verified: bounded ``--version``, zero-prompt ACP
#: K3/max route observation, exact ``--session <id>`` resume continuity,
#: and the rendered boot header on a private tmux stage), accepts
#: ``0.31.0`` (cond-0310: the
#: operator's binary auto-updated again; the installed 0.31.0 bundle
#: ``main.mjs`` sha256
#: 689fc2a123dfc3145dab26a8e6a86c71a5dc8552b13fe0449679e065ce96774e
#: declares the composer-newline/submit, paste-burst, and steer-chord facts
#: byte-identical to the 0.29.x/0.30.0 line — ``tui.input.newLine``
#: ``['shift+enter','ctrl+j']`` with ``tui.input.submit`` ``'enter'``,
#: ``submitValue()`` computes ``expandPasteMarkers(lines.join('\\n')).trim()``,
#: ``PASTE_ENTER_SUPPRESS_WINDOW_MS = 120``, and ``Key.ctrl("s")`` for steer),
#: so its bundle-read tier is proven; live text/control EFFECT on the real
#: build remains a separate post-review acceptance gate), accepts ``0.30.0``
#: (cond-0198: the operator's binary auto-updated; the installed 0.30.0
#: bundle's composer, paste-burst, and steer-chord facts read byte-identical
#: to the 0.29.x line, with the text/control path live-proven on the real
#: build) and retains ``0.29.2``, ``0.29.1``, and ``0.29.0`` so sessions
#: minted under them still validate (installed 0.29.2 bundle ``main.mjs``
#: sha256 2ee6e2f15d68bffdce532d1c8e50f8d40e0230090b3a0dd1dbcdb7c29bf532db,
#: matching the npm-published digest; the three-way 0.29.0/0.29.1/0.29.2
#: bundle comparison proved the composer, paste-burst, and steer-chord
#: facts byte-identical).  Image delivery authority stays pinned to
#: ``0.29.2`` alone — no image block is advertised for ``0.30.0``,
#: ``0.31.0``, ``0.32.0``, ``0.33.0``, or ``0.34.0``.
#:
#: Claude accepts only ``2.1.220``, the stage-verified installed build
#: (``versions/2.1.220`` sha256
#: 8addc857f3fe64d5a0368af9ee50321b50afb4a6918ba3ef018ab84f5dbbe081).
#: ``2.1.218`` is *not* retained: unlike Kimi, no managed Claude session
#: has ever been minted, so there is no existing receipt that retaining it
#: would keep valid — it would only assert that a build nobody has read
#: the composer behaviour of is acceptable.
#:
#: Codex accepts only ``0.146.0``.  Its generated app-server schemas were
#: compared with ``0.145.0`` for every method CAO uses: ``initialize``,
#: ``config/read``, ``thread/start``, ``turn/start``, and ``turn/interrupt``.
#: Their required request and consumed response fields are unchanged; the
#: observed differences are property ordering or additive optional fields.
#:
#: In open-enforcement mode (every provider's default) a launch succeeds
#: against any non-empty semver-shaped version and receives full capability:
#: the per-surface conservative defaults and runtime bundle reads cover
#: builds nobody has listed.  The tuple below is consulted again only when a
#: provider is forced to strict mode after a reproduced regression — it is
#: the quarantine set, not a capability gate.
SUPPORTED_VERSIONS: dict[str, tuple[str, ...]] = {
    PROVIDER_CODEX: ("0.146.0",),
    PROVIDER_KIMI: ("0.34.0", "0.33.0", "0.32.0", "0.31.0", "0.30.0", "0.29.2", "0.29.1", "0.29.0"),
    PROVIDER_CLAUDE: ("2.1.233", "2.1.220"),
    PROVIDER_MUSE: ("0.1.0",),
}
# The current pin must always be an accepted version — asserted here so the
# two maps cannot silently drift apart.
assert all(
    PINNED_VERSIONS[provider] in versions for provider, versions in SUPPORTED_VERSIONS.items()
)

#: Seconds one ``--version`` observation may take before the launch fails
#: closed.  The bound must be finite — a probe that cannot answer inside it
#: fails before any pane, session, or task byte — but it must also survive
#: a cold start on a loaded host: a healthy pinned Kimi 0.31.0 answered in
#: 0.37–0.41 s warm, yet one campaign launch observed its Node bundle miss
#: a fixed 5 s deadline under startup load and the launch failed closed
#: before any delivery (cond-0313).  Kimi therefore observes under the
#: same 20 s bound the route attestor's ACP probe and the native-TUI
#: acceptance harness already allow this exact probe; every other provider
#: keeps the generic bound.  One adequate deadline, never a replayed
#: launch.
VERSION_PROBE_TIMEOUT_SECONDS = 5.0
KIMI_VERSION_PROBE_TIMEOUT_SECONDS = 20.0

#: Keyed by the canonical *wire* provider name — the launch surfaces that
#: consult this table speak that namespace (``request["provider"]``), and
#: the short recovery names deliberately find no entry here.
VERSION_PROBE_TIMEOUTS: dict[str, float] = {
    PROVIDER_KIMI_CLI: KIMI_VERSION_PROBE_TIMEOUT_SECONDS,
}


def version_probe_timeout(provider: str) -> float:
    """The bounded ``--version`` observation deadline for one wire provider.

    Exact-name lookup, never a prefix match or a widened default: a
    provider with no entry keeps the generic bound, so one provider's
    runway cannot silently widen another's.
    """
    return VERSION_PROBE_TIMEOUTS.get(provider, VERSION_PROBE_TIMEOUT_SECONDS)


def version_enforcement_mode(provider: str) -> str:
    """The active enforcement mode for ``provider``.

    Defaults come from :data:`VERSION_ENFORCEMENT_MODE`.  Each provider can
    be forced back to strict exact pins at runtime by setting
    ``CAO_PROVIDER_VERSION_ENFORCEMENT_<PROVIDER>=strict`` (or forward to
    open with ``=open``).  This is the generic re-enable/rollback path: if
    a future Kimi build causes a reproducible regression, set
    ``CAO_PROVIDER_VERSION_ENFORCEMENT_KIMI=strict`` to restore exact-pin
    fail-closed behaviour without a code change.
    """
    suffix = _VERSION_ENV_SUFFIX.get(provider, provider.upper())
    env_var = f"CAO_PROVIDER_VERSION_ENFORCEMENT_{suffix}"
    override = os.environ.get(env_var)
    # Preserve compatibility with callers that configured the wire spelling
    # before the short-name policy was documented.  The short-name variable
    # wins if both are present, so one provider has one deterministic setting.
    if override not in (VERSION_ENFORCEMENT_STRICT, VERSION_ENFORCEMENT_OPEN):
        wire_suffix = provider.upper()
        if wire_suffix != suffix:
            override = os.environ.get(f"CAO_PROVIDER_VERSION_ENFORCEMENT_{wire_suffix}")
    if override in (VERSION_ENFORCEMENT_STRICT, VERSION_ENFORCEMENT_OPEN):
        return override
    policy_key = _VERSION_POLICY_KEY.get(provider, provider)
    return VERSION_ENFORCEMENT_MODE.get(policy_key, VERSION_ENFORCEMENT_STRICT)


# The sole accepted pre-turn native-identity source per provider.
NATIVE_ID_SOURCES = {
    PROVIDER_CODEX: "app_server_thread_start",  # app-server thread/start id
    PROVIDER_KIMI: "acp_session_new",  # ACP session/new sessionId
    PROVIDER_CLAUDE: "cli_session_id",  # explicit --session-id <uuid> at start
    # Muse's fresh launch starts a no-prompt TUI that generates the session
    # id itself; the managed launch discovers it from the provider's own
    # /status panel at zero turns (verified on the installed 0.1.0-R708.1
    # build: fresh `muse <route>` -> /status -> provider UUID, then
    # `muse resume <id>` restores it).  `muse resume <known-id>` is the
    # restoration form, not a caller-chosen creation.
    PROVIDER_MUSE: "provider_status_discovered",
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
#: real managed launch alike. Confirmed again on the installed 0.29.2:
#: the zero-prompt ACP ``initialize`` + ``session/new`` selected
#: ``kimi-code/kimi-for-coding`` with no selectable effort. An exact set
#: rather than a capability probe, for the same reason the version pins
#: are exact: this is a fact about specific builds that were read, and
#: guessing at others would assert something nobody verified.
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


#: How an effort can be learned for one ``(provider, model)`` pair. Three
#: values, three different claims — and the difference between the last
#: two is load-bearing, not a nicety.
#:
#: ``observable``          the provider resolves an effort and it can be
#:                         read back before a turn, so a receipt states it
#:                         and bind requires observed == requested.
#: ``none``                the model exposes no effort surface at all. The
#:                         route carries the provider-default sentinel and
#:                         the observation is null.
#: ``unobserved-pre-turn`` the model *has* an effort surface, but nothing
#:                         can read it before the first turn. The route
#:                         keeps its concrete requested effort; only the
#:                         observation is null.
#:
#: A model with no effort surface and a model whose effort cannot yet be
#: *seen* are different facts. The sentinel that truthfully describes the
#: first would be a lie about the second, so a provider in the third class
#: must never be routed through the sentinel to make the plumbing simpler:
#: that would silently discard a real requested effort. Should an
#: authoritative pre-turn proof appear later, the pair is redeclared
#: ``observable`` and enters the existing equality with no new field and
#: no new branch.
EFFORT_OBSERVABLE = "observable"
EFFORT_OBSERVABILITY_NONE = "none"
EFFORT_UNOBSERVED_PRE_TURN = "unobserved-pre-turn"

#: Pairs whose observability differs from the default ``observable``.
#: Keyed by provider, then by model where the answer is model-specific.
#: Claude is provider-wide: no Claude model exposes a pre-turn effort
#: reading, which is the same fact the route attestor already records when
#: it refuses to name an observed effort.
_EFFORT_OBSERVABILITY: dict = {
    PROVIDER_CLAUDE_CODE: {None: EFFORT_UNOBSERVED_PRE_TURN},
    PROVIDER_KIMI_CLI: {model: EFFORT_OBSERVABILITY_NONE for model in EFFORTLESS_MODELS},
}


def effort_observability(provider: Optional[str], model: Optional[str]) -> str:
    """The declared observability of one ``(provider, model)`` pair.

    Defaults to ``observable``, which is what Codex and the K3 Kimi routes
    already are: an undeclared pair keeps the strict equality it has
    today, so adding a provider cannot silently weaken an existing check —
    the weaker classes are opt-in and each one is written down.
    """
    by_model = _EFFORT_OBSERVABILITY.get(provider or "")
    if not by_model:
        return EFFORT_OBSERVABLE
    if model in by_model:
        return by_model[model]
    # A provider-wide declaration, recorded under ``None``.
    return by_model.get(None, EFFORT_OBSERVABLE)


#: The one lawful crossing from the recovery-capability short names to the
#: wire keys, written down once because the two namespaces are deliberately
#: not merged and a caller guessing at the crossing gets no error — only a
#: wrong answer.
#:
#: ``effort_observability`` is keyed by wire name, but the recovery receipt
#: surface is closed over the short names, so asking it ``"claude"`` finds
#: no declaration and silently returns the default ``observable``. That is
#: the worst possible failure: the call *looks* like it consults the
#: authority while actually restoring the strict equality the declaration
#: exists to relax, and nothing raises to say so.
_WIRE_FOR_RECOVERY_NAME = {
    PROVIDER_CODEX: PROVIDER_CODEX_WIRE,
    PROVIDER_KIMI: PROVIDER_KIMI_CLI,
    PROVIDER_CLAUDE: PROVIDER_CLAUDE_CODE,
}


def effort_observability_for_recovery_provider(
    provider: Optional[str], model: Optional[str]
) -> str:
    """``effort_observability`` for a *recovery-capability* short name.

    The recovery receipt surface speaks ``codex|kimi|claude``; the
    observability declarations are keyed by the wire names. This is the
    only supported way to ask the observability question from that side,
    so the translation is not repeated at call sites where a silent miss
    would read as a real ``observable`` answer.

    An unknown short name raises rather than defaulting: the caller has
    already validated its provider against ``PROVIDERS``, so a miss here
    means the two namespaces have drifted and the safe answer is not a
    guess.
    """
    wire = _WIRE_FOR_RECOVERY_NAME.get(provider or "")
    if wire is None:
        raise ProviderContractError(
            f"unknown recovery-capability provider: {provider!r}; expected one "
            f"of {list(_WIRE_FOR_RECOVERY_NAME)}"
        )
    return effort_observability(wire, model)


def effort_receipt_matches(
    expected: Optional[str],
    observed: Optional[str],
    *,
    observability: str = EFFORT_OBSERVABLE,
) -> bool:
    """Whether a receipt's observed effort is the one this route asked for.

    The comparison lives here, with the rest of the effort vocabulary,
    because the route and a truthful receipt speak different alphabets and
    only this module knows both. A raw string equality between them
    refuses every provider-default launch: the route says
    ``provider-default`` and an honest native receipt says ``None``,
    because that session was never told an effort and never checked one.

    By declared observability:

    * ``observable`` — ``observed == expected``, unchanged. This is where
      Codex and K3 live and nothing about them moves.
    * ``none`` — ``observed`` must be null. A non-null value is a genuine
      mismatch: it means the session settled on an effort nobody selected,
      which is the drift the exact-route check exists to catch.
    * ``unobserved-pre-turn`` — ``observed`` must be null too, but for the
      opposite reason: an effort *was* requested and simply cannot be seen
      yet. A non-null value here is a claim nothing could have produced,
      so it is refused rather than accepted as a bonus.

    Null is never universally acceptable. Treating it as always-matching
    would unblock the unobservable pairs by making every route's effort
    unverifiable, including the ones where an effort really is read back.
    """
    if observability in (EFFORT_OBSERVABILITY_NONE, EFFORT_UNOBSERVED_PRE_TURN):
        return observed is None
    if route_selects_effort(expected):
        return observed == expected
    return observed is None


def kimi_effort_env(effort: Optional[str]) -> dict:
    """The Kimi effort environment for a route — empty when it selects none.

    Empty rather than absent-keyed-to-a-default: the override is *omitted*,
    never translated into some other value, so the provider applies its own
    default and nothing here claims to know what that is.
    """
    return {"KIMI_MODEL_THINKING_EFFORT": effort} if route_selects_effort(effort) else {}


#: The provider's own deterministic updater kill-switch, as an environment
#: fragment for every managed Kimi process.
#:
#: Kimi Code's supported background updater replaced the installed binary
#: mid-campaign four times running (0.30.0, 0.31.0, 0.32.0, and 0.33.0 —
#: the last *during* the 0.32.0 stage verification, two seconds after an
#: interactive stage pane made the device rollout-eligible).  Every CLI
#: entry point runs the update preflight, and its first check is this
#: variable (bundle-read from the installed 0.33.0:
#: ``isAutoUpdateDisabledByEnv`` returns before any update check,
#: background install, or pre-boot install prompt — the prompt alone would
#: stall a managed pane until the render-convergence deadline froze it,
#: and the config-file knob does not suppress it).  Live-verified across
#: ``--version``, a zero-prompt ACP session, and an interactive TUI boot:
#: zero updater-state writes.
#:
#: This is a per-process atomicity fence, nothing more: it makes the one
#: Kimi process a managed launch selected — its preflight probe, its
#: bootstrap, its bridge child, its resumed TUI — immutable for the life
#: of that process, so the bytes that were attested are the bytes that run
#: and the bytes that keep running.  It is scoped to CAO-managed child
#: environments and changes nothing outside them: the operator's PATH
#: installation stays free to update on its own schedule, and nothing here
#: freezes, manages, or replaces it.  Pinned by the reservation rather than
#: inherited from the ambient shell, so an operator-supplied conflicting
#: value cannot re-enable the updater inside a managed child.  The fence
#: stays useful when a content-addressed runtime vault exists: the vault
#: chooses which bytes run, and this keeps a chosen process self-identical
#: until it exits.
def kimi_update_suppression_env() -> dict:
    """A fresh ``{"KIMI_CODE_NO_AUTO_UPDATE": "1"}`` per call."""
    return {"KIMI_CODE_NO_AUTO_UPDATE": "1"}


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

    ``"codex 0.146.0"``, ``"2.1.220 (Claude Code)"``, ``"kimi 0.29.1"`` all
    yield the bare version; an absent one yields ``""``.  Exposed so a
    caller can record the *actual* validated version rather than a pin
    constant that may name the wrong one of several accepted builds.
    """
    for token in installed_version.strip().split():
        if re.fullmatch(r"\d+\.\d+\.\d+", token):
            return token
    return ""


def check_pinned_version(provider: str, installed_version: str) -> None:
    """Fail closed unless the installed version satisfies the provider policy.

    In open mode any provider accepts a non-empty semver-shaped observed
    version at the launch identity boundary.  In strict mode the version
    must be an exact member of ``SUPPORTED_VERSIONS`` — the quarantine set.
    Unknown providers and unparseable versions always fail closed.
    """
    if provider not in SUPPORTED_VERSIONS:
        raise ProviderContractError(f"unknown provider: {provider!r}")
    normalized = normalized_version(installed_version)
    if not normalized:
        raise ProviderVersionDrift(
            f"{provider} version unparseable: {installed_version.strip()!r}; "
            "resume refuses (41) until a stage-verified pinned binary is installed"
        )
    if version_enforcement_mode(provider) == VERSION_ENFORCEMENT_OPEN:
        return
    accepted = SUPPORTED_VERSIONS[provider]
    if normalized not in accepted:
        raise ProviderVersionDrift(
            f"{provider} version drift: accepted {list(accepted)}, installed "
            f"{installed_version.strip()!r}; resume refuses (41) until a "
            "stage-verified pinned binary is installed"
        )


def is_listed_version(provider: str, installed_version: str | None) -> bool:
    """Return whether an exact build is a member of ``SUPPORTED_VERSIONS``.

    This is strict-mode quarantine membership, **not** a capability
    authority.  Under the unpinned provider-version policy an unlisted build
    receives full capability through the per-surface conservative defaults
    and runtime bundle reads; this predicate survives only for the surfaces
    that still name exact builds (the §6 narrow tables' reference cells and
    the strict-mode launch quarantine in :func:`check_pinned_version`).
    It intentionally ignores the launch enforcement mode.
    """
    if provider not in SUPPORTED_VERSIONS or not isinstance(installed_version, str):
        return False
    normalized = normalized_version(installed_version)
    return bool(normalized and normalized in SUPPORTED_VERSIONS[provider])


#: The exact builds stage-proven for the *native bind/admission* contract:
#: the pre-turn native identity a managed generation binds against — the
#: zero-turn bootstrap that mints the id, the exact resume that adopts
#: it, and the input-ready observation the readiness receipt carries.
#:
#: This is a capability table in its own right, deliberately NOT a view
#: of ``SUPPORTED_VERSIONS``.  The two answer different questions and
#: may legitimately disagree in either direction: Codex 0.147.0 holds
#: the narrow native-bind proof (its zero-turn app-server
#: bootstrap/resume exchange was stage-verified on the installed build)
#: while the broad advanced surfaces — force-pause, rendered identity,
#: resume and route authority — remain unproven, so the broad table still
#: lists only 0.146.0 and those gates keep refusing 0.147.0 independently.
#: Its multiline composer is separately proven in the build-exact
#: ``codex_native_control._PROVEN_COMPOSER_NEWLINE`` table; that narrower
#: fact does not widen the broad table.
#:
#: Codex's cell is the same fact as
#: ``codex_native_bootstrap.BOOTSTRAP_CAPABLE_VERSIONS``; the canonical
#: literal lives here so the mint that produces a native id and the bind
#: seam that accepts it cannot drift into the state where one accepts a
#: build the other refuses — which is exactly the reproduced failure
#: this table exists to close.
#:
#: Kimi, Claude, and Muse have no separate narrow proof: their native
#: identity paths (ACP ``session/new``, the ``--session-id`` mint, the
#: ``/status`` discovery) were verified as part of each accepted build's
#: broad stage verification, so their cells are the broad tuples by
#: reference.  A future build that must be bind-gated more narrowly than
#: the broad table is written here as its own literal tuple, never by
#: weakening the broad one.
NATIVE_BIND_CAPABLE_VERSIONS: dict[str, tuple[str, ...]] = {
    PROVIDER_CODEX: ("0.146.0", "0.147.0"),
    PROVIDER_KIMI: SUPPORTED_VERSIONS[PROVIDER_KIMI],
    PROVIDER_CLAUDE: SUPPORTED_VERSIONS[PROVIDER_CLAUDE],
    PROVIDER_MUSE: SUPPORTED_VERSIONS[PROVIDER_MUSE],
}
# The current pin must always be native-bind capable — asserted for the
# same reason the pin/SUPPORTED_VERSIONS agreement above is: the two maps
# must not be able to silently drift apart.
assert all(
    PINNED_VERSIONS[provider] in versions
    for provider, versions in NATIVE_BIND_CAPABLE_VERSIONS.items()
)


def is_native_bind_capable(provider: str, installed_version: str | None) -> bool:
    """Return whether an exact build is proven for the native bind seam.

    The acceptance authority for managed native bind/admission: a readiness
    receipt from a build outside this table cannot become a generation's
    bound native identity, whatever the launch enforcement mode admits.
    Independent of :func:`is_listed_version` in both directions — a build
    may hold this narrow proof without the broad one (Codex 0.147.0) — so
    neither predicate may stand in for the other at a call site.
    """
    if provider not in NATIVE_BIND_CAPABLE_VERSIONS or not isinstance(installed_version, str):
        return False
    normalized = normalized_version(installed_version)
    return bool(normalized and normalized in NATIVE_BIND_CAPABLE_VERSIONS[provider])


#: The exact builds proven for the zero-task trust/route-attestation
#: contract: the app-server ``initialize -> config/read(includeLayers) ->
#: thread/start(ephemeral)`` exchange with NO ``turn/*`` that the route
#: attestor runs to prove a failed route is healthy, plus the exact trust
#: root (`config/read` resolving the canonical project as ``trusted`` from
#: ``sessionFlags`` provenance) and a byte-identical protected user config
#: afterward.
#:
#: This is a capability table in its own right, deliberately NOT a view of
#: ``SUPPORTED_VERSIONS``, for the same reason the native-bind table is:
#: the two answer different questions and may disagree.  Codex 0.147.0
#: holds the narrow zero-task attestation proof — its app-server exchange
#: and trust/provenance surface are the same zero-turn mechanics the
#: native-bind seam stage-verified on the installed build, and a live
#: 0.147.0 install reached this seam refusing only on the stale exact
#: banner gate — while the broad advanced surfaces remain unproven, so
#: ``SUPPORTED_VERSIONS`` still lists only 0.146.0 and native
#: control/resume/route authority keep refusing 0.147.0 independently.
#:
#: Kimi, Claude, and Muse have no separate narrow proof: their route
#: probes were verified as part of each accepted build's broad stage
#: verification, so their cells are the broad tuples by reference.  A
#: future build that must be attestation-gated more narrowly than the
#: broad table is written here as its own literal tuple, never by
#: weakening the broad one.
ROUTE_ATTEST_CAPABLE_VERSIONS: dict[str, tuple[str, ...]] = {
    PROVIDER_CODEX: ("0.146.0", "0.147.0"),
    PROVIDER_KIMI: SUPPORTED_VERSIONS[PROVIDER_KIMI],
    PROVIDER_CLAUDE: SUPPORTED_VERSIONS[PROVIDER_CLAUDE],
    PROVIDER_MUSE: SUPPORTED_VERSIONS[PROVIDER_MUSE],
}
# The current pin must always be route-attestation capable — asserted for
# the same reason the pin/SUPPORTED_VERSIONS agreement above is: the two
# maps must not be able to silently drift apart.
assert all(
    PINNED_VERSIONS[provider] in versions
    for provider, versions in ROUTE_ATTEST_CAPABLE_VERSIONS.items()
)


def is_route_attest_capable(provider: str, installed_version: str | None) -> bool:
    """Return whether an exact build is proven for zero-task route attestation.

    The acceptance authority for the route-attestation seam: a receipt
    from a build outside this table cannot re-arm a launch breaker,
    whatever the launch enforcement mode admits.  Independent of
    :func:`is_listed_version` in both directions — a build may hold this
    narrow proof without the broad one (Codex 0.147.0) — so neither
    predicate may stand in for the other at a call site.
    """
    if provider not in ROUTE_ATTEST_CAPABLE_VERSIONS or not isinstance(installed_version, str):
        return False
    normalized = normalized_version(installed_version)
    return bool(normalized and normalized in ROUTE_ATTEST_CAPABLE_VERSIONS[provider])


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
      kimi:   ``--session <id>`` · ``-S <id>`` · ``-r <id>``
      claude: ``--resume <uuid>``
      muse:   ``muse resume <id>``
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
        # ``--session`` is the golden launch form; ``-S`` is its documented
        # short form; ``-r`` is the bundle-verified hidden compatibility
        # alias (registered with hideHelp() in 0.29.1 and 0.29.2 — absence
        # from human-facing --help is not evidence of invalidity).
        if len(args) == 2 and args[0] in ("--session", "-S", "-r") and args[1]:
            return ResumeForm(provider, tuple(args), args[1])
        raise ResumeFormRefused(
            "kimi resume accepts exactly `--session <id>`, `-S <id>`, or `-r <id>`"
        )
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
    if provider == PROVIDER_MUSE:
        # The installed interactive lifecycle is ``muse resume <id>``: the
        # TUI binds the exact caller-chosen id (verified on 0.1.0-R708.1).
        if len(args) == 2 and args[0] == "resume" and args[1] and not args[1].startswith("-"):
            native_id = args[1]
            try:
                parsed = _uuid_module.UUID(native_id)
            except ValueError as exc:
                raise ResumeFormRefused(
                    "muse resume native id must be a canonical UUID; " f"got {native_id!r}"
                ) from exc
            if str(parsed) != native_id:
                raise ResumeFormRefused("muse resume native id must be a canonical lowercase UUID")
            return ResumeForm(provider, tuple(args), native_id)
        raise ResumeFormRefused("muse resume accepts exactly `muse resume <id>`")
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


def _version_observed(provider: str, installed_version: Optional[str]) -> bool:
    """True only when the installed version was successfully observed.

    Resume identity is decided from the exact-id resume forms and their
    runtime observations, not from build listing: an unlisted build resumes
    through the same validated ``--session <id>`` / ``--resume <uuid>``
    forms, and a build whose behaviour changed fails loudly at the
    observation seam.  What remains fail-closed here is the *failed
    observation* — an absent or unparseable banner — which is a different
    answer from "nothing was written down about this build".
    """
    if installed_version is None or provider not in SUPPORTED_VERSIONS:
        return False
    return bool(normalized_version(installed_version))


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
    ``--version`` observation (a failed observation — absent or unparseable
    — removes the capability; an *unlisted* build is merely nothing written
    down and keeps it), ``kimi_acp_proof`` is the validated durable ACP
    new→kill→load receipt, and ``route_proof`` is the provider-specific
    model-input-bound non-echo route receipt, validated against the
    pinned route expectation supplied by the authority boundary
    (``expected_model``/``expected_effort``/``expected_model_input_digest``).
    With PF-2 red and no receipts, every provider reports
    unproven/unsupported — caller booleans can no longer promote anything.
    """
    if provider == PROVIDER_CODEX:
        version_ok = _version_observed(PROVIDER_CODEX, installed_version)
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
                else "identity unavailable: the installed Codex binary's version "
                "could not be observed (fail closed, outcome 41)"
            ),
        )
    if provider == PROVIDER_CLAUDE:
        version_ok = _version_observed(PROVIDER_CLAUDE, installed_version)
        return ProviderResumeStatus(
            provider=provider,
            identity_available=version_ok,
            authority_supported=False,
            reason=(
                "resume identity available (--session-id/--resume); unsupported "
                "by default: no pre-input effort surface exists"
                if version_ok
                else "identity unavailable: the installed Claude binary's version "
                "could not be observed (fail closed, outcome 41)"
            ),
        )
    if provider == PROVIDER_KIMI:
        version_ok = _version_observed(PROVIDER_KIMI, installed_version)
        proven = version_ok and kimi_acp_proof is not None
        return ProviderResumeStatus(
            provider=provider,
            identity_available=proven,
            authority_supported=False,
            reason=(
                "identity requires the installed-CLI ACP session/new→kill→"
                "session/load proof; effort authority unproven"
                if proven
                else "identity disabled until the installed binary's version is "
                "observed and the installed-CLI ACP "
                "session/new→kill→session/load proof passes"
            ),
        )
    raise ProviderContractError(f"unknown provider: {provider!r}")
