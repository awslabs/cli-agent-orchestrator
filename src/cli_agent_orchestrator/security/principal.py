"""Who a deferred unit of work belongs to, and whether it may still start (#745).

#745's acceptance criterion: *"Shared-use assignments preserve tenant/owner
context through queues, schedules and callbacks; removed members cannot start
more work, and authorized cancellation/cleanup still functions after their
delegated access is revoked."* The trusted tenant/owner record itself is #774's
and #778's scope — SAML/Entra sign-in and a first-class organisation. What
belongs to **this** issue is the part the restructuring put at risk:

> Queued or scheduled work must not become the server's anonymous local user
> when its initiating request ends.

Before the split, an accepted request and the process that executed it were the
same process. Now a scheduled flow fires minutes later in a central server, an
elastic assignment is executed in a pod that was minted after the request
returned, and the result comes back as a callback from a third process. Each of
those hops is a place where the owner can silently become "whoever the server
runs as". So this module gives the owner a shape that survives a hop:

- a ``Principal`` — an issuer and a subject, nothing else, deliberately not a
  user profile — with a single canonical string form (``id``) for a DB column,
  an env var or a JSON payload;
- ``LOCAL_PRINCIPAL``, the named owner of work in a single-user installation.
  It is a *named* principal, not a null: "local" is an answer, whereas ``None``
  is the anonymity the criterion forbids, and the two must not be spelled the
  same way;
- a revocation seam, and the asymmetry that makes revocation safe.

**The asymmetry is the load-bearing part.** A revoked owner must not be able to
*start* more work, and must still be able to have their running work *stopped*
and its diagnostics collected — that is the criterion's second half, and getting
it backwards is how a revocation leaves an agent running with nobody authorised
to kill it. Hence two predicates rather than one flag: ``may_start_work`` and
``may_stop_work``, named so the call site reads as the policy it implements.

Deliberately NOT here: how a principal comes to be revoked. This registry is
process-local and seeded from ``CAO_REVOKED_PRINCIPALS``; #779 owns the
management surface (roles, member removal, revocable sessions) and the
durability that goes with it. The seam exists so that when #779 lands, the
*enforcement points* are already in the right places and already tested.

This module is imported by ``security/auth.py`` and therefore holds to the same
boundary: standard library only, no ``clients.database``, no ``clients.tmux``.
"""

import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Iterable, Optional, Set

logger = logging.getLogger(__name__)

LOCAL_ISSUER = "cao:local"
LOCAL_SUBJECT = "local"

REVOKED_ENV = "CAO_REVOKED_PRINCIPALS"

# The separator in the canonical id. '#' cannot appear in a URL issuer's
# authority and is not produced by any IdP we accept as part of a `sub`, so
# `id` round-trips: split once and both halves come back intact.
_SEPARATOR = "#"


class PrincipalError(ValueError):
    """A principal could not be built from the given parts."""


@dataclass(frozen=True)
class Principal:
    """The owner of a unit of work: an issuer and a subject within it.

    Frozen because a principal is carried through queues and callbacks — a
    holder that could mutate it is a holder that can change whose work this is.

    Two fields on purpose. A display name, an email or a role would all be
    copies of state that #774/#778 own, and a copy is a thing that goes stale;
    the subject is the only part an authorisation decision needs.
    """

    subject: str
    issuer: str = LOCAL_ISSUER

    def __post_init__(self) -> None:
        for field, value in (("subject", self.subject), ("issuer", self.issuer)):
            if not isinstance(value, str) or not value.strip():
                raise PrincipalError(f"principal {field} must be a non-empty string")
            if _SEPARATOR in value:
                # Otherwise `id` would not round-trip and two different
                # principals could share one canonical form.
                raise PrincipalError(f"principal {field} must not contain {_SEPARATOR!r}")

    @property
    def id(self) -> str:
        """The canonical form: one opaque string, safe in a column or an env var."""
        return f"{self.issuer}{_SEPARATOR}{self.subject}"

    @property
    def is_local(self) -> bool:
        return self.issuer == LOCAL_ISSUER

    def __str__(self) -> str:  # log lines read better than the dataclass repr
        return self.id

    @classmethod
    def parse(cls, raw: Optional[str]) -> Optional["Principal"]:
        """Read a canonical id back, or ``None`` for a missing one.

        A *malformed* value is not read as local. A row written by a newer
        version, or a truncated env var, is an unknown owner — and an unknown
        owner silently becoming the local one is exactly the anonymisation this
        module exists to prevent, so it raises.
        """
        if raw is None:
            return None
        text = raw.strip()
        if not text:
            return None
        if _SEPARATOR not in text:
            raise PrincipalError(f"not a principal id: {text!r}")
        issuer, subject = text.split(_SEPARATOR, 1)
        return cls(subject=subject, issuer=issuer)

    def to_json(self) -> str:
        """The wire form for a command payload. Sorted, so it is comparable."""
        return json.dumps({"iss": self.issuer, "sub": self.subject}, sort_keys=True)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> Optional["Principal"]:
        if raw is None or not str(raw).strip():
            return None
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise PrincipalError(f"not a principal document: {exc}") from exc
        if not isinstance(data, dict):
            raise PrincipalError("principal document must be an object")
        # Unknown keys are tolerated, not copied: #774/#778 will add a tenant to
        # this document, and an older server must carry what it does understand
        # rather than reject the work outright.
        return cls(subject=str(data.get("sub", "")), issuer=str(data.get("iss", LOCAL_ISSUER)))


