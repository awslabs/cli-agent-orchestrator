"""M3-D durable task/round occurrence seam (cond-0380)."""

from __future__ import annotations

import uuid

import pytest

from cli_agent_orchestrator.services import task_occurrence as occ

SESSION = "cao-m3d-a"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


def _incarnation(suffix="1", generation=None):
    return occ.EffectIncarnation(
        incarnation_id=f"inc-{suffix}",
        terminal_id=f"term-{suffix}",
        generation=generation or f"gen-{suffix}",
        lineage_id=f"lin-{suffix}",
        native_session_id=f"native-{suffix}",
    )


def _open(agent_id=None, *, round_index=0, occurrence_id=None, suffix="1", seed=None):
    return occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=occurrence_id or str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=agent_id or str(uuid.uuid4()),
            round_index=round_index,
            dispatch_digest=_DIGEST_A,
            incarnation=_incarnation(suffix),
            seed=seed or occ.EMPTY_SEED,
        )
    )


def _complete_seed():
    return occ.TaskSeed(
        occ.SEED_COMPLETE,
        summary_digest=_DIGEST_B,
        artifacts=(
            occ.ArtifactReference(
                artifact_id="notes",
                kind="markdown",
                reference="/tmp/notes.md",
                content_digest=_DIGEST_C,
            ),
        ),
        produced_by="supervisor",
    )


# ---------------------------------------------------------------------------
# identity: a generation is not a task id
# ---------------------------------------------------------------------------


def test_occurrence_id_is_distinct_from_the_effect_incarnation_it_records():
    record = _open()

    assert record["task_occurrence_id"] != record["incarnation_id"]
    assert record["task_occurrence_id"] != record["generation"]
    assert record["task_occurrence_id"] != record["terminal_id"]
    # ...and the exact effect is still recorded alongside it.
    assert record["incarnation_id"] == "inc-1"
    assert record["generation"] == "gen-1"
    assert record["native_session_id"] == "native-1"


def test_the_same_native_conversation_across_generations_is_two_occurrences():
    agent = str(uuid.uuid4())
    first = _open(agent, round_index=0, suffix="1")
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=first["task_occurrence_id"],
            expected_revision=first["revision"],
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    second = occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=agent,
            round_index=1,
            dispatch_digest=_DIGEST_B,
            # exact reincarnation: same native conversation, new generation
            incarnation=occ.EffectIncarnation(
                incarnation_id="inc-2",
                terminal_id="term-2",
                generation="gen-2",
                lineage_id="lin-1",
                native_session_id="native-1",
            ),
        )
    )

    assert second["task_occurrence_id"] != first["task_occurrence_id"]
    assert second["native_session_id"] == first["native_session_id"]


def test_open_adopts_an_exact_replay_and_refuses_a_changed_one():
    occurrence_id = str(uuid.uuid4())
    agent = str(uuid.uuid4())
    first = _open(agent, occurrence_id=occurrence_id)
    again = _open(agent, occurrence_id=occurrence_id)

    assert again["adopted"] is True
    assert again["task_occurrence_id"] == first["task_occurrence_id"]

    with pytest.raises(occ.TaskOccurrenceConflict, match="different immutable content"):
        occ.open_occurrence(
            occ.OpenRequest(
                task_occurrence_id=occurrence_id,
                session_name=SESSION,
                agent_id=agent,
                round_index=0,
                dispatch_digest=_DIGEST_B,
                incarnation=_incarnation("1"),
            )
        )


# ---------------------------------------------------------------------------
# one task execution authority, and finalized history isolation
# ---------------------------------------------------------------------------


def test_one_stable_agent_has_at_most_one_open_occurrence():
    agent = str(uuid.uuid4())
    _open(agent, round_index=0)

    with pytest.raises(occ.TaskOccurrenceConflict, match="already has open task occurrence"):
        _open(agent, round_index=1)


