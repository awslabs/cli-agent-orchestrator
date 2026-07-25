"""The route receipt's effort agreement must come from the one effort judge.

Two different defects produce an identical-looking call site here, so both
are pinned:

1. A local ``observed_effort == assigned_effort`` — a second rule answering
   a question that already has an authority, and answering it worse.
2. Asking the authority with the wrong provider vocabulary. The receipt
   surface is closed over the recovery short names (``codex|kimi|claude``)
   while the observability declarations are keyed by wire name
   (``kimi_cli|claude_code``). A short name finds no declaration and
   silently returns the default ``observable``, which restores exactly the
   equality the declaration exists to relax — with no error to say so.

The second is the dangerous one: the code reads as though it consults the
judge, and every test that only exercises an ``observable`` pair passes.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import provider_contracts
from cli_agent_orchestrator.services import recovery_receipts as rr


def _observed(**overrides) -> dict:
    """A fully-observed route receipt: all five observed fields present."""
    kwargs = {
        "provider": "claude",
        "native_id": "3f2a8c1e-0b4d-4a7e-9c11-5d6e7f801234",
        "authority_status": "observed",
        "assigned_model": "claude-opus-5",
        "assigned_effort": "high",
        "assigned_policy_sha256": "7" * 64,
        "assigned_profile_sha256": "8" * 64,
        "assigned_config_sha256": "9" * 64,
        "requested_model": "claude-opus-5",
        "requested_effort": "high",
        "observed_model": "claude-opus-5",
        "observed_effort": "high",
        "protocol_version": "1",
        "event_sequence": 1,
        "native_turn_id": "turn-1",
        "attested_at": "2026-07-25T11:00:05Z",
    }
    kwargs.update(overrides)
    return kwargs


def test_the_crossing_to_the_wire_namespace_actually_changes_the_answer() -> None:
    """The two vocabularies disagree, so the crossing is load-bearing.

    If this ever stops holding, the tests below would pass against a call
    site that never crosses namespaces at all, and they would be worthless
    without failing.
    """
    assert (
        provider_contracts.effort_observability("claude", "claude-opus-5")
        == provider_contracts.EFFORT_OBSERVABLE
    )
    assert (
        provider_contracts.effort_observability_for_recovery_provider("claude", "claude-opus-5")
        == provider_contracts.EFFORT_UNOBSERVED_PRE_TURN
    )


def test_a_claude_route_may_not_claim_an_observed_effort() -> None:
    """No Claude model exposes a pre-turn effort reading.

    So a receipt naming one is a claim nothing could have produced. Under a
    raw ``==`` — or under the judge asked with the short name — the values
    are equal and this is accepted, which is how an invented observation
    would reach the segment chain wearing full authority.
    """
    with pytest.raises(rr.ReceiptFactError, match="observation == assignment"):
        rr.route_payload(**_observed())


def test_an_effortless_kimi_model_may_not_claim_an_observed_effort() -> None:
    """``kimi-code/kimi-for-coding`` has no effort surface at all."""
    with pytest.raises(rr.ReceiptFactError, match="observation == assignment"):
        rr.route_payload(
            **_observed(
                provider="kimi",
                native_id="session_bf43ec1e",
                assigned_model="kimi-code/kimi-for-coding",
                requested_model="kimi-code/kimi-for-coding",
                observed_model="kimi-code/kimi-for-coding",
                assigned_effort=provider_contracts.EFFORT_PROVIDER_DEFAULT,
                requested_effort=provider_contracts.EFFORT_PROVIDER_DEFAULT,
                observed_effort=provider_contracts.EFFORT_PROVIDER_DEFAULT,
            )
        )


def test_an_observable_pair_keeps_its_exact_equality() -> None:
    """Codex is undeclared, so nothing about its strict check moves.

    This is the half that must NOT change: routing through the judge is
    only safe if the default class still refuses a real disagreement.
    """
    rr.route_payload(
        **_observed(
            provider="codex",
            native_id="thr_0192a7b4",
            assigned_model="gpt-5.6-sol",
            requested_model="gpt-5.6-sol",
            observed_model="gpt-5.6-sol",
            assigned_effort="xhigh",
            requested_effort="xhigh",
            observed_effort="xhigh",
        )
    )
    with pytest.raises(rr.ReceiptFactError, match="observation == assignment"):
        rr.route_payload(
            **_observed(
                provider="codex",
                native_id="thr_0192a7b4",
                assigned_model="gpt-5.6-sol",
                requested_model="gpt-5.6-sol",
                observed_model="gpt-5.6-sol",
                assigned_effort="xhigh",
                requested_effort="xhigh",
                observed_effort="low",
            )
        )


def test_an_unknown_recovery_provider_raises_rather_than_defaulting() -> None:
    """A namespace miss must be loud, because its quiet answer is wrong."""
    with pytest.raises(provider_contracts.ProviderContractError, match="unknown recovery"):
        provider_contracts.effort_observability_for_recovery_provider(
            "claude_code", "claude-opus-5"
        )


class TestDriftIsTheComplementOfAgreement:
    """Both branches answer one question, so one judge must decide both.

    A raw ``!=`` in the drifted branch does not merely duplicate the
    agreement rule -- it disagrees with it. For a pair whose effort cannot
    be observed, a receipt echoing the assigned effort into the observed
    slot is a claim nothing could have produced: the agreement rule refuses
    it, while ``!=`` sees equal strings, calls it agreement, and refuses to
    record drift. The tuple then satisfies *neither* status, so a real
    drift cannot be recorded at all -- the one outcome a drift check exists
    to prevent.
    """

    def test_an_effortless_model_claiming_an_effort_is_drift(self) -> None:
        """The sentinel echoed into the observed slot. Nothing observed it."""
        rr.route_payload(
            **_observed(
                authority_status="drifted",
                provider="kimi",
                native_id="session_bf43ec1e",
                assigned_model="kimi-code/kimi-for-coding",
                requested_model="kimi-code/kimi-for-coding",
                observed_model="kimi-code/kimi-for-coding",
                assigned_effort=provider_contracts.EFFORT_PROVIDER_DEFAULT,
                requested_effort=provider_contracts.EFFORT_PROVIDER_DEFAULT,
                observed_effort=provider_contracts.EFFORT_PROVIDER_DEFAULT,
            )
        )

    def test_an_unobservable_claude_effort_is_drift(self) -> None:
        """No Claude model exposes a pre-turn effort reading."""
        rr.route_payload(**_observed(authority_status="drifted"))

    def test_an_observable_pair_still_needs_a_real_disagreement(self) -> None:
        """The half that must NOT change: agreement is not drift."""
        with pytest.raises(rr.ReceiptFactError, match="requires a mismatch"):
            rr.route_payload(
                **_observed(
                    authority_status="drifted",
                    provider="codex",
                    native_id="thr_0192a7b4",
                    assigned_model="gpt-5.6-sol",
                    requested_model="gpt-5.6-sol",
                    observed_model="gpt-5.6-sol",
                    assigned_effort="xhigh",
                    requested_effort="xhigh",
                    observed_effort="xhigh",
                )
            )

    def test_an_observable_effort_mismatch_is_still_drift(self) -> None:
        rr.route_payload(
            **_observed(
                authority_status="drifted",
                provider="codex",
                native_id="thr_0192a7b4",
                assigned_model="gpt-5.6-sol",
                requested_model="gpt-5.6-sol",
                observed_model="gpt-5.6-sol",
                assigned_effort="xhigh",
                requested_effort="xhigh",
                observed_effort="low",
            )
        )

    def test_a_model_mismatch_is_still_drift_whatever_the_effort_says(self) -> None:
        rr.route_payload(
            **_observed(
                authority_status="drifted",
                provider="codex",
                native_id="thr_0192a7b4",
                assigned_model="gpt-5.6-sol",
                requested_model="gpt-5.6-sol",
                observed_model="gpt-5.5",
                assigned_effort="xhigh",
                requested_effort="xhigh",
                observed_effort="xhigh",
            )
        )

    @pytest.mark.parametrize(
        "route",
        [
            pytest.param(
                {
                    "provider": "kimi",
                    "native_id": "session_bf43ec1e",
                    "assigned_model": "kimi-code/kimi-for-coding",
                    "requested_model": "kimi-code/kimi-for-coding",
                    "observed_model": "kimi-code/kimi-for-coding",
                    "assigned_effort": "provider-default",
                    "requested_effort": "provider-default",
                    "observed_effort": "provider-default",
                },
                id="effortless-model-claiming-an-effort",
            ),
            pytest.param({}, id="unobservable-claude-effort"),
            pytest.param(
                {
                    "provider": "codex",
                    "native_id": "thr_0192a7b4",
                    "assigned_model": "gpt-5.6-sol",
                    "requested_model": "gpt-5.6-sol",
                    "observed_model": "gpt-5.6-sol",
                    "assigned_effort": "xhigh",
                    "requested_effort": "xhigh",
                    "observed_effort": "xhigh",
                },
                id="observable-agreement",
            ),
            pytest.param(
                {
                    "provider": "codex",
                    "native_id": "thr_0192a7b4",
                    "assigned_model": "gpt-5.6-sol",
                    "requested_model": "gpt-5.6-sol",
                    "observed_model": "gpt-5.6-sol",
                    "assigned_effort": "xhigh",
                    "requested_effort": "xhigh",
                    "observed_effort": "low",
                },
                id="observable-effort-mismatch",
            ),
        ],
    )
    def test_exactly_one_status_accepts_any_fully_observed_route(self, route) -> None:
        """The property, not four examples of it.

        Every fully-observed tuple is either agreement or drift, never both
        and never neither. This is what makes the gap unreintroducible: any
        second rule in either branch breaks the complement for some tuple,
        and this fails on it without anyone having to think of that tuple.
        """
        accepted = []
        for status in ("observed", "drifted"):
            try:
                rr.route_payload(**_observed(**route, authority_status=status))
                accepted.append(status)
            except rr.ReceiptFactError:
                pass

        assert len(accepted) == 1, f"expected exactly one lawful status, got {accepted}"
