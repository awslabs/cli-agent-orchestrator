"""Recovery capability surface (``GET /managed/recovery-capabilities``).

The capability payload is the single truthful negotiation surface for
the recovery control plane: every consumer (conductor preflights,
doctor, deployed-pairing checks) reads it and fails closed on absence
or unknown fields.

Invariant: with zero proven providers and no authorized containment
artifact, the surface advertises exactly that — ``containment:
unproven`` and per-provider observed-route ``unsupported``/``unproven``
— and every dependent path stays preserved/alert-only.  Provider echo,
manifest requests, TUI/footer state, logs, and client-local
configuration are never reported as provider-observed proof.

Failure mode prevented: an over-claiming capability surface would let a
deployed pairing silently treat the alert-only foundation as the full
plane, re-admitting the exact false-recovery behaviors this design
keeps fail-closed.
"""

from __future__ import annotations

from typing import Any, Optional

from cli_agent_orchestrator.constants import (
    INBOX_RECONCILE_GRACE_SECONDS,
    INBOX_RECONCILE_INTERVAL,
)
from cli_agent_orchestrator.services import actor_broker, provider_contracts
from cli_agent_orchestrator.services.containment import ContainmentComposition
from cli_agent_orchestrator.services.resource_registry import REGISTRY_SCHEMA_VERSION

CAPABILITY_SCHEMA_VERSION = 1
CAPABILITY_PROTOCOL = "cao-recovery-capabilities-v1"

RECEIPT_SCHEMAS = [
    "cao-w13-fence-receipt-v1",
    "cao-actor-assertion-v1",
    "cao-receipt-v1",
    "cao-route-segment-v1",
    "cao-destructive-receipt-v1",
    "cao-containment-proof-v1",
]


def callback_recovery_admission_allowed(expected_provider: str) -> bool:
    """Recompute the exact live authority intersection for a start request."""
    callback = build_capabilities()["callback_recovery"]
    return bool(
        callback["enabled"]
        and callback["pending_sweep"]["enabled"]
        and expected_provider in callback["providers"]
    )


