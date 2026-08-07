"""Which workers would come back, answered by asking rather than by table.

Stopping a session collects every pane. The spec's rule is that
proceeding is allowed and proceeding *unknowingly* is not — "a button
labelled hibernate must never silently be a one-way door" — so the
confirmation has to name every worker that will not return.

**The list is computed, never looked up.** That is the whole point of this
module, and it exists because the obvious implementation is wrong in four
separate ways that all fail in the same direction: silently understating
the loss.

The design document's own provider table says ``claude``, ``codex``,
``kimi``, ``muse`` and ``glm`` are resumable. Against the code:

- **muse has no resume path at all.** ``muse_native_launch`` defines
  ``build_launch_argv`` and no ``build_resume_argv``, and the native argv
  binder ignores ``launch_kind`` entirely — "both launch and recovery
  therefore re-run the exec command bound to the minted session id".
- **``glm`` is not a provider.** It is ``claude_code`` carrying
  ``provider_route="glm"``, and its route envelope holds a *one-shot*
  consumed marker, so a GLM resume is a Claude resume plus a re-minted
  envelope.
- **A provider with a resume path is useless without a recorded session
  id.** Only the v2 launch path writes one. Every v1 row — which on the
  reference fleet is every supervisor — has nothing to resume against.
- **A drifted binary breaks resume even for a supported provider**, because
  the resume argv is pinned per version.

Any one of those, applied to a real fleet, turns "0 workers will not come
back" into a dialog that loses thirteen of them.

So a worker is reported resumable only when every clause holds, and each
refusal carries its own reason — an operator deciding whether to stop
needs to know *why* a worker is a one-way door, because "wrong provider"
and "we never recorded its id" have different remedies.

Note what this module does not claim. It reports whether a worker *could*
be resumed if a resume path existed. **No resume path exists in production
for any provider today**: the one route named ``resume`` refuses on both
branches, and every native launch mints a fresh session. So today every
clause-passing worker is still, in practice, not coming back — which is
why :func:`stop_impact` reports the machinery gap as its own field rather
than folding it into the per-worker verdict. Conflating "this worker is
structurally resumable" with "resume works" would leave a silent lie in
place the day resume lands.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Why one worker cannot be brought back.
REASON_PROVIDER_UNSUPPORTED = "provider-has-no-resume-path"
REASON_NO_RECORDED_SESSION = "no-recorded-native-session-id"
REASON_VERSION_DRIFT = "provider-version-drifted-from-pin"
REASON_CLAIM_FROZEN = "native-session-claim-frozen"
REASON_UNKNOWN_PROVIDER = "provider-not-recognised"
#: The pane could not be observed at all. Counted as a loss rather than
#: skipped: "not live" and "we could not look" are different facts, and
#: only the first means the worker is already gone.
REASON_LIVENESS_UNKNOWN = "liveness-could-not-be-observed"
#: Distinct from drift. Nothing in production supplies the installed
#: versions today, so reporting drift would accuse every worker of a
#: mismatch against a check nobody ran — and an operator who fixes the
#: "drift" would find the verdict unchanged.
REASON_VERSION_UNVERIFIED = "provider-version-not-verified"

#: True while no provider has a working resume path in production. Read
#: from the code rather than asserted: the route exists and refuses.
RESUME_MACHINERY_ABSENT_REASON = (
    "no provider has a working resume path yet — the resume route refuses on every branch "
    "and every native launch mints a fresh session, so stopping is currently one-way for "
    "every worker regardless of the per-worker verdict below"
)


def _supported_providers() -> frozenset[str]:
    """The providers the native launch path can actually bind argv for.

    Derived from ``managed_launch_v2`` rather than restated, because that
    set is itself an intersection of four maps — argv binder, readiness
    receipt, issuance source, pinned version — and a copy here would drift
    the first time one of them changed.
    """
    from cli_agent_orchestrator.services import managed_launch_v2

    return managed_launch_v2.NATIVE_TUI_PROVIDERS


#: Terminals carry `ProviderType` values; `provider_contracts` pins
#: versions under its own shorter names. They are not the same strings and
#: nothing translates between them, so a check written against the wrong
#: vocabulary silently passes for every provider.
_CONTRACT_NAME = {"codex": "codex", "claude_code": "claude", "kimi_cli": "kimi"}


def _version_is_pinned(provider: str, installed_version: Optional[str]) -> tuple[bool, str]:
    """Whether the installed binary matches the pin the resume argv assumes.

    ``installed_version`` is supplied by the caller rather than probed
    here, matching how ``provider_contracts.resume_status`` is fed
    everywhere else — the probe is a subprocess per provider and belongs
    to whoever is already paying for one.

    An *absent* version is reported as unverified rather than as pinned.
    That is the conservative direction, and it is the only honest one: the
    resume argv is pinned per version, so "we did not check" and "it
    matches" cannot share an answer without the confirmation dialog
    understating what an operator is about to lose.
    """
    from cli_agent_orchestrator.services import provider_contracts

    contract_name = _CONTRACT_NAME.get(provider)
    if contract_name is None:
        return False, f"no version pin is defined for {provider}"
    if not installed_version:
        return False, "installed version not supplied, so the pin could not be checked"
    try:
        provider_contracts.check_pinned_version(contract_name, installed_version)
    except Exception as exc:  # noqa: BLE001 - drift, or an unreadable pin
        return False, str(exc)
    return True, installed_version


def worker_resumability(
    row: dict[str, Any], *, installed_versions: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    """Answer for one worker, with the reason it is a one-way door.

    ``row`` is a terminal projection row: it carries ``provider`` and,
    for v2 rows only, ``native_session_id``. ``installed_versions`` maps
    a `ProviderType` to the live ``--version`` string; absent means the
    pin could not be checked, which is reported rather than assumed.
    """
    terminal_id = row.get("terminal_id") or row.get("id")
    provider = row.get("provider")
    verdict: dict[str, Any] = {
        "terminal_id": terminal_id,
        "provider": provider,
        "agent_profile": row.get("agent_profile"),
        "native_session_id": row.get("native_session_id"),
        "resumable": False,
        "reason": None,
    }

    if not isinstance(provider, str) or not provider:
        verdict["reason"] = REASON_UNKNOWN_PROVIDER
        return verdict

    try:
        supported = _supported_providers()
    except Exception as exc:  # noqa: BLE001 - unknown support is not support
        logger.warning("Could not read the native-TUI provider set: %s", exc)
        verdict["reason"] = REASON_PROVIDER_UNSUPPORTED
        verdict["detail"] = str(exc)
        return verdict

    if provider not in supported:
        verdict["reason"] = REASON_PROVIDER_UNSUPPORTED
        verdict["detail"] = f"{provider} is not in {sorted(supported)}"
        return verdict

    if not row.get("native_session_id"):
        # The common case on a real fleet, and the one a provider table
        # cannot see: the provider supports resume, and this particular
        # worker was launched by a path that never recorded an id.
        verdict["reason"] = REASON_NO_RECORDED_SESSION
        return verdict

    supplied = (installed_versions or {}).get(provider)
    pinned, detail = _version_is_pinned(provider, supplied)
    if not pinned:
        verdict["reason"] = REASON_VERSION_DRIFT if supplied else REASON_VERSION_UNVERIFIED
        verdict["detail"] = detail
        return verdict

    frozen = _claim_is_frozen(provider, str(row["native_session_id"]))
    if frozen:
        verdict["reason"] = REASON_CLAIM_FROZEN
        verdict["detail"] = frozen
        return verdict

    verdict["resumable"] = True
    verdict["installed_version"] = detail
    return verdict


def _claim_is_frozen(provider: str, native_session_id: str) -> Optional[str]:
    """A frozen claim can never be re-declared, so its worker cannot return.

    Checked here rather than at resume time because the operator deciding
    whether to stop is the one who needs to know. A frozen claim is
    resolvable — ``cao attachment adjudicate`` exists — but only by a human
    who knows to look.
    """
    from cli_agent_orchestrator.services import native_attachment

    try:
        record = native_attachment.get(provider, native_session_id)
    except Exception as exc:  # noqa: BLE001 - unknown is not clear
        return f"claim unreadable: {exc}"
    if record is None:
        return None
    if record["state"] == native_attachment.AMBIGUOUS:
        return (
            f"the claim on this session is frozen ({record.get('ambiguity_reason')}); "
            "it needs `cao attachment adjudicate` before anything can re-acquire it"
        )
    return None


def stop_impact(
    session_name: str, *, installed_versions: Optional[dict[str, str]] = None
) -> dict[str, Any]:
    """What stopping this session would cost, in the operator's terms.

    **Normalises the session name**, because terminal rows carry the
    prefixed form and the write path normalises too. Without it a bare
    name reads zero workers here and the stop that follows lands on the
    real session: the dialog says nothing will be lost, the operator
    agrees, and a fleet of live workers is silenced to the marshal on the
    strength of it.

    A **dead** pane is excluded — already gone, and naming it would
    inflate the loss an operator is being asked to accept, which trains
    them to click through the dialog.

    A pane whose liveness could **not be observed** is included as a loss,
    with its own reason. That is the opposite call from ``dead``, for the
    reason this module exists: "not live" and "we could not look" are
    different facts. A tmux server that cannot be read makes *every* row
    unobservable — and that is exactly when somebody reaches for stop — so
    reporting "0 workers" there would be this module's own failure mode,
    reached through its own filter.
    """
    from cli_agent_orchestrator.services import session_lifecycle, terminal_projection

    session_name = session_lifecycle.normalise_session_name(session_name)
    try:
        rows = terminal_projection.project_session(session_name)
    except Exception as exc:  # noqa: BLE001
        return {
            "session_name": session_name,
            "unreadable": str(exc),
            "live_workers": 0,
            "resumable": [],
            "not_resumable": [],
            "resume_machinery_available": False,
            "resume_machinery_reason": RESUME_MACHINERY_ABSENT_REASON,
        }

    verdicts: list[dict[str, Any]] = []
    for row in rows:
        state = row.get("lifecycle_state")
        if state in terminal_projection.ATTACHABLE_LIFECYCLE_STATES:
            verdicts.append(worker_resumability(row, installed_versions=installed_versions))
        elif state not in (
            terminal_projection.LIFECYCLE_DEAD,
            terminal_projection.LIFECYCLE_SUPERSEDED,
        ):
            verdicts.append(
                {
                    "terminal_id": row.get("terminal_id") or row.get("id"),
                    "provider": row.get("provider"),
                    "agent_profile": row.get("agent_profile"),
                    "native_session_id": row.get("native_session_id"),
                    "resumable": False,
                    "reason": REASON_LIVENESS_UNKNOWN,
                    "detail": row.get("lifecycle_reason") or state,
                }
            )
    not_resumable = [v for v in verdicts if not v["resumable"]]
    return {
        "session_name": session_name,
        "live_workers": len(verdicts),
        "resumable": [v for v in verdicts if v["resumable"]],
        "not_resumable": not_resumable,
        # Reported separately from the per-worker verdicts on purpose. A
        # worker can be structurally resumable while nothing in the system
        # can resume it, and folding the two together would leave a lie in
        # place the day the machinery lands.
        "resume_machinery_available": False,
        "resume_machinery_reason": RESUME_MACHINERY_ABSENT_REASON,
        "one_way_for_every_worker": True,
    }