def test_stable_agent_reuse_never_reopens_a_finalized_occurrence():
    agent = str(uuid.uuid4())
    record = _open(agent)
    finalized = occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=record["revision"],
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    assert finalized["state"] == occ.STATE_FINALIZED

    with pytest.raises(occ.TaskOccurrenceConflict, match="is finalized"):
        occ.open_occurrence(
            occ.OpenRequest(
                task_occurrence_id=record["task_occurrence_id"],
                session_name=SESSION,
                agent_id=agent,
                round_index=0,
                dispatch_digest=_DIGEST_A,
                incarnation=_incarnation("1"),
            )
        )
    # The agent itself is free again; reuse opens a *new* occurrence.
    fresh = _open(agent, round_index=1, suffix="2")
    assert fresh["state"] == occ.STATE_OPEN
    assert fresh["task_occurrence_id"] != record["task_occurrence_id"]


def test_open_occurrence_for_agent_never_returns_a_finished_round():
    agent = str(uuid.uuid4())
    record = _open(agent)
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=record["revision"],
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )

    assert occ.open_occurrence_for_agent(agent) is None
    history = occ.occurrence_history(SESSION, agent)
    assert history["open"] is None
    assert [item["task_occurrence_id"] for item in history["finalized"]] == [
        record["task_occurrence_id"]
    ]


def test_a_round_index_is_recorded_once_per_agent():
    agent = str(uuid.uuid4())
    first = _open(agent, round_index=3)
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=first["task_occurrence_id"],
            expected_revision=first["revision"],
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )

    with pytest.raises(occ.TaskOccurrenceConflict, match="already recorded round 3"):
        _open(agent, round_index=3, suffix="2")


# ---------------------------------------------------------------------------
# current vs finalized preservation
# ---------------------------------------------------------------------------


def test_finalizing_copies_current_evidence_and_a_later_write_cannot_change_it():
    record = _open(seed=_complete_seed())
    boundary = occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=record["revision"],
            recorded_by="worker",
            report_digest=_DIGEST_B,
            checkpoint_digest=_DIGEST_C,
        )
    )
    finalized = occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=boundary["revision"],
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )

    assert finalized["finalized"]["report_digest"] == _DIGEST_B
    assert finalized["finalized"]["checkpoint_digest"] == _DIGEST_C
    assert finalized["finalized"]["boundary_digest"] == finalized["current"]["boundary_digest"]

    # A late worker write cannot rewrite what the finished round reported.
    with pytest.raises(occ.TaskOccurrenceConflict, match="immutable"):
        occ.record_boundary(
            occ.BoundaryRecord(
                task_occurrence_id=record["task_occurrence_id"],
                expected_revision=finalized["revision"],
                recorded_by="worker",
                report_digest=_DIGEST_A,
            )
        )
    after = occ.get_occurrence(record["task_occurrence_id"])
    assert after["finalized"]["report_digest"] == _DIGEST_B


def test_boundary_requires_evidence_and_is_compare_and_swap():
    record = _open()
    with pytest.raises(occ.TaskOccurrenceInvalid, match="report digest or a checkpoint"):
        occ.BoundaryRecord(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
        )

    first = occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    assert first["revision"] == 1
    with pytest.raises(occ.TaskOccurrenceConflict, match="moved to revision"):
        occ.record_boundary(
            occ.BoundaryRecord(
                task_occurrence_id=record["task_occurrence_id"],
                expected_revision=0,
                recorded_by="worker",
                checkpoint_digest=_DIGEST_C,
            )
        )


def test_an_exact_boundary_replay_adopts_rather_than_bumping_the_revision():
    record = _open()
    request = occ.BoundaryRecord(
        task_occurrence_id=record["task_occurrence_id"],
        expected_revision=0,
        recorded_by="worker",
        report_digest=_DIGEST_B,
    )
    first = occ.record_boundary(request)
    replay = occ.record_boundary(request)

    assert replay["adopted"] is True
    assert replay["revision"] == first["revision"]


