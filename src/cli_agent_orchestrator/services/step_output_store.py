"""In-memory structured-return store (issue #312, Bolt 2 / N4, ADR-4).

A process-local, capped, oldest-first-evicting collection keyed by
``(run_id, step_id)`` that holds the worker-emitted output of one workflow step
plus its schema-validation verdict. It decouples worker-emit timing from
engine-consume timing; the engine (N5, Bolt 3) reads it when sequencing the next
step. The N6 run journal swaps in behind this same interface with no contract
change.

The store is **transient** and **best-effort**: a process restart loses it (the
explicit pre-N6 gap, ADR-8), and cap eviction honors the non-blocking promise —
it NEVER raises. ``record_step_output`` validates the output against an
``output_schema`` (passed WITH the request for the synthetic-key MVP, since there
is no run record in Bolt 2 — F2) using the SAME ``jsonschema`` Draft 2020-12
validator family Bolt 1 used to check schema well-formedness, so authoring-time
and runtime checks agree (B2-BR-7). A schema failure is RECORDED (validated=False,
state COMPLETED_UNVALIDATED), never raised.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

import jsonschema  # type: ignore[import-untyped]  # stable Draft 2020-12 API, matches N1

from cli_agent_orchestrator.constants import (
    WORKFLOW_NAME_RE,
    WORKFLOW_OUTPUT_STORE_MAX_ENTRIES,
)
from cli_agent_orchestrator.models.workflow import StepOutputRecord, StepState

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(WORKFLOW_NAME_RE)


def _validate_key_part(value: str, label: str) -> str:
    """Validate a ``run_id`` / ``step_id`` against the anchored name regex (B2-BR-1).

    Rejects traversal tokens and any value not matching ``WORKFLOW_NAME_RE``.
    Raises ``ValueError`` -> HTTPException 400 at the boundary.
    """
    if value in (".", ".."):
        raise ValueError(f"{label} '{value}' is not allowed (traversal token)")
    if not _NAME_RE.match(value):
        raise ValueError(f"{label} '{value}' is invalid (must match {WORKFLOW_NAME_RE})")
    return value


class StepOutputStore:
    """A capped, insertion-ordered ``(run_id, step_id)`` -> ``StepOutputRecord`` map.

    Oldest-first soft eviction at ``WORKFLOW_OUTPUT_STORE_MAX_ENTRIES`` (Q1=A): a
    plain ``OrderedDict`` with ``popitem(last=False)``, no LRU dependency.
    Last-write-wins on the same key (a reprompt overwrites in place rather than
    growing the store), and re-inserting an existing key refreshes its position
    so it is not the eviction victim.
    """

    def __init__(self, max_entries: int = WORKFLOW_OUTPUT_STORE_MAX_ENTRIES) -> None:
        self._max_entries = max_entries
        self._store: "OrderedDict[Tuple[str, str], StepOutputRecord]" = OrderedDict()

    def put(self, run_id: str, step_id: str, record: StepOutputRecord) -> None:
        """Store a record, evicting the oldest entry first if over the cap.

        Eviction is best-effort and NEVER raises (the store is transient): any
        unexpected error during eviction is logged and swallowed so a worker's
        structured return is never blocked by store bookkeeping.
        """
        key = (run_id, step_id)
        # Last-write-wins: drop any existing entry so the re-insert lands at the
        # newest position (and the count is correct for the cap check).
        if key in self._store:
            del self._store[key]
        self._store[key] = record
        try:
            while len(self._store) > self._max_entries:
                evicted_key, _ = self._store.popitem(last=False)
                logger.debug("StepOutputStore: evicted oldest entry %s (cap reached)", evicted_key)
        except Exception as e:  # noqa: BLE001 — transient store; eviction must never block a put
            logger.warning("StepOutputStore: eviction skipped after error: %s", e)

    def get(self, run_id: str, step_id: str) -> Optional[StepOutputRecord]:
        """Return the record for ``(run_id, step_id)``, or None if absent."""
        return self._store.get((run_id, step_id))

    def __len__(self) -> int:
        return len(self._store)


# Module-level singleton — process-local, shared by the API endpoint.
step_output_store = StepOutputStore()


def _collapse_schema_error(exc: jsonschema.ValidationError) -> str:
    """Collapse a (possibly multi-line) jsonschema error to a single line."""
    message = str(exc).splitlines()[0] if str(exc) else "output failed schema validation"
    return message


def record_step_output(
    run_id: str,
    step_id: str,
    output: Dict[str, Any],
    output_schema: Optional[Dict[str, Any]] = None,
) -> StepOutputRecord:
    """Validate a worker output against its schema and store the record (C5, FR-4.1).

    For the synthetic-key MVP the ``output_schema`` arrives WITH the request
    (there is no run record to re-resolve it from — F2). The validation locus is
    the seam, not the engine (ADR-4).

    - No ``output_schema`` -> ``validated=True`` (trivially valid), state
      ``COMPLETED``.
    - Schema **pass** -> ``validated=True``, ``errors=[]``, state ``COMPLETED``.
    - Schema **fail** -> ``validated=False``, ``errors=[<collapsed reason>]``,
      state ``COMPLETED_UNVALIDATED``. This does NOT raise — the flag is recorded
      for the engine to act on (FR-4.3 / B2-BR-8).

    ``run_id`` / ``step_id`` are validated per B2-BR-1 before any store write; a
    malformed key raises ``ValueError`` -> HTTPException 400.

    Returns the stored ``StepOutputRecord``.
    """
    _validate_key_part(run_id, "run_id")
    _validate_key_part(step_id, "step_id")

    validated = True
    errors: List[str] = []
    if output_schema is not None:
        try:
            jsonschema.validate(output, output_schema, cls=jsonschema.Draft202012Validator)
        except jsonschema.ValidationError as exc:
            validated = False
            errors = [_collapse_schema_error(exc)]
        except jsonschema.SchemaError as exc:
            # A malformed schema arriving at runtime is a caller error (the
            # authoring-time check should have caught it); treat it as a 400.
            raise ValueError(f"output_schema is not valid JSON-Schema: {exc}")

    record = StepOutputRecord(
        run_id=run_id,
        step_id=step_id,
        output=output,
        validated=validated,
        errors=errors,
        state=StepState.COMPLETED if validated else StepState.COMPLETED_UNVALIDATED,
    )
    step_output_store.put(run_id, step_id, record)
    return record
