"""Build, serialise and parse a step's result envelope (issue #583, unit ``result-envelope``).

Leaf module: imports only ``constants``, the model, and ``secret_gate``. Pure — no I/O, no
state, no logging, no exceptions raised on any input. Bolt 1A writes through it
(``settlement-rewire`` calls ``build_envelope``, ``journal-step-lifecycle`` persists the
string); Bolt 1B's replay gate reads back through it.

It is a module of its own rather than a home in ``services/step_replay.py`` because that
module is created by ``replay-gate`` in Bolt 1B, while a Bolt 1A caller needs
``build_envelope`` — and Bolt 1A ships as its own pull request first.

WHAT THIS MODULE DOES NOT DO. It does not write the column, does not decide whether a step
may replay, and does not touch ``output_json``. It only makes an envelope exist, be safe to
store, and be safe to read back.
"""

import json
from typing import Optional

from cli_agent_orchestrator.constants import WORKFLOW_JOURNAL_RESULT_MAX_BYTES
from cli_agent_orchestrator.models.workflow import StepResultEnvelope
from cli_agent_orchestrator.services.secret_gate import redact_secrets


def build_envelope(
    last_message: str,
    status: str,
    terminal_id: Optional[str] = None,
) -> StepResultEnvelope:
    """Build the durable envelope for a settled step. TOTAL — never raises (INV-2).

    REDACTION RUNS FIRST, THEN BOUNDING (SR-1/BR-2). The order is a security requirement,
    not an implementation preference. Were bounding first, the redactor would only ever see
    the kept prefix, so a credential in the dropped tail would be invisible to it — and a
    credential straddling the boundary would be CUT IN HALF, defeating the pattern match
    while persisting the surviving half. A partial credential is not safe merely for being
    partial: an AWS key prefix or a PEM header is itself a signal.

    THE BOUND IS ON BYTES, NOT CHARACTERS (TD-2). The column limit is a storage limit, so a
    character count would let a multi-byte-heavy message exceed it. Slicing the UTF-8
    encoding can split a multi-byte character, hence ``errors="ignore"`` on the decode — the
    partial trailing sequence is dropped rather than raising or emitting U+FFFD.

    ``redacted`` is ``bool(fired)`` and the pattern names are DISCARDED (SR-4): redaction
    cascades, so a later pattern can match an earlier ``[REDACTED:<name>]`` marker and
    ``fired`` can name a pattern that only ever matched a marker. Storing it would ship
    evidence that looks precise and is not.

    Bounding is lossy but TOTAL (SR-5/BR-3): an over-long message is truncated and flagged,
    never rejected. The step already succeeded; turning a persistence limit into a run
    failure would let a verbose agent fail runs by talking too much.

    The original matched bytes are never echoed — not into the envelope, not into a log line,
    not into an exception (SR-2). This module emits nothing at all.

    Args:
        last_message: the step's raw text result. Empty is legal (BR-3 edge case).
        status: the terminal status the step finished with. A FAILED step gets an envelope
            too (BR-1) — absence means *crash between the writes*, never *the step failed*.
        terminal_id: the terminal the step ran on, or ``None`` (BR-6). Retained even though
            it names a dead terminal by replay time.

    Returns:
        A ``StepResultEnvelope`` whose ``truncated``/``redacted`` flags are ``True`` exactly
        when the corresponding transformation removed content (INV-5).
    """
    text, fired = redact_secrets(last_message)
    # SR-4: the boolean only. ``fired`` is deliberately not carried any further.
    redacted = bool(fired)

    encoded = text.encode("utf-8")
    truncated = len(encoded) > WORKFLOW_JOURNAL_RESULT_MAX_BYTES
    if truncated:
        # Inclusive boundary: a message of exactly the bound is NOT truncated.
        text = encoded[:WORKFLOW_JOURNAL_RESULT_MAX_BYTES].decode("utf-8", errors="ignore")

    return StepResultEnvelope(
        last_message=text,
        status=status,
        terminal_id=terminal_id,
        truncated=truncated,
        redacted=redacted,
    )


def serialise_envelope(envelope: StepResultEnvelope) -> str:
    """Serialise an envelope to the compact JSON stored in ``workflow_run_step.result_json``.

    Stdlib ``json`` with compact separators (TD-2): the two ``str`` fields, the optional id
    and the two booleans are all JSON-native, so no encoder is needed. Compact because the
    value is a database column, not a document a human reads formatted.

    Bounding already happened in ``build_envelope``, so the serialised string is bounded by
    the ``last_message`` limit plus a short status, an id and two booleans.
    """
    return json.dumps(envelope.model_dump(), separators=(",", ":"))


def parse_envelope(result_json: Optional[str]) -> Optional[StepResultEnvelope]:
    """Read an envelope back off a row. TOTAL — never raises (INV-3/BR-5).

    NULL, malformed JSON and valid-JSON-of-the-wrong-shape are ALL ``None``, and collapsing
    them is deliberate: every one of them means "this row cannot be replayed". Distinguishing
    them would invite a third behaviour for corrupt data; collapsing them leaves exactly two
    outcomes — replay with a verified envelope, or do not replay.

    ``None`` here is the RETURN CONTRACT, not a swallowed error. A corrupt envelope must
    decline to replay rather than crash a resume, and a pre-unit row reads as absent by
    construction (``DEFAULT NULL``, BR-10) — which agrees with FR-6, since such a row's
    fingerprint is legacy-scheme or NULL and it can never reach the replay path anyway.

    Nothing is logged: the input is agent-produced text that may contain a credential shape
    ``secret_gate`` does not know (SR-3), and a log line about a malformed envelope is
    exactly the durable echo SR-2 forbids. A caller that needs to distinguish "absent" from
    "corrupt" for diagnostics must decide that above this function, on evidence it owns.
    """
    if result_json is None:
        return None
    try:
        return StepResultEnvelope.model_validate_json(result_json)
    except Exception:  # noqa: BLE001 — totality is the contract (INV-3); see the docstring
        return None
