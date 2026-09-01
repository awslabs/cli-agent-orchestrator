"""Failure classification (issue #583 Bolt 3, ``failure-classification``).

This unit is the sole owner of **FR-10's second Pass criterion** — "transient failures are
distinguishable from artifact defects requiring a new run" — whose Fail condition is "the two failure
classes are conflated".

Four carry the unit's load:

* ``test_a_cancelled_run_with_a_step_error_is_still_cancelled`` — THE ORDERING PROPERTY. A run cancelled
  while a step was in flight carries BOTH ``state == CANCELLED`` and a step-level error. Read the kind
  first and a human's deliberate stop is labelled a failure to diagnose — the conflation FR-10 forbids,
  arriving from a direction its wording does not anticipate. The wrong order yields a plausible
  ``transient``, not an exception, so nothing but this test would notice.
* ``test_an_unrecognised_kind_defaults_and_logs`` — a silent fallback is indistinguishable from a correct
  classification. Under the CLI's action phrasing an unrecognised kind renders as "Fix the spec", a
  confident instruction derived from no evidence, so the record is what makes a misled operator's case
  findable afterwards.
* ``test_the_error_text_is_never_consulted`` — SEC-3's teeth. A run whose message says "timeout" but whose
  kind is ``error`` must NOT classify ``transient``: the contract is branch-on-the-field, and a
  classifier that reads message text can be steered by workflow-controlled content.
* ``test_the_two_classes_differ_over_http`` — FR-10's criterion at the surface a caller actually uses. A
  unit test of the mapping would pass even if the field never reached the response.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import cli_agent_orchestrator.api.main as api_main


def _row(state: str, run_id: str = "r1", current_step_id: str | None = None):
    return SimpleNamespace(state=state, run_id=run_id, current_step_id=current_step_id)


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,kind,expected",
    [
        ("completed", None, None),
        ("completed", "error", None),  # a kind on a success is still not a failure
        ("running", None, None),
        ("failed", "timeout", "transient"),
        ("failed", "error", "artifact_defect"),
        ("failed", None, "artifact_defect"),
        ("failed", "cancelled", "cancelled"),
        ("cancelled", None, "cancelled"),
        ("cancelled", "cancelled", "cancelled"),
    ],
)
def test_the_mapping(state, kind, expected):
    assert api_main._classify_failure(_row(state), kind) == expected


def test_a_cancelled_run_with_a_step_error_is_still_cancelled():
    """THE ORDERING PROPERTY (BR-2). The wrong order is a plausible answer, not a failure.

    A cancel that arrives while a step is in flight leaves the run CANCELLED *and* the step carrying an
    error whose kind may be anything. Classifying on the kind first would tell the operator to retry or
    to rewrite their spec, when in fact they stopped it themselves.
    """
    for kind in ("timeout", "error", "something_new"):
        assert api_main._classify_failure(_row("cancelled"), kind) == "cancelled", (
            f"a cancelled run with kind={kind!r} must classify cancelled — the human's act is the "
            "more informative fact, and reading the kind first is the conflation FR-10 forbids"
        )


def test_the_error_text_is_never_consulted():
    """SEC-3. The classifier sees no message, so a message cannot steer it.

    Asserted through the signature: ``_classify_failure`` takes the row and the resolved kind, and has
    no parameter through which an error string could reach it. A run whose message says "timeout" but
    whose kind is ``error`` therefore cannot classify ``transient``.
    """
    import inspect

    params = list(inspect.signature(api_main._classify_failure).parameters)
    assert params == ["row", "error_kind"], (
        f"signature is {params} — a message/step parameter would open the regex-scraping path that "
        "services.md forbids"
    )
    row = _row("failed")
    row.error = "connection timeout after 30s"  # noqa: E501 - the decoy
    assert api_main._classify_failure(row, "error") == "artifact_defect"


# ---------------------------------------------------------------------------
# Totality (REL-1, REL-9) and the fallback record (REL-3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "row,kind",
    [
        (_row("failed"), "utterly_unknown"),
        (_row("not-a-real-state"), "error"),
        (SimpleNamespace(), "error"),  # no attributes at all
        (SimpleNamespace(state=None), None),
        (_row("failed"), ""),
    ],
)
def test_it_never_raises(row, kind):
    """REL-1/REL-9. It runs inside the result assembly for a FAILED run — the one request an operator
    makes when something has already gone wrong. Trading a diagnosis for a classification inverts the
    point of the unit."""
    api_main._classify_failure(row, kind)  # must not raise


def test_an_unrecognised_kind_defaults_and_logs(caplog):
    """REL-3. The value is the same default a bare ``error`` gets; the difference must not be silent."""
    with caplog.at_level(logging.WARNING, logger=api_main.logger.name):
        result = api_main._classify_failure(_row("failed", run_id="run-xyz"), "brand_new_kind")

    assert result == "artifact_defect"
    records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(records) == 1, f"exactly one record per classification, got {len(records)}"
    message = records[0].getMessage()
    assert "brand_new_kind" in message and "run-xyz" in message


def test_a_bare_error_logs_nothing(caplog):
    """The assertion that keeps REL-3's record meaningful.

    ``error`` is the most common failure value. If the fallback logged for it too, the record would fire
    on the ordinary case and an engineer scanning for the unrecognised-kind signal would find noise.
    """
    with caplog.at_level(logging.WARNING, logger=api_main.logger.name):
        assert api_main._classify_failure(_row("failed"), "error") == "artifact_defect"

    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_fallback_and_the_bare_error_default_are_the_same_value():
    """REL-2. One default, reached two ways — two would be two things to keep in step, invisibly."""
    assert api_main._classify_failure(_row("failed"), "error") == api_main._classify_failure(
        _row("failed"), "unknown_to_anyone"
    )


# ---------------------------------------------------------------------------
# The envelope, and the surfaces
# ---------------------------------------------------------------------------


def test_the_envelope_carries_the_classification():
    envelope = api_main._build_failure_envelope(_row("failed"), [], "r1", "timeout")
    assert envelope is not None
    assert envelope["classification"] == "transient"
    assert (
        envelope["error_kind"] == "timeout"
    ), "the CAUSE field is kept alongside the NEXT-ACTION one"


def test_a_successful_run_gets_no_envelope_and_so_no_classification():
    """BR-7 / SEC-6 / PERF-3 in one assertion: the placement inherits the byte-identical guarantee."""
    assert api_main._build_failure_envelope(_row("completed"), [], "r1", None) is None


def test_the_classification_is_reproducible():
    """REL-5. No clock, no environment, no configuration — a later agent must get the same answer."""
    row = _row("failed")
    assert [api_main._classify_failure(row, "error") for _ in range(5)] == ["artifact_defect"] * 5


def test_the_known_kind_set_is_closed_and_excludes_the_unproduced_provider_error():
    """``diverged`` and ``decision_required`` are NOT classifications (BR-5, BR-9).

    Both are returned as a ``kind`` on a 409 from the step-execution route and neither is persisted, so
    a read-time classifier has no evidence to emit them from. If either ever appears in the transient
    set, someone has conflated a live-call condition with a run outcome.
    """
    assert api_main._TRANSIENT_ERROR_KINDS == {"timeout"}
    assert "provider_error" not in api_main._TRANSIENT_ERROR_KINDS
    assert "diverged" not in api_main._TRANSIENT_ERROR_KINDS
    assert "decision_required" not in api_main._TRANSIENT_ERROR_KINDS