LOCAL_PRINCIPAL = Principal(subject=LOCAL_SUBJECT, issuer=LOCAL_ISSUER)


class RevocationRegistry:
    """Which principals may no longer start work.

    Process-local and explicitly not durable: #779 owns member removal and
    revocable sessions, including where that state lives. Seeding from
    ``CAO_REVOKED_PRINCIPALS`` is what makes the gate operable before then —
    an operator can name revoked ids in the server's environment.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: Set[str] = set()
        self._seeded = False

    def _seed(self) -> None:
        """Read the env allowlist once, on first use.

        Lazy rather than at import: the server reads its environment after the
        module graph is built, and tests set the variable per case.
        """
        if self._seeded:
            return
        raw = os.environ.get(REVOKED_ENV, "")
        for item in raw.replace("\n", ",").split(","):
            candidate = item.strip()
            if not candidate:
                continue
            try:
                principal = Principal.parse(candidate)
            except PrincipalError:
                # A malformed entry must not be silently ignored: an operator
                # who mistyped a revocation would believe access was withdrawn.
                logger.error("ignoring malformed entry in %s: %r", REVOKED_ENV, candidate)
                continue
            if principal is not None:
                self._revoked.add(principal.id)
        self._seeded = True

    def revoke(self, principal: Principal) -> None:
        with self._lock:
            self._seed()
            self._revoked.add(principal.id)
        logger.info("principal revoked: %s", principal.id)

    def reinstate(self, principal: Principal) -> None:
        with self._lock:
            self._seed()
            self._revoked.discard(principal.id)

    def is_revoked(self, principal: Optional[Principal]) -> bool:
        """Whether this principal's access has been withdrawn.

        An *unknown* owner (``None``) is not revoked. Work whose owner was never
        recorded predates this plumbing; treating it as revoked would strand
        existing schedules on upgrade, and the criterion is about removed
        members, not about rows written before the column existed.
        """
        if principal is None:
            return False
        with self._lock:
            self._seed()
            return principal.id in self._revoked

    def any_revoked(self) -> bool:
        """Whether anything is revoked at all.

        A gate on a hot path (inbox delivery) can answer "allowed" from this
        without first resolving whose work it is — and resolving an owner costs a
        DB read per batch. With no revocations configured, which is every
        single-user installation, that read never happens.
        """
        with self._lock:
            self._seed()
            return bool(self._revoked)

    def revoked_ids(self) -> Set[str]:
        with self._lock:
            self._seed()
            return set(self._revoked)

    def reset(self, ids: Iterable[str] = ()) -> None:
        """Test seam: replace the set and re-arm seeding."""
        with self._lock:
            self._revoked = {str(i) for i in ids}
            self._seeded = False


revocation = RevocationRegistry()


def may_start_work(principal: Optional[Principal]) -> bool:
    """Whether new work may be dispatched on this principal's behalf.

    Consulted at the *dispatch* boundary — where queued or scheduled work
    becomes a running agent — not at admission. A revocation that arrives while
    an attempt sits in a queue has to stop that attempt, which is the whole
    point of checking late.
    """
    return not revocation.is_revoked(principal)


def may_stop_work(principal: Optional[Principal]) -> bool:
    """Whether cancellation, teardown and diagnostics are still permitted.

    Always true, and a function rather than a comment so the asymmetry is
    visible at the call site and testable. Revocation withdraws the authority
    to *start* work; if it also withdrew the authority to stop it, a removed
    member's agent would keep running with nobody entitled to kill it, and the
    final diagnostics needed to explain what it did would be unreachable.
    """
    return True
