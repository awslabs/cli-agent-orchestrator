"""The readiness receipt and the bind proof must agree on one field name.

Reproduced in production. A fresh Claude native launch built a healthy TUI,
Claude's own SessionStart hook named the exact native session, and the fork
durably published a complete ready receipt -- and every bind attempt still
returned HTTP 425 forever:

    {"detail": {"reason": "bind-bridge-not-durably-ready",
                "message": "... missing ['session_start_hook_id']"}}

``await_session_start`` publishes the provider-authored id as
``native_session_id``; ``_readiness_proof_fields`` asked for ``session_id``.
Two independent spellings of one cross-module contract, so the field was
silently always absent, the completeness rule always reported it missing,
and no real Claude launch could ever bind. Zero task bytes were delivered.

Every test here drives the REAL ``await_session_start`` over a real hook
file and feeds its ACTUAL return into the real consumer. That is the whole
point: the defect lived in the gap between a producer and a consumer that
were each individually correct and separately tested. A hand-authored
fixture spelled the consumer's way would have passed against the broken
code -- which is exactly how this shipped.
"""

from __future__ import annotations

import json
import uuid

import pytest

from cli_agent_orchestrator.services import claude_native_readiness as readiness
from cli_agent_orchestrator.services import managed_launch_v2 as v2

NATIVE_SESSION_ID = "de5d09ae-0558-48d7-a748-a27bc10df1ca"
GENERATION = "875f21c6-2ceb-47eb-8e57-df200aae4657"
TERMINAL_ID = "93ed2a73"
PANE_ID = "%31"


def _hook_file(tmp_path, session_id: str = NATIVE_SESSION_ID, **extra):
    """A SessionStart hook file in the shape Claude itself writes.

    Keyed ``session_id``, which is Claude's own spelling in the raw record.
    The receipt republishes it under its own key, and keeping the two
    namespaces distinct here is what makes this a real reproduction rather
    than a restatement of whichever spelling the code happens to use.
    """
    path = tmp_path / "readiness.jsonl"
    record = {
        "session_id": session_id,
        "transcript_path": str(tmp_path / "transcript.jsonl"),
        "source": "startup",
        "cwd": str(tmp_path),
        "model": "claude-opus-5",
    }
    record.update(extra)
    path.write_text(json.dumps(record) + "\n")
    return path


class _Row:
    """The durable reservation fields the proof assembler reads.

    ``execution_mode`` is load-bearing and was missing from the first
    version of this stub. Without it the row classifies as a legacy ACP
    reservation, ``_incomplete_readiness_fields`` short-circuits to ``[]``
    before it ever looks at a native field, and every assertion below
    passes against the broken producer too. A completeness test that never
    reaches the completeness rule is worse than no test: it reports the
    defect as fixed.
    """

    provider = "claude_code"
    terminal_id = TERMINAL_ID
    generation = GENERATION
    execution_mode = "native_tui"
    execution_mode_source = "request"


def _receipt_from_real_producer(tmp_path, **extra):
    """A readiness receipt whose session-start block is the real output.

    ``provider_session_start`` is assigned the return value verbatim, which
    is exactly what the launch path does.
    """
    session_start = readiness.await_session_start(
        _hook_file(tmp_path, **extra), NATIVE_SESSION_ID, timeout=1.0
    )
    return {
        "provider_session_start": session_start,
        "provider_session_start_proven": True,
        "provider_session_id": NATIVE_SESSION_ID,
        "model_input_ready": True,
        "model_input_ready_observation": {
            "provider_status": "idle",
            "pane_id": PANE_ID,
            "observed_at": "2026-07-25T23:25:03.792897Z",
        },
        "process_identity": {"pid": 4242, "start_marker": "Jul 25 23:24:59 2026"},
    }