def build_capabilities(
    *,
    containment: Optional[ContainmentComposition] = None,
    provider_versions: Optional[dict[str, Optional[str]]] = None,
    kimi_acp_proof: Optional[dict[str, Any]] = None,
    route_proofs: Optional[dict[str, dict[str, Any]]] = None,
    route_expectations: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Assemble the capability payload from live composition state.

    Every authority claim derives from provider-specific, version-checked,
    generation-bound receipts — never from configuration or caller
    assertion.  ``provider_versions`` maps provider → live ``--version``
    output (None = binary absent/unverified, which removes the
    capability); ``kimi_acp_proof`` is the validated durable ACP
    new→kill→load receipt; ``route_proofs`` maps provider → validated
    model-input-bound non-echo route receipt, and ``route_expectations``
    maps provider → the authority boundary's pinned route
    (``model``/``effort``/``model_input_digest``) each receipt is
    validated against.  With zero receipts the surface advertises exactly
    that: unproven containment, no observed routes, no enabled providers,
    and no automated recovery/finalization/destructive path.
    """
    composition = containment or ContainmentComposition()
    containment_status = composition.status()
    versions = provider_versions or {}
    proofs = route_proofs or {}
    expectations = route_expectations or {}

    def _expectation_kwargs(provider: str) -> dict[str, Any]:
        expectation = expectations.get(provider) or {}
        return {
            "expected_model": expectation.get("model"),
            "expected_effort": expectation.get("effort"),
            "expected_model_input_digest": expectation.get("model_input_digest"),
        }

    def _route_valid(provider: str) -> bool:
        return provider_contracts.validate_route_proof(
            provider, proofs.get(provider), **_expectation_kwargs(provider)
        )

    codex = provider_contracts.resume_status(
        "codex",
        installed_version=versions.get("codex"),
        route_proof=proofs.get("codex"),
        **_expectation_kwargs("codex"),
    )
    claude = provider_contracts.resume_status(
        "claude",
        installed_version=versions.get("claude"),
        route_proof=proofs.get("claude"),
        **_expectation_kwargs("claude"),
    )
    kimi = provider_contracts.resume_status(
        "kimi",
        installed_version=versions.get("kimi"),
        kimi_acp_proof=kimi_acp_proof,
        route_proof=proofs.get("kimi"),
        **_expectation_kwargs("kimi"),
    )
    observed_route = {
        # PF-2 is red for every pinned provider unless a provider-specific
        # route receipt is validated against the authority boundary's
        # pinned route: none emits a model-input-bound non-echo receipt
        # carrying resolved model and effective effort by default.
        # Truthiness is not proof — only the validated cao-route-receipt-v1
        # for this exact provider counts.
        "codex": "proven" if _route_valid("codex") else "unsupported",
        "claude": "proven" if _route_valid("claude") else "unsupported",
        "kimi": "proven" if _route_valid("kimi") else "unproven",
    }
    enabled_providers = [
        # Route authority, not identity: a provider is enabled only when
        # its authority_supported bit is set, which requires the pinned
        # version AND a validated provider-specific observed-route proof.
        # Identity availability alone enables nothing automated.
        provider
        for provider, status in (("codex", codex), ("claude", claude), ("kimi", kimi))
        if status.authority_supported
    ]
    zero_proven = not enabled_providers or containment_status != "proven"
    from cli_agent_orchestrator.services import callback_recovery

    # The lifecycle API uses ``kimi_cli`` on the wire, while the authority
    # surface calls the same provider ``kimi``.  Never advertise a callback
    # provider outside the proven outer authority intersection.
    callback_providers = [
        wire_name
        for provider, wire_name in (("codex", "codex"), ("kimi", "kimi_cli"))
        if provider in enabled_providers
    ]

    return {
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "protocol": CAPABILITY_PROTOCOL,
        "heartbeat": {
            "schema_version": 2,
            "producer": "managed-bridge-or-hook-receiver",
            "fencing": "producer-token",
            "coalesce_seconds": 20,
            "max_lease_ttl_s": 300,
        },
        "fence": {
            "request_schema": "cao-w13-fence-req-v1",
            "response_schema": "cao-w13-fence-resp-v1",
            "single_use_intent": True,
        },
        "actor_broker": {
            "assertion_schema": "cao-actor-assertion-v1",
            "platform_peer_identity": actor_broker.platform_supported(),
            # Issuance is wired to the generation-private UDS accept path
            # with kernel credentials/lineage in managed_provider_bridge.
            "production_wiring": "managed-provider-bridge",
        },
        "containment": containment_status,
        "observed_route": observed_route,
        "enabled_providers": enabled_providers,
        "automated_paths": {
            # Zero proven providers keeps every automated authority surface
            # unavailable — preserved/alert-only is the only honest state.
            "recovery": not zero_proven,
            "finalization": not zero_proven,
            "destructive": not zero_proven,
        },
        # This additive block is the only lifecycle-v2 negotiation surface.
        # It remains visible while disabled so callers can distinguish an old
        # fork from a deliberate safe rollout hold.
        "callback_recovery": {
            "lifecycle_version": 2,
            "enabled": callback_recovery.lifecycle_v2_enabled() and bool(callback_providers),
            "request_schema": callback_recovery.REQUEST_SCHEMA,
            "operation_schema": callback_recovery.OPERATION_SCHEMA,
            "callback_lookup_schema": callback_recovery.CALLBACK_LOOKUP_SCHEMA,
            "providers": callback_providers,
            "pending_sweep": {
                "enabled": True,
                "interval_seconds": INBOX_RECONCILE_INTERVAL,
                "grace_seconds": INBOX_RECONCILE_GRACE_SECONDS,
            },
        },
        "delivery_journal": {
            "schema_version": 1,
            "states": [
                "accepted",
                "terminal_queued",
                "submitted",
                "submit-acked",
                "submit-ambiguous",
                "consumer-acked",
            ],
            "at_most_once_honest": True,
            # Intent/submit/ack transitions are wired around the real
            # provider call in managed_provider_bridge.
            "production_wiring": "managed-provider-bridge",
        },
        "resource_registry_version": REGISTRY_SCHEMA_VERSION,
        "resume": {
            provider: {
                "identity_available": status.identity_available,
                "authority_supported": status.authority_supported,
                "reason": status.reason,
            }
            for provider, status in (("codex", codex), ("claude", claude), ("kimi", kimi))
        },
        "receipts": list(RECEIPT_SCHEMAS),
    }
