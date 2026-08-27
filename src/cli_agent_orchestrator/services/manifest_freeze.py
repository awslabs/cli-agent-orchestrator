"""Assemble and freeze a script-tier run's execution manifest (issue #583, unit ``manifest-freeze``).

Where pass 2A's three other units become one behaviour: this module derives a repository baseline, assembles
the fields it can actually know, computes the ``plan_id``, and builds the bounded, redacted envelope. It
re-implements none of that — ``execution_manifest`` owns the envelope, ``plan_identifier`` owns the
identifier, ``git_baseline`` owns the baseline.

IT RETURNS A VALUE AND WRITES NOTHING, AND THAT IS THE WHOLE DESIGN. The two script-tier callers already
call ``workflow_journal.insert_run``; they pass this function's result to it, so the manifest and the run row
land in ONE statement.

Bolt 1 paid for this lesson. Its critical hazard was exactly the two-write shape: ``append_step`` +
``update_step`` were two transactions for one logical settle, and a crash between them left a row with a
settled state and a NULL output that the replay guard could not detect — returning a successful replay
carrying no output. ADR-583-4's answer was a single-transaction settle. The equivalent window here would
produce a run row whose ``manifest_json`` is NULL, INDISTINGUISHABLE from a YAML run and from a failed
freeze, so nothing could tell a crashed freeze from a benign absence. Eliminating the interval eliminates the
state.

BEST-EFFORT AND TOTAL: every failure returns ``None`` after a ``warning``, and the run continues. This
matches ``_journal_insert_run``'s stated posture ("journal write is best-effort; in-memory floor still serves
live reads") and INV-4 — a journal concern must never reach into a running step. It is safe because the
failure direction is FAIL-CLOSED: a ``None`` becomes a NULL manifest, which the Bolt 2 approval gate reads as
"never approved" and REFUSES. The failure can never let an unapproved run execute; it can only refuse a run
that should have been allowed.

SIX OF FR-8'S NINE MANIFEST FIELDS ARE OMITTED, AND THE OMISSION IS THE HONEST READING. Verified against
``WorkflowRunRequest`` (exactly ``name_or_path``, ``inputs``, ``run_id``) and ``ScriptSpec`` (``name``,
``path``, ``source``, ``content_hash``, ``findings``, ``inputs``): provider, model, profile, permissions,
limits and retry policy have NO run-level source in the script tier. Each is a per-step argument the Python
passes to ``step()`` while executing — the same property that made ``plan_id`` run-level in the first place,
because script-tier steps are DISCOVERED BY EXECUTING THE PYTHON.

They are omitted rather than written as ``null`` because a ``null`` asserts "this field exists and has no
value", while the truth is that the field has no run-level existence here. Six nulls would collapse "not
applicable in this tier", "not captured" and "genuinely absent" into one indistinguishable token, and the
repair a future reader would most plausibly attempt is inventing run-level execution defaults — a concept
this tier does not have.

FR-8'S PASS CRITERION STILL HOLDS, TRANSITIVELY: those six settings are chosen BY THE SOURCE, so changing any
of them means editing the script, which changes ``content_hash``, which changes ``plan_id``. The source hash
covers all six. That argument is not left as prose — ``test_manifest_freeze.py`` changes a step's provider in
the script source and asserts the identifier changes.

WHAT THIS MODULE DOES NOT DO. It does not write (the callers do, in one statement), does not bound or redact
(``execution_manifest.build`` does), does not compute the identifier (``plan_identifier.compute`` does), does
not resolve memory (``memory-resolve-once``, pass 2B, fills the record this leaves empty), and does not touch
the YAML tier at all.
"""

import logging
import os
from typing import Any, Dict, Optional

from cli_agent_orchestrator.services import execution_manifest, plan_identifier
from cli_agent_orchestrator.utils.git_baseline import derive_baseline

logger = logging.getLogger(__name__)

OMITTED_FIELDS_NOTE = (
    "provider, model, profile, permissions, limits and retry_policy are omitted: they have no "
    "run-level source in the script tier (each is a per-step argument to step()). They are covered "
    "transitively by source_hash, because the script's own source chooses them."
)
"""Recorded IN the envelope, not only in the design documents.

The envelope is what an agent reads when diagnosing a failed run (FR-12's concern). A divergence from FR-8's
field list explained only in a design artifact is a divergence the reader of the DATA will rediscover, and
most likely misread as missing data.
"""


def build_manifest_json(
    *,
    source_hash: str,
    inputs: Any,
    cwd: Optional[str] = None,
) -> Optional[str]:
    """Freeze the manifest for a script-tier run and return the value to store, or ``None``.

    Called ONCE per run, at run-row creation, before any step executes — ``plan_id`` must exist before
    execution for an approval to be checked against it, so a manifest assembled later could only describe
    what already ran.

    TOTAL: raises nothing. Any failure logs a ``warning`` and returns ``None``, which the caller passes to
    ``insert_run`` as a NULL manifest. The exception text is logged; NO FIELD VALUE IS, because ``inputs``
    is exactly where a credential appears in practice.
    """
    try:
        baseline: Dict[str, Any] = derive_baseline(cwd or os.getcwd())
        if not baseline.get("available"):
            logger.warning(
                "manifest freeze unavailable repository baseline (run continues, manifest absent)"
            )
            return None

        # Six of FR-8's nine fields are None here because they have no run-level source in this tier
        # (see the module docstring). ``plan_identifier`` renders a None scalar as a distinct
        # not-supplied sentinel, so their absence is part of the identity rather than invisible to it.
        fields = plan_identifier.PlanFields(
            source_hash=source_hash,
            inputs=inputs,
            repo_baseline=baseline,
            provider=None,
            model=None,
            profile=None,
            permissions=None,
            limits=None,
            retry_policy=None,
        )
        plan_id = plan_identifier.compute(fields)

        # ``build`` is the ONLY door: it owns bounding and redaction, so nothing here constructs JSON,
        # truncates, or calls a redactor. Two paths producing manifests would let the newer one skip
        # NFR-1's guarantees entirely.
        envelope = execution_manifest.build(
            plan_id=plan_id,
            source_hash=source_hash,
            inputs=inputs,
            repo_baseline=baseline,
            # The six are left None, which ``_document`` DROPS rather than serialising as null — the
            # distinction between "no run-level existence in this tier" and "exists but empty".
            provider=None,
            model=None,
            profile=None,
            permissions=None,
            limits=None,
            retry_policy=None,
            # The explanation travels IN the envelope, in its own field rather than smuggled into a
            # field meant for a provider name.
            notes=OMITTED_FIELDS_NOTE,
            # Empty at freeze time: there is no resolved memory at run start. ``memory-resolve-once``
            # (pass 2B) owns populating this, and a reader expecting FR-9's payload here would otherwise
            # read the empty record as a defect.
            memory_content="",
            memory_source="",
        )
        return execution_manifest.serialise(envelope)
    except (
        Exception
    ) as e:  # noqa: BLE001 — best-effort; the run must not fail because of the manifest
        logger.warning("manifest freeze failed (run continues, manifest absent): %s", e)
        return None