class TestTheProducerAndConsumerAgreeOnTheFieldName:
    def test_the_receipt_publishes_the_id_under_the_named_key(self, tmp_path):
        """The producer half, read through the constant the consumer uses."""
        session_start = readiness.await_session_start(
            _hook_file(tmp_path), NATIVE_SESSION_ID, timeout=1.0
        )

        assert session_start[readiness.SESSION_START_ID_KEY] == NATIVE_SESSION_ID

    def test_the_bind_proof_carries_the_provider_authored_id(self, tmp_path):
        """The seam. This is the assertion the drift made impossible.

        Built from the real receipt, so it fails if either side moves.
        """
        receipt = _receipt_from_real_producer(tmp_path)

        fields = v2._readiness_proof_fields(_Row(), receipt, "native_tui")

        assert fields["session_start_hook_id"] == NATIVE_SESSION_ID

    def test_the_completeness_rule_reports_nothing_missing(self, tmp_path):
        """The exact production symptom: missing ['session_start_hook_id'].

        The bind refusal is computed from this list, so an empty list here
        is what "the durable receipt is complete enough to bind" means.
        """
        receipt = _receipt_from_real_producer(tmp_path)

        missing = v2._incomplete_readiness_fields(_Row(), receipt)

        assert "session_start_hook_id" not in missing
        assert missing == []

    def test_the_raw_hook_keeps_claudes_own_spelling(self, tmp_path):
        """The two namespaces stay distinct, and that is deliberate.

        Claude's record says ``session_id``; the receipt is a published
        statement rather than a copy of its input. Collapsing them would
        make the reproduction above vacuous -- the test would pass whatever
        the receipt published.
        """
        path = _hook_file(tmp_path)

        assert json.loads(path.read_text())["session_id"] == NATIVE_SESSION_ID
        assert readiness.SESSION_START_ID_KEY != "session_id"

    def test_the_key_is_pinned_to_the_durable_wire_spelling(self):
        """The constant's VALUE is a wire contract, not an internal name.

        Every other test here reads the constant on both sides, so renaming
        it to a third spelling keeps them all green -- while receipts
        already persisted on disk still carry ``native_session_id`` and
        would go back to failing the completeness rule forever, which is
        the exact 425 this fixes. Only a literal can catch that, so the
        literal is written down once, here.
        """
        assert readiness.SESSION_START_ID_KEY == "native_session_id"


class TestTheRefusalStillWorksWhenItShould:
    """The fix must not make the proof unfalsifiable.

    A completeness rule that reports nothing missing regardless is exactly
    as broken as one that always reports this field missing -- it just
    fails in the direction nobody notices.
    """

    def test_a_receipt_with_no_session_start_is_still_incomplete(self, tmp_path):
        receipt = _receipt_from_real_producer(tmp_path)
        receipt["provider_session_start"] = None

        missing = v2._incomplete_readiness_fields(_Row(), receipt)

        assert "session_start_hook_id" in missing

    def test_a_session_start_missing_the_id_is_still_incomplete(self, tmp_path):
        receipt = _receipt_from_real_producer(tmp_path)
        receipt["provider_session_start"].pop(readiness.SESSION_START_ID_KEY)

        missing = v2._incomplete_readiness_fields(_Row(), receipt)

        assert "session_start_hook_id" in missing

    def test_a_pane_that_is_not_input_ready_is_still_refused(self, tmp_path):
        """Unrelated to this field, and it must stay that way."""
        receipt = _receipt_from_real_producer(tmp_path)
        receipt["model_input_ready"] = False

        assert "input_ready" in v2._incomplete_readiness_fields(_Row(), receipt)


class TestTheProducerStillRefusesTheWrongSession:
    """The reason this field exists at all, unchanged by the rename.

    A hook naming a different session is the single most important thing
    this can catch: it means Claude opened a session other than the one it
    was told to, which is what a resume falling back to its interactive
    picker looks like from outside.
    """

    def test_a_different_session_id_never_produces_a_receipt(self, tmp_path):
        path = _hook_file(tmp_path, session_id=str(uuid.uuid4()))

        with pytest.raises(readiness.ClaudeNativeNotReady) as raised:
            readiness.await_session_start(path, NATIVE_SESSION_ID, timeout=0.3)

        assert "other sessions started instead" in str(raised.value)
        assert "No task bytes were submitted." in str(raised.value)

    def test_an_empty_hook_file_never_produces_a_receipt(self, tmp_path):
        path = tmp_path / "readiness.jsonl"
        path.write_text("")

        with pytest.raises(readiness.ClaudeNativeNotReady):
            readiness.await_session_start(path, NATIVE_SESSION_ID, timeout=0.3)


def test_the_row_stub_actually_reaches_the_completeness_rule():
    """Guards the trap above rather than trusting the stub stays right.

    A row that classifies as legacy ACP returns ``[]`` unconditionally, so
    every "nothing is missing" assertion here would hold no matter what
    the receipt said. This proves the fixture is a native row, by showing
    the rule still reports a genuinely absent field.
    """
    assert v2._incomplete_readiness_fields(_Row(), {}) != []