def test_finalize_is_write_once_and_adopts_an_identical_replay():
    record = _open()
    request = occ.FinalizeRequest(
        task_occurrence_id=record["task_occurrence_id"],
        expected_revision=0,
        disposition=occ.DISPOSITION_ABANDONED,
        finalized_by="supervisor",
    )
    occ.finalize_occurrence(request)
    assert occ.finalize_occurrence(request)["adopted"] is True

    with pytest.raises(occ.TaskOccurrenceConflict, match="already finalized"):
        occ.finalize_occurrence(
            occ.FinalizeRequest(
                task_occurrence_id=record["task_occurrence_id"],
                expected_revision=1,
                disposition=occ.DISPOSITION_REPORTED,
                finalized_by="supervisor",
            )
        )


# ---------------------------------------------------------------------------
# seed completeness
# ---------------------------------------------------------------------------


def test_seed_quality_is_explicit_and_only_complete_starts_a_fresh_worker():
    assert _complete_seed().sufficient_for_fresh_start is True
    truncated = occ.TaskSeed(occ.SEED_TRUNCATED, summary_digest=_DIGEST_B)
    assert truncated.sufficient_for_fresh_start is False
    assert occ.EMPTY_SEED.sufficient_for_fresh_start is False


def test_an_empty_seed_cannot_carry_content_and_a_content_seed_cannot_be_bare():
    with pytest.raises(occ.TaskOccurrenceInvalid, match="empty seed carries no summary"):
        occ.TaskSeed(occ.SEED_EMPTY, summary_digest=_DIGEST_B)
    with pytest.raises(occ.TaskOccurrenceInvalid, match="must carry a summary digest"):
        occ.TaskSeed(occ.SEED_COMPLETE)


def test_seed_verdict_reports_truncation_as_insufficient_with_a_reason():
    record = _open(seed=occ.TaskSeed(occ.SEED_TRUNCATED, summary_digest=_DIGEST_B))
    verdict = occ.get_occurrence(record["task_occurrence_id"])["seed_verdict"]

    assert verdict["family"] == "current"
    assert verdict["quality"] == occ.SEED_TRUNCATED
    assert verdict["sufficient_for_fresh_start"] is False
    assert "truncated" in verdict["reason"]


def test_seed_verdict_reads_the_finalized_family_once_finalized():
    record = _open(seed=_complete_seed())
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=0,
            disposition=occ.DISPOSITION_REPORTED,
            finalized_by="supervisor",
        )
    )
    verdict = occ.get_occurrence(record["task_occurrence_id"])["seed_verdict"]

    assert verdict["family"] == "finalized"
    assert verdict["sufficient_for_fresh_start"] is True


def test_artifact_seed_digest_binds_the_exact_artifact_content():
    seed = _complete_seed()
    other = occ.TaskSeed(
        occ.SEED_COMPLETE,
        summary_digest=_DIGEST_B,
        artifacts=(
            occ.ArtifactReference(
                artifact_id="notes",
                kind="markdown",
                reference="/tmp/notes.md",
                content_digest=_DIGEST_A,
            ),
        ),
    )
    assert seed.artifact_seed_digest != other.artifact_seed_digest


# ---------------------------------------------------------------------------
# opaque versioned extensions
# ---------------------------------------------------------------------------


def _extension(occurrence_id, *, kind="future.completion-claim/v2", final=True, ext_id="x1"):
    return occ.ExtensionRecord(
        task_occurrence_id=occurrence_id,
        extension_id=ext_id,
        extension_kind=kind,
        extension_version="2",
        decider="cao-conductor",
        payload={"claim": "done", "unknown_field": [1, 2, 3]},
        claims_final=final,
    )


def test_an_unknown_future_completion_claim_is_preserved_verbatim_not_honoured():
    record = _open()
    attached = occ.attach_extension(_extension(record["task_occurrence_id"]))

    assert attached["recognized"] is False
    assert attached["claims_final"] is True
    assert attached["routing_state"] == occ.ROUTING_PENDING
    # Preserved byte-for-byte, and the occurrence is still open: a claim this
    # build cannot read does not close a round.
    assert attached["payload"] == {"claim": "done", "unknown_field": [1, 2, 3]}
    assert occ.get_occurrence(record["task_occurrence_id"])["state"] == occ.STATE_OPEN


