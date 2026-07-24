"""Containment production-composition interfaces (§9.3a).

The selected containment architecture is a macOS Endpoint Security
auth-client system extension plus a minimal signed manager daemon,
built and proven in the separately owned, human-authorized
``cao-containment-ext`` companion lane.  This module is the fork's
*production-composition interface* to that artifact: proof-receipt
verification, composition status, and generation-revocation hooks.

Invariant: ``proven`` is reported only against a live, digest-bound
proof receipt issued by the named proof signer recorded in the human
artifact authorization, binding the exact extension + manager content
digests and the current deployment generation.  No artifact has been
authorized; the default and current status is therefore always
``unproven``, and every containment-dependent path fails closed.

Failure mode prevented: admitting generations, finalizing reports, or
tearing down processes on the strength of "no denial observed" — an
absence-of-evidence default that a dead or never-installed extension
would satisfy while enforcing nothing.

Why this guard exists: Endpoint Security clients fail open on death, so
dependent paths must require positive, live, digest-bound proof; this
interface makes "unproven" the only representable state until the
companion lane's proof matrix supplies such receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

CONTAINMENT_PROOF_SCHEMA = "cao-containment-proof-v1"

STATUS_PROVEN = "proven"
STATUS_UNPROVEN = "unproven"


class ContainmentError(RuntimeError):
    """Base error for containment composition."""


class ContainmentUnproven(ContainmentError):
    """A containment-dependent path was requested while unproven."""


@dataclass(frozen=True)
class ArtifactAuthorization:
    """The human authorization of the containment artifact (§19.6a shape).

    This record can only originate from the human authorization decision
    in the companion lane; until it exists, no proof receipt can
    validate and every dependent lane stays closed.
    """

    repository: str
    extension_sha256: str
    manager_sha256: str
    proof_issuer: str
    authorized_at: str


def validate_proof_receipt(
    receipt: dict[str, Any],
    *,
    authorization: Optional[ArtifactAuthorization],
    deployment_generation: int,
    now: Optional[datetime] = None,
) -> None:
    """Verify one containment proof receipt against the authorization.

    A valid receipt binds the exact extension and manager digests, is
    issued by the named proof issuer, names the proof matrix it
    discharges, and is live (unexpired).  Anything else raises
    ``ContainmentUnproven`` — the fail-closed default.
    """
    if authorization is None:
        raise ContainmentUnproven(
            "no human containment artifact authorization exists; PF-1a cannot "
            "start and every dependent lane stays closed"
        )
    if receipt.get("schema") != CONTAINMENT_PROOF_SCHEMA:
        raise ContainmentUnproven("unknown containment proof receipt schema")
    if receipt.get("extension_sha256") != authorization.extension_sha256:
        raise ContainmentUnproven("proof receipt binds a different extension digest")
    if receipt.get("manager_sha256") != authorization.manager_sha256:
        raise ContainmentUnproven("proof receipt binds a different manager digest")
    if receipt.get("proof_issuer") != authorization.proof_issuer:
        raise ContainmentUnproven("proof receipt issuer is not the named proof signer")
    if receipt.get("deployment_generation") != deployment_generation:
        raise ContainmentUnproven("proof receipt names a stale deployment generation")
    if not receipt.get("proof_matrix_id"):
        raise ContainmentUnproven("proof receipt names no proof matrix")
    expires = receipt.get("expires_at")
    moment = now or datetime.now(timezone.utc)
    if not isinstance(expires, str):
        raise ContainmentUnproven("proof receipt carries no liveness expiry")
    try:
        expiry = datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContainmentUnproven("proof receipt expiry is not RFC3339") from exc
    if moment >= expiry:
        raise ContainmentUnproven(
            "proof receipt is not live; a dead extension yields unproven, "
            "refusal, and freeze of admitted generations"
        )


class ContainmentComposition:
    """The production composition of bridge + registry + policy + revocation.

    Constructed with whatever the deployment actually provides; with no
    authorized artifact (the current and default state) every query
    reports ``unproven`` and every dependent operation refuses.
    """

    def __init__(
        self,
        *,
        authorization: Optional[ArtifactAuthorization] = None,
        live_proof_receipt: Optional[dict[str, Any]] = None,
        deployment_generation: int = 0,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._authorization = authorization
        self._receipt = live_proof_receipt
        self._generation = deployment_generation
        self._clock = clock

    def status(self) -> str:
        if self._receipt is None:
            return STATUS_UNPROVEN
        try:
            validate_proof_receipt(
                self._receipt,
                authorization=self._authorization,
                deployment_generation=self._generation,
                now=self._clock(),
            )
        except ContainmentUnproven:
            return STATUS_UNPROVEN
        return STATUS_PROVEN

    def require_proven(self, purpose: str) -> None:
        """Gate a containment-dependent path; refuses while unproven."""
        if self.status() != STATUS_PROVEN:
            raise ContainmentUnproven(
                f"{purpose} requires the proven containment composition; "
                "containment is unproven, so the path stays preserved/alert-only"
            )

    def revoke_generation(self, terminal_generation: str) -> None:
        """Crash-independent revocation of one generation's processes/handles.

        Requires a live, heartbeating extension session; without one the
        only honest outcome is refusal (fail closed, never open).
        """
        self.require_proven(f"generation revocation for {terminal_generation}")
        raise ContainmentUnproven(
            "no extension session is attached to this composition instance; "
            "the privileged artifact comes from the PF-1a companion lane"
        )

    def quiesce_effects(self, terminal_generation: str) -> None:
        """Drain then journal-revoke every generation-owned effect."""
        self.require_proven(f"effect closure for {terminal_generation}")
        raise ContainmentUnproven("no extension session is attached to this composition instance")
