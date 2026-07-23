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


def build_capabilities(
    *,
    containment: Optional[ContainmentComposition] = None,
    kimi_acp_proof_green: bool = False,
    route_receipt_proven: bool = False,
) -> dict[str, Any]:
    """Assemble the capability payload from live composition state.

    Every authority claim derives from the composed objects themselves —
    never from configuration or caller assertion.
    """
    composition = containment or ContainmentComposition()
    containment_status = composition.status()
    codex = provider_contracts.resume_status("codex", route_receipt_proven=route_receipt_proven)
    claude = provider_contracts.resume_status("claude")
    kimi = provider_contracts.resume_status("kimi", kimi_acp_proof_green=kimi_acp_proof_green)
    observed_route = {
        # PF-2 is red for every pinned provider: none emits a
        # model-input-bound non-echo receipt carrying resolved model and
        # effective effort, so observed-route authority is unsupported.
        "codex": "proven" if route_receipt_proven else "unsupported",
        "claude": "proven" if route_receipt_proven else "unsupported",
        "kimi": "proven" if route_receipt_proven else "unproven",
    }
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
        },
        "containment": containment_status,
        "observed_route": observed_route,
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
