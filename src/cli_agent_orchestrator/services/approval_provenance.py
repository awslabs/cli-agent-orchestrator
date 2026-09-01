"""Resolve who approved a plan, bounded (issue #583 Bolt 2, unit ``approval-operation``).

This exists as its own module for one reason: it is the discharge of ``SR-2B1-7``, an obligation
``approval-store`` carried unowned across two units — *"``approved_by``'s length and provenance are
``approval-operation``'s to constrain"* — and an obligation that lives inside a request handler is an
obligation nobody can find or test.

IT IS A PROVENANCE NOTE AND NOT AN IDENTITY CLAIM, and every description of it must say so. CAO runs
as the invoking user and nothing here verifies anything; ``approval-gate`` already recorded the bound
(``SR-2B4-12``: a same-user local control, not a privilege boundary). What this value can honestly
report is which local account ran the approve command. Documenting it as more than that would be the
defect.

IT IS BOUNDED BECAUSE IT IS INPUT, WHICH IS THE PART THAT LOOKS WRONG UNTIL YOU CHECK.
``getpass.getuser()`` resolves from ``LOGNAME`` / ``USER`` / ``LNAME`` / ``USERNAME`` before falling
back to the password database — environment variables, hence caller-influenced. Its air of being an
ambient fact about the machine is exactly what would get it written to a durable row unchecked. Two
concrete consequences if it were not bounded:

* an arbitrarily long value grows a write-once row that can never be updated or deleted;
* a value containing a newline splits one log line into two apparent events, and this unit logs
  ``approved_by`` at ``info`` as the only record of when an approval intent was expressed.
"""

import getpass
import logging

logger = logging.getLogger(__name__)

#: Comfortably longer than any real account name, short enough that a crafted value cannot grow the
#: row. ``approval-store``'s column is unbounded TEXT, so the bound has to live here.
APPROVED_BY_MAX_LENGTH = 64

#: Used when the account cannot be resolved at all. Obviously generic on purpose — it must not be
#: mistakable for a real username, because a reader comparing two rows should be able to tell
#: "unknown" from "someone called unknown".
UNRESOLVED_ACCOUNT = "unknown-local-account"


def bound(value: str) -> str:
    """Strip control characters and truncate to :data:`APPROVED_BY_MAX_LENGTH`.

    Separate from :func:`local_account` so the bounding rule can be tested directly against hostile
    values rather than only through whatever the test machine's environment happens to hold.
    """
    cleaned = "".join(ch for ch in value if ch.isprintable())
    cleaned = cleaned.strip()
    if not cleaned:
        return UNRESOLVED_ACCOUNT
    return cleaned[:APPROVED_BY_MAX_LENGTH]


def local_account() -> str:
    """The local OS account, bounded. NEVER raises.

    A grant must not fail because a provenance note could not be written: the operator's intent is
    the point, and this value is a note about it. Anything unresolvable becomes
    :data:`UNRESOLVED_ACCOUNT`.
    """
    try:
        raw = getpass.getuser()
    except Exception as e:  # noqa: BLE001 — a note must never fail the operation it annotates
        logger.warning("could not resolve the local account for an approval: %s", e)
        return UNRESOLVED_ACCOUNT
    if not isinstance(raw, str):
        return UNRESOLVED_ACCOUNT
    return bound(raw)
