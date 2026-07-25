"""Canonical KAS launch-refusal exception.

This module is the canonical home for ``KiroLaunchRefusedError`` (ADR-002).
It deliberately sits at the ``models`` layer — below both ``providers`` and
``services`` — so that the ``utils``-layer launch guard (ADR-001) can raise it
without creating a ``utils -> providers`` module-level import (BR-U1-1).

Its only dependency is :class:`KiroEngine`, so nothing about it required living
in ``providers`` (where the Phase 0 exception was originally defined).
"""

from __future__ import annotations

from typing import Optional

from cli_agent_orchestrator.models.kiro_engine import KiroEngine

# The refusal code used by Phase 0's ``KiroPhase0KASError`` construction form.
# Legacy call sites construct the exception without ``code`` or ``message``; the
# default lets the constructor recognise them and reproduce the Phase 0 wording
# verbatim during the Bolt-1 -> Bolt-3 migration window (BR-U1-4).
LEGACY_REFUSAL_CODE = "kas-unavailable"


def _legacy_phase0_message(profile_has_v2_policy: bool) -> str:
    """Reproduce the Phase 0 refusal wording, including the profile note.

    Mirrors the message the relocated ``KiroPhase0KASError`` composed so the
    pre-existing suite and any operator-facing output are unchanged (NFR-105).
    """
    profile_note = (
        " The selected profile contains v2 allowedTools/toolsSettings that "
        "cannot be translated to Cedar in Phase 0."
        if profile_has_v2_policy
        else ""
    )
    return (
        "Kiro engine 'kas' is not available in Phase 0: KAS profiles and Cedar "
        "policy translation are not implemented. Retry with engine 'v2'." + profile_note
    )


class KiroLaunchRefusedError(ValueError):
    """Structured refusal of a KAS launch attempt.

    Subclasses ``ValueError`` (BR-U1-3 / team convention for 400-class domain
    errors) so pre-existing broad handlers keep binding. Each consumer surface
    renders it from the structured fields (ADR-005): the CLI prints the human
    message plus code and field, the API serialises ``code`` / ``profile_field``
    / ``message`` as JSON, and any un-updated generic handler still degrades
    gracefully because ``str(exc)`` is always the human message (BR-U1-5).

    ``profile_has_v2_policy`` is the **legacy Phase 0 parameter** and is
    deliberately first and positional-or-keyword: six pre-existing sites pass it
    by keyword and one (``terminal_service.create_terminal``) passes it
    positionally, and both forms must keep constructing between Bolt 1 (this
    unit) and Bolt 3 (call-site migration) — BR-U1-4.
    """

    def __init__(
        self,
        profile_has_v2_policy: bool = False,
        *,
        code: str = LEGACY_REFUSAL_CODE,
        message: Optional[str] = None,
        profile_field: Optional[str] = None,
        engine: KiroEngine = KiroEngine.KAS,
    ) -> None:
        # BR-U1-5 / SEC-U1-6: never ``super().__init__(None)`` — that makes
        # ``str(exc)`` the literal string "None" and every generic logging
        # handler would emit it instead of an explanation. A message is always
        # composed: caller-supplied verbatim, else the legacy Phase 0 text when
        # the caller used the legacy construction form (no explicit code), else
        # derived from the code and engine.
        if message is None:
            if code == LEGACY_REFUSAL_CODE:
                message = _legacy_phase0_message(profile_has_v2_policy)
            else:
                message = (
                    f"Kiro engine '{engine.value}' launch was refused ({code}). "
                    "See 'cao profile lint <name>' for the blocking diagnostic."
                )
        super().__init__(message)
        self.code = code
        self.message = message
        self.profile_field = profile_field
        self.engine = engine
        # Retained so the legacy construction form remains fully inspectable
        # until U8 removes its last caller.
        self.profile_has_v2_policy = profile_has_v2_policy
