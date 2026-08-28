"""Tests for the step result envelope (issue #583, unit ``result-envelope``).

Covers the three functions in ``services/step_result.py`` and the one additive column
``result_json`` on ``workflow_run_step``.

Two of these tests are the point of the file:

* ``test_credential_beyond_the_bound_is_still_redacted`` (and its straddling sibling) is the
  ONLY thing that catches a redact/bound ORDER SWAP (SR-1/BR-2). Under a swapped order the
  redactor sees only the kept prefix, so a credential in the dropped tail vanishes without
  ``redacted`` ever being set, and a credential straddling the boundary is cut in half —
  defeating the pattern match while persisting the surviving half.
* ``test_column_exists_after_migration`` exists because ``_migrate_workflow_run_step``
  swallows its own failure at debug level, so BR-8 requires the column be VERIFIED rather
  than assumed. A silent failure would otherwise surface far away as every settle losing its
  envelope, which the replay gate would read as crash-window rows and halt on.

OUT OF SCOPE HERE (unit 6, ``journal-step-lifecycle``): ``StepRow.result_json``, the two
``SELECT`` lists, and the write at settle. BR-1's unconditional write is provable only there,
on the ROW rather than on the call — this unit cannot round-trip through the journal and does
not try.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.database import _migrate_workflow_run_step
from cli_agent_orchestrator.constants import WORKFLOW_JOURNAL_RESULT_MAX_BYTES as BOUND
from cli_agent_orchestrator.models.workflow import StepResultEnvelope
from cli_agent_orchestrator.services.step_result import (
    build_envelope,
    parse_envelope,
    serialise_envelope,
)

# A synthetic value matching ``secret_gate``'s ``aws_access_key`` pattern
# (``(?:AKIA|ASIA)[0-9A-Z]{16}``). NOT a real credential: the body spells TESTONLY and the
# literal is assembled from two halves so a repository secret scanner does not flag the file.
_FAKE_AWS_KEY = "AKIA" + "TESTONLY0000ABCD"

# The marker ``redact_secrets`` substitutes for that pattern.
_MARKER = "[REDACTED:aws_access_key]"


# ---------------------------------------------------------------------------
# build_envelope — flags and bounding
# ---------------------------------------------------------------------------
def test_clean_short_message_sets_no_flags() -> None:
    """BR-4/INV-5: both flags are False when nothing was removed, and the text is verbatim."""
    env = build_envelope("step finished normally", "COMPLETED", "term-1")
    assert env.last_message == "step finished normally"
    assert env.status == "COMPLETED"
    assert env.terminal_id == "term-1"
    assert env.truncated is False
    assert env.redacted is False


def test_empty_message_is_legal() -> None:
    """Edge case (business-rules): empty is a legal result — an envelope, both flags False.

    A FAILED step may settle with no text at all (BR-1's corollary); that must not raise and
    must not be reported as lossy.
    """
    env = build_envelope("", "FAILED", None)
    assert env.last_message == ""
    assert env.status == "FAILED"
    assert env.truncated is False
    assert env.redacted is False


def test_over_bound_truncates_and_flags() -> None:
    """BR-3/SR-5/INV-2: bounding is lossy but TOTAL — truncate and flag, never raise."""
    env = build_envelope("a" * (BOUND + 5000), "COMPLETED", None)
    assert env.truncated is True
    assert env.redacted is False
    assert len(env.last_message.encode("utf-8")) == BOUND

    # Far over the bound: still an envelope, still no exception.
    huge = build_envelope("a" * (BOUND * 4), "COMPLETED", None)
    assert huge.truncated is True
    assert len(huge.last_message.encode("utf-8")) <= BOUND


def test_bound_is_inclusive() -> None:
    """Edge case: a message of exactly the bound is NOT truncated (the boundary is inclusive)."""
    exact = "a" * BOUND
    env = build_envelope(exact, "COMPLETED", None)
    assert env.truncated is False
    assert env.last_message == exact
    assert len(env.last_message.encode("utf-8")) == BOUND


def test_multibyte_truncation_does_not_exceed_bound() -> None:
    """TD-2: the bound is on BYTES, not characters, and a split character is dropped cleanly.

    ``€`` is 3 UTF-8 bytes and ``BOUND`` is not a multiple of 3, so the byte slice lands
    mid-character. ``errors="ignore"`` must drop the partial trailing sequence rather than
    raising or emitting U+FFFD. A character-counting implementation would sail past the byte
    bound here by ~3x, which is the failure this test names.
    """
    assert BOUND % 3 != 0, "fixture assumes the bound splits a 3-byte character"
    env = build_envelope("€" * (BOUND // 3 + 500), "COMPLETED", None)
    encoded_len = len(env.last_message.encode("utf-8"))
    assert env.truncated is True
    assert encoded_len <= BOUND
    assert encoded_len == BOUND - (BOUND % 3)  # the split character was dropped whole
    assert "�" not in env.last_message  # no replacement char smuggled in
    assert set(env.last_message) == {"€"}  # every surviving character is intact


# ---------------------------------------------------------------------------
# build_envelope — redaction (SR-1, SR-2, SR-4)
# ---------------------------------------------------------------------------
def test_credential_is_redacted_and_flagged() -> None:
    """SR-2: the credential is replaced by a marker and never echoed into the stored value."""
    env = build_envelope(f"connect failed using {_FAKE_AWS_KEY} as the key", "FAILED", "term-1")
    assert env.redacted is True
    assert env.truncated is False
    assert _FAKE_AWS_KEY not in env.last_message
    assert _MARKER in env.last_message
    # SR-2 asserts on the STORED value, not the return value: an envelope is persisted, so a
    # leak here would be durable.
    stored = serialise_envelope(env)
    assert _FAKE_AWS_KEY not in stored
    assert _MARKER in stored


def test_credential_beyond_the_bound_is_still_redacted() -> None:
    """SR-1/BR-2 — THE ORDER TEST. Redaction runs BEFORE bounding, always.

    The credential sits entirely PAST the byte bound. Redacting first removes it and records
    ``redacted=True``; the truncation that follows then drops the marker as well, so the
    surviving evidence is the flag. Bounding first would drop the credential unseen and leave
    ``redacted=False`` — a persisted envelope that silently claims nothing was removed.

    ``redacted is True`` is the assertion that fails on a swapped order. Keep it.
    """
    message = "a" * (BOUND + 100) + _FAKE_AWS_KEY
    env = build_envelope(message, "FAILED", "term-1")

    assert env.redacted is True  # <- fails if the order is ever swapped
    assert env.truncated is True
    assert _FAKE_AWS_KEY not in env.last_message
    assert "AKIA" not in env.last_message  # no surviving fragment of the match
    assert _FAKE_AWS_KEY not in serialise_envelope(env)


def test_credential_straddling_the_bound_is_still_redacted() -> None:
    """SR-1/BR-2 — the nastier half of the order rule: a credential CUT IN HALF.

    The credential starts 10 bytes before the bound, so a bound-first implementation would
    persist its first 10 characters (``AKIATESTON``) while the regex, seeing only that
    fragment, would not match — the match defeated and the remainder kept. A partial
    credential is not safe merely for being partial: an AWS key prefix is itself a signal.
    """
    message = "a" * (BOUND - 10) + _FAKE_AWS_KEY
    env = build_envelope(message, "FAILED", None)

    assert env.redacted is True
    assert env.truncated is True
    assert "AKIA" not in env.last_message
    assert _FAKE_AWS_KEY[:10] not in env.last_message
    assert "AKIA" not in serialise_envelope(env)


def test_fired_is_not_stored() -> None:
    """SR-4: only the boolean is persisted — never ``fired``'s pattern-name list.

    Redaction cascades, so ``fired`` can name a pattern that only matched an earlier
    ``[REDACTED:<name>]`` marker. A stored name list would look like precise evidence of which
    credential type was present without being that.
    """
    env = build_envelope(f"key={_FAKE_AWS_KEY}", "FAILED", None)
    assert isinstance(env.redacted, bool)
    assert not hasattr(env, "fired")

    payload = json.loads(serialise_envelope(env))
    assert set(payload) == {"last_message", "status", "terminal_id", "truncated", "redacted"}
    assert payload["redacted"] is True
    # No field anywhere in the persisted envelope carries a list of pattern names.
    assert not any(isinstance(v, list) for v in payload.values())


# ---------------------------------------------------------------------------
# parse_envelope — totality (BR-5, INV-3, SR-6)
# ---------------------------------------------------------------------------
def test_parse_none_returns_none() -> None:
    """BR-5: NULL means absent. Also the shape every pre-#583 row has (BR-10)."""
    assert parse_envelope(None) is None


def test_parse_corrupt_returns_none() -> None:
    """BR-5/INV-3: malformed JSON is treated exactly like absent, and never raises.

    A corrupt envelope must DECLINE to replay, not crash a resume.
    """
    for corrupt in ("{not json", '{"last_message": ', "", "   ", "\x00\x01"):
        assert parse_envelope(corrupt) is None


def test_parse_valid_json_wrong_shape_returns_none() -> None:
    """BR-5: valid JSON of the wrong shape is also absent — no partially-trusted envelope."""
    for wrong in ('{"a":1}', '{"last_message":"x"}', '"a bare string"', "null", "[]", "42"):
        assert parse_envelope(wrong) is None


def test_round_trip() -> None:
    """INV-2/INV-3: a clean envelope survives serialise -> parse unchanged."""
    env = build_envelope("all good", "COMPLETED", "term-7")
    parsed = parse_envelope(serialise_envelope(env))
    assert parsed == env
    assert parsed is not None
    assert parsed.last_message == "all good"
    assert parsed.status == "COMPLETED"
    assert parsed.terminal_id == "term-7"
    assert parsed.truncated is False
    assert parsed.redacted is False


def test_round_trip_preserves_flags() -> None:
    """BR-4/INV-5: the self-reported lossiness survives persistence, or FR-12 loses it."""
    env = build_envelope("a" * (BOUND + 1) + _FAKE_AWS_KEY, "FAILED", "term-9")
    parsed = parse_envelope(serialise_envelope(env))
    assert parsed is not None
    assert parsed.truncated is True
    assert parsed.redacted is True


def test_terminal_id_optional() -> None:
    """BR-6: ``None`` is a legal ``terminal_id`` (a handoff caller may reuse a terminal)."""
    env = build_envelope("done", "COMPLETED")
    assert env.terminal_id is None
    parsed = parse_envelope(serialise_envelope(env))
    assert parsed is not None
    assert parsed.terminal_id is None
    # And a present id is retained verbatim, even though it names a dead terminal on replay.
    kept = parse_envelope(serialise_envelope(build_envelope("done", "COMPLETED", "term-dead")))
    assert kept is not None
    assert kept.terminal_id == "term-dead"


def test_model_default_flags_are_false() -> None:
    """The model itself defaults both flags to False, so a hand-built envelope is not lossy."""
    env = StepResultEnvelope(last_message="x", status="COMPLETED")
    assert env.terminal_id is None
    assert env.truncated is False
    assert env.redacted is False


# ---------------------------------------------------------------------------
# The column (BR-7, BR-8, BR-10)
# ---------------------------------------------------------------------------
@pytest.fixture
def patched_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the migrator at a fresh temp DB (repo idiom, test_workflow_run_migration.py)."""
    db_path = tmp_path / "t.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    return db_path


def _step_columns(db_path: Path) -> dict:
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("PRAGMA table_info(workflow_run_step)").fetchall()
    return {r[1]: r for r in rows}  # (cid, name, type, notnull, dflt_value, pk)


def test_column_exists_after_migration(patched_db: Path) -> None:
    """BR-7/BR-8: the column is VERIFIED present, not assumed.

    ``_migrate_workflow_run_step`` wraps its whole body in ``except Exception`` ->
    ``logger.debug``, so a failed migration is SILENT and a returning migrator proves nothing.
    """
    _migrate_workflow_run_step()
    cols = _step_columns(patched_db)
    assert "result_json" in cols
    assert cols["result_json"][2] == "TEXT"
    assert cols["result_json"][3] == 0  # nullable
    # PRAGMA table_info reports the literal default expression as the string "NULL".
    assert cols["result_json"][4] == "NULL"
    # The pre-existing columns are untouched — BR-9: ``output_json`` keeps its own contract.
    assert "output_json" in cols
    assert "call_fingerprint" in cols


def test_migration_is_idempotent(patched_db: Path) -> None:
    """BR-7: a second run adds nothing, raises nothing, and preserves existing rows."""
    _migrate_workflow_run_step()
    before = set(_step_columns(patched_db))
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, updated_at) "
            "VALUES ('r1', 's1', 'completed', 1, '2026-01-01T00:00:00Z')"
        )
        conn.commit()

    _migrate_workflow_run_step()  # must be a no-op

    assert set(_step_columns(patched_db)) == before
    with sqlite3.connect(str(patched_db)) as conn:
        count = conn.execute("SELECT COUNT(*) FROM workflow_run_step").fetchone()[0]
    assert count == 1


def test_row_written_without_an_envelope_reads_as_absent(patched_db: Path) -> None:
    """BR-10: ``DEFAULT NULL`` means a row written without an envelope reads as absent.

    Every pre-#583 row has exactly this shape. It is safe rather than a gap: such a row's
    fingerprint is legacy-scheme or NULL, so FR-6 keeps it off the replay path too — the two
    guards agree instead of disagreeing.
    """
    _migrate_workflow_run_step()
    with sqlite3.connect(str(patched_db)) as conn:
        conn.execute(
            "INSERT INTO workflow_run_step "
            "(run_id, step_id, state, attempts, updated_at) "
            "VALUES ('r1', 's1', 'completed', 1, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        stored = conn.execute(
            "SELECT result_json FROM workflow_run_step WHERE run_id='r1' AND step_id='s1'"
        ).fetchone()[0]

    assert stored is None
    assert parse_envelope(stored) is None