def test_a_pending_extension_blocks_reporting_the_occurrence_complete():
    record = _open()
    occ.attach_extension(_extension(record["task_occurrence_id"]))

    with pytest.raises(occ.TaskOccurrenceConflict, match="awaiting their decider"):
        occ.finalize_occurrence(
            occ.FinalizeRequest(
                task_occurrence_id=record["task_occurrence_id"],
                expected_revision=0,
                disposition=occ.DISPOSITION_REPORTED,
                finalized_by="supervisor",
            )
        )
    # Abandoning is still allowed: that is not a claim about the extension.
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=0,
            disposition=occ.DISPOSITION_ABANDONED,
            finalized_by="supervisor",
        )
    )


def test_routing_an_extension_hands_it_to_its_decider_and_is_idempotent():
    record = _open()
    occ.attach_extension(_extension(record["task_occurrence_id"]))
    pending = occ.pending_extensions("cao-conductor", session_name=SESSION)
    assert [item["extension_id"] for item in pending] == ["x1"]

    routed = occ.route_extension(record["task_occurrence_id"], "x1", routed_by="supervisor")
    assert routed["routing_state"] == occ.ROUTING_ROUTED
    assert routed["routed_receipt"]
    again = occ.route_extension(record["task_occurrence_id"], "x1", routed_by="supervisor")
    assert again["adopted"] is True
    assert again["routed_receipt"] == routed["routed_receipt"]
    assert occ.pending_extensions("cao-conductor", session_name=SESSION) == []

    # Routing does not finalize, does not dispatch, and does not rewrite the
    # payload; the extension is exactly what arrived.
    after = occ.get_occurrence(record["task_occurrence_id"])
    assert after["state"] == occ.STATE_OPEN
    assert after["extensions"][0]["payload"] == {"claim": "done", "unknown_field": [1, 2, 3]}


def test_an_extension_replay_adopts_and_a_changed_payload_is_refused():
    record = _open()
    request = _extension(record["task_occurrence_id"])
    occ.attach_extension(request)
    assert occ.attach_extension(request)["adopted"] is True

    with pytest.raises(occ.TaskOccurrenceConflict, match="different payload"):
        occ.attach_extension(
            occ.ExtensionRecord(
                task_occurrence_id=record["task_occurrence_id"],
                extension_id="x1",
                extension_kind="future.completion-claim/v2",
                extension_version="2",
                decider="cao-conductor",
                payload={"claim": "not-done"},
                claims_final=True,
            )
        )


def test_pending_extensions_are_scoped_by_decider_and_session():
    record = _open()
    occ.attach_extension(_extension(record["task_occurrence_id"], ext_id="x1"))
    occ.attach_extension(
        occ.ExtensionRecord(
            task_occurrence_id=record["task_occurrence_id"],
            extension_id="x2",
            extension_kind="other.kind/v1",
            extension_version="1",
            decider="memory-curator",
            payload={},
        )
    )

    assert len(occ.pending_extensions()) == 2
    assert len(occ.pending_extensions("memory-curator")) == 1
    assert occ.pending_extensions("memory-curator", session_name="cao-other") == []


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def test_occurrence_ids_and_digests_are_validated_before_anything_is_written():
    with pytest.raises(occ.TaskOccurrenceInvalid, match="canonical lowercase UUID"):
        occ.OpenRequest(
            task_occurrence_id="not-a-uuid",
            session_name=SESSION,
            agent_id=str(uuid.uuid4()),
            round_index=0,
            dispatch_digest=_DIGEST_A,
            incarnation=_incarnation(),
        )
    with pytest.raises(occ.TaskOccurrenceInvalid, match="64 lowercase hex"):
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=str(uuid.uuid4()),
            round_index=0,
            dispatch_digest="short",
            incarnation=_incarnation(),
        )


def test_unknown_occurrence_reads_are_typed_not_found():
    with pytest.raises(occ.TaskOccurrenceNotFound):
        occ.get_occurrence(str(uuid.uuid4()))
