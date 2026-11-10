"""Self-test of the AG-UI 1.0 pre-check against the vendored upstream corpus.

The pre-check in ``test/_support/agui_conformance.py`` is a partial Python port of
``verifyEvents``. It earns its keep only if it tells a broken stream from a valid
one, so it must reproduce the upstream corpus's verdict on an explicit set of
fixtures: every MUST-FAIL fixture fails with the fixture's ``errorContains``, and
every MUST-PASS fixture passes. ``chunk-conflicting-tool-name-fatal`` is a negative
control: chunk rules are out of the port's scope, so it is expected NOT to be
detected, and the test pins that so the scope stays explicit.
"""

from __future__ import annotations

import json
from pathlib import Path
from test._support.agui_conformance import ConformanceError, check_lifecycle, validate_frames

import pytest

CORPUS = Path(__file__).resolve().parents[2] / "fixtures" / "agui_conformance_1_0" / "streams"

MUST_FAIL = [
    "first-event-not-run-started-fatal",
    "nested-run-started-fatal",
    "event-after-run-finished-fatal",
    "open-message-at-run-finished-fatal",
    "content-without-start-fatal",
    "step-finished-without-start-fatal",
    "run-finished-with-active-subagent-fatal",
    "subagent-finished-unstarted-fatal",
    "subagent-started-missing-name-fatal",
    "subagent-attribution-mismatch-fatal",
]

MUST_PASS = [
    "run-finished-cancelled-outcome-accepted",
    "run-finished-interrupt-outcome-accepted",
    "run-error-first-run-continues",
    "run-error-then-new-run-continues",
    "late-run-error-after-finished-run-continues",
    "two-sequential-runs-accumulate",
    "subagent-unannounced-attribution-accepted",
    "subagent-terminal-closes-open-chunk-stream",
    "triad-assembles-message",
    "tool-call-triad-assembles",
]

OUT_OF_SCOPE = ["chunk-conflicting-tool-name-fatal"]


def _load(name: str) -> dict:
    return json.loads((CORPUS / f"{name}.json").read_text(encoding="utf-8"))


def test_corpus_is_the_vendored_68_fixtures() -> None:
    assert len(list(CORPUS.glob("*.json"))) == 68
    manifest = (CORPUS / "MANIFEST.txt").read_text(encoding="utf-8").splitlines()
    assert len(manifest) == 75


@pytest.mark.parametrize("name", MUST_FAIL)
def test_port_fails_what_the_corpus_fails(name: str) -> None:
    fixture = _load(name)
    assert fixture["expect"]["outcome"] == "failed"  # premise: upstream says fatal
    with pytest.raises(ConformanceError) as err:
        check_lifecycle(fixture["stream"])
    assert fixture["expect"]["errorContains"] in str(err.value)


@pytest.mark.parametrize("name", MUST_PASS)
def test_port_passes_what_the_corpus_passes(name: str) -> None:
    fixture = _load(name)
    assert fixture["expect"]["outcome"] == "completed"  # premise: upstream accepts it
    check_lifecycle(fixture["stream"])


@pytest.mark.parametrize("name", OUT_OF_SCOPE)
def test_chunk_rules_are_out_of_scope(name: str) -> None:
    fixture = _load(name)
    assert fixture["expect"]["outcome"] == "failed"
    check_lifecycle(fixture["stream"])  # not detected by the port, by design


@pytest.mark.parametrize("name", MUST_PASS)
def test_validate_frames_accepts_the_corpus_streams(name: str) -> None:
    validate_frames(_load(name)["stream"])


def test_validate_frames_rejects_an_undescribed_snake_case_key() -> None:
    with pytest.raises(ConformanceError, match="thread_id"):
        validate_frames([{"type": "RUN_STARTED", "threadId": "t", "runId": "r", "thread_id": "t"}])


def test_validate_frames_rejects_a_payload_without_type() -> None:
    with pytest.raises(ConformanceError, match="no 'type'"):
        validate_frames([{"threadId": "t", "runId": "r"}])
