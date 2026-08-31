"""Resolve CAO memory once per run and freeze it (issue #583 Bolt 2, unit ``memory-resolve-once``).

FR-9's actual guarantee. Two units built the parts and left them inert: ``manifest-freeze`` wrote an
empty ``FrozenMemoryRecord`` and named this unit as its filler, and ``terminal-frozen-memory`` added
the optional pre-resolved parameter on the injection path with nothing to supply it. This module is
what resolves, records, and supplies — so that altering CAO memory after a failure cannot change what
a resumed run sees.

THE ORDER IS PERSIST-THEN-USE, AND IT IS THE WHOLE CORRECTNESS ARGUMENT. The record is written into
``manifest_json`` BEFORE the block is handed to a terminal. The two crash outcomes are not symmetric:

* crash AFTER persisting — the manifest records memory that may never have been used. An over-record:
  harmless, and detectable, because the run has a memory record and no terminal.
* crash AFTER using and before persisting — the run SAW memory that nothing recorded. That is FR-9's
  own Fail criterion, and it fails SILENTLY, because an empty memory record is indistinguishable from
  a run that never created a terminal. Worse, a later replay reads that empty record and falls through
  to LIVE memory, which is the exact drift the feature exists to prevent.

Only one of the two breaks the requirement, so the ordering is forced rather than preferred. It is the
same discipline the envelope already applies with redact-then-bound and hash-then-redact.

A FAILED PERSIST INJECTS NOTHING. That is not a separate policy — handing over a block whose record did
not persist IS the use-then-persist failure, reached by another route. The run proceeds with no memory:
a visible, explicable difference rather than an unrecorded one.

THE ONCE-PER-RUN FLAG IS THE RECORD'S PRESENCE, NOT ITS CONTENT. A run that legitimately resolved no
memories records ``""``, and ``""`` is a RESOLVED result — ``terminal_service`` discriminates on
``is None`` for exactly this reason. Were the flag "content is truthy", such a run would re-resolve at
every terminal and again on every resume, each time reading the live store. The flag is durable
(a column, not process state) because a resume can happen in a fresh process, and process-local
bookkeeping would forget and re-resolve — reintroducing the requirement's Fail criterion through
bookkeeping rather than logic.

THE RUN IS INJECTED WITH WHAT WAS STORED, not with what was resolved. The stored copy is redacted and
may be truncated, so it and the raw resolution are not always the same bytes — and only one of them can
be injected. Injecting the stored copy makes the original run and every replay see BYTE-IDENTICAL
context, which is FR-9 met strictly. Two consequences, both accepted deliberately:

* a workflow-driven terminal's memory block is REDACTED, where a non-workflow terminal's is not. That
  is a real behaviour difference, documented in ``docs/workflows.md``; it is also a security
  improvement, since a secret sitting in curated memory stops reaching the agent's context here.
* when the bound bites, the agent sees less memory than the live path would have given it, and
  ``memory.truncated`` records that it happened.

The alternative — full block on the first run, stored copy on a replay — diverges only on runs where
redaction or truncation bit, which is to say on runs with a lot of memory, which is to say it would be
found in production rather than in a test.

WHAT THIS MODULE DOES NOT DO. It does not hash, redact, bound or truncate (``execution_manifest`` owns
all four, and hashes BEFORE redacting so the hash identifies what was resolved). It does not decide
whether memory reaches an agent at all — the operator's kill switch governs that, and
``terminal_service`` honours it on the frozen path too. It does not resolve eagerly: a run that creates
no terminal resolves nothing, because an unused block is sensitive text kept for no reason.
"""

import logging
from typing import Optional

from cli_agent_orchestrator.services import execution_manifest, workflow_journal

logger = logging.getLogger(__name__)

#: What ``source`` records for a block resolved through the ordinary curated path. A fixed token
#: rather than free text, because it is compared across a resume and read by ``frozen-context-proof``.
MEMORY_SOURCE_CURATED = "cao-memory:curated"


def _resolve_live(terminal_id: str, task_description: str) -> str:
    """Resolve memory the same way the live injection path does.

    Deliberately the SAME call ``terminal_service.inject_memory_context`` makes, including the 200
    character task-description slice: this unit changes WHEN memory is resolved, never WHAT
    resolution means. A separate resolution path here would make a replayed run's context differ from
    a live one for reasons unrelated to freezing.
    """
    from cli_agent_orchestrator.services.memory_service import MemoryService

    return MemoryService().get_curated_memory_context(
        terminal_id, task_description=task_description[:200]
    )


def frozen_memory_for(
    run_id: Optional[str], terminal_id: str, task_description: str = ""
) -> Optional[str]:
    """The memory block this run's terminals must be given, or ``None`` for "resolve live".

    ``None`` means the caller should pass nothing to ``send_input`` and let today's live path run —
    which is the correct answer for a non-workflow terminal, for a YAML run, or for a run with no
    readable manifest. ``""`` means a manifest existed but its memory block could not be persisted;
    the caller must supply that empty block to prevent the terminal's live-memory fallback.

    TOTAL: never raises. A workflow step must not fail because a memory block could not be frozen;
    the worst outcome here is a run that proceeds without memory, which is visible in the manifest.
    """
    if not run_id:
        # Not a workflow terminal. Today's behaviour, untouched — C-1.
        return None

    try:
        row = workflow_journal.get_run(run_id)
    except Exception as e:  # noqa: BLE001 — a journal fault must not fail the step
        logger.warning("frozen memory: could not read run '%s' (resolving live): %s", run_id, e)
        return None

    if row is None or row.manifest_json is None:
        # A YAML run never freezes a manifest, and a script run whose freeze failed has none either.
        # Both mean "there is nothing frozen to honour", which is not the same as "no memory".
        return None

    envelope = execution_manifest.parse(row.manifest_json)
    if envelope is None:
        logger.warning(
            "frozen memory: run '%s' has an unreadable manifest (resolving live)", run_id
        )
        return None

    while True:
        if envelope.memory.source:
            # ``source`` is the durable resolved flag. Content may legitimately be "", and the
            # hash is always populated even before the lazy fill, so neither can identify a fill.
            return envelope.memory.content

        resolved = _resolve_live(terminal_id, task_description)
        filled = execution_manifest.with_memory(
            envelope, content=resolved, source=MEMORY_SOURCE_CURATED
        )

        try:
            persisted = workflow_journal.compare_and_set_run_manifest(
                run_id, row.manifest_json, execution_manifest.serialise(filled)
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "frozen memory: could not record the resolved block for run '%s'; the run proceeds "
                "with NO memory rather than using memory the manifest does not record: %s",
                run_id,
                e,
            )
            return ""

        if persisted:
            # The STORED copy — redacted, possibly truncated — so this run and every replay sees
            # the same bytes. Not `resolved`, which is the raw resolution.
            return filled.memory.content

        try:
            row = workflow_journal.get_run(run_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "frozen memory: could not reload run '%s' after a competing fill; "
                "the run proceeds with NO memory: %s",
                run_id,
                e,
            )
            return ""

        if row is None or row.manifest_json is None:
            logger.warning(
                "frozen memory: run '%s' disappeared after a competing fill; "
                "the run proceeds with NO memory",
                run_id,
            )
            return ""

        envelope = execution_manifest.parse(row.manifest_json)
        if envelope is None:
            logger.warning("frozen memory: run '%s' has an unreadable manifest", run_id)
            return ""
