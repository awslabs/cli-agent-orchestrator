"""Golden-vector and rule tests for recovery receipt facts (T-LG-SQL fork side).

Every digest and byte count below is normative from the approved design;
the fixture inputs are the design's own normative fixture values.
"""

from __future__ import annotations

import hashlib

import pytest

from cli_agent_orchestrator.services import recovery_receipts as rr

PROVIDER = "codex"
NATIVE = "thr_0192a7b4"

# Normative golden chain: creation (chain_seq 1) -> route (chain_seq 2) ->
# first route segment, all for codex/thr_0192a7b4.
CREATION_PAYLOAD_BYTES = 293
CREATION_PAYLOAD_SHA = "a258aa554d77e9a7cd5e1291af9a7bf97ffef5ca70dd8ab3e04f7e521d01767a"
CREATION_RECEIPT_BYTES = 245
CREATION_RECEIPT_SHA = "a6cd2284e53db3ebfed7780219fc015db6bb212aa920ac1cc7471c9f1a950244"
ROUTE_PAYLOAD_BYTES = 663
ROUTE_PAYLOAD_SHA = "2af5437a1cb3831bc06f9c1448b768fed46d4a25065f723bb142a4e396b82d5e"
ROUTE_RECEIPT_BYTES = 304
ROUTE_RECEIPT_SHA = "dc9337604ec4e6db5f4b0fe0dd864b238ea94177081d41858729f3e7fd202d28"
SEGMENT_BYTES = 734
SEGMENT_SHA = "904830fa6e8587781e94e491b2410a84bb1a8f43e14af3a8f139c9248f71668c"


def _creation_payload() -> bytes:
    return rr.creation_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        provider_version="0.146.0",
        issuance_source="app_server_thread_start",
        obligation_generation="obgen-7c2e4a1b",
        task_id="self-heal-demo-task",
        run_id="run-0001",
        created_at="2026-07-23T11:00:00Z",
    )


def _route_kwargs() -> dict:
    return {
        "provider": PROVIDER,
        "native_id": NATIVE,
        "authority_status": "unobserved",
        "assigned_model": "gpt-5.6-sol",
        "assigned_effort": "xhigh",
        "assigned_policy_sha256": "7" * 64,
        "assigned_profile_sha256": "8" * 64,
        "assigned_config_sha256": "9" * 64,
        "requested_model": "gpt-5.6-sol",
        "requested_effort": "xhigh",
        "observed_model": None,
        "observed_effort": None,
        "protocol_version": None,
        "event_sequence": None,
        "native_turn_id": None,
        "attested_at": "2026-07-23T11:00:05Z",
    }


def test_creation_payload_vector() -> None:
    payload = _creation_payload()
    assert len(payload) == CREATION_PAYLOAD_BYTES
    assert rr.payload_digest(payload) == CREATION_PAYLOAD_SHA


def test_creation_receipt_vector() -> None:
    payload_sha = rr.payload_digest(_creation_payload())
    obj = rr.receipt_hash_object(
        provider=PROVIDER,
        native_id=NATIVE,
        chain_seq=1,
        kind="creation",
        prev_receipt_sha256=None,
        payload_sha256=payload_sha,
        at="2026-07-23T11:00:00Z",
    )
    from cli_agent_orchestrator.services.canonical_json import encode_canonical

    raw = encode_canonical(obj)
    assert len(raw) == CREATION_RECEIPT_BYTES
    assert hashlib.sha256(raw).hexdigest() == CREATION_RECEIPT_SHA


def test_route_payload_vector() -> None:
    payload = rr.route_payload(**_route_kwargs())
    assert len(payload) == ROUTE_PAYLOAD_BYTES
    assert rr.payload_digest(payload) == ROUTE_PAYLOAD_SHA


def test_route_receipt_vector() -> None:
    creation_sha = rr.receipt_digest(
        provider=PROVIDER,
        native_id=NATIVE,
        chain_seq=1,
        kind="creation",
        prev_receipt_sha256=None,
        payload_sha256=rr.payload_digest(_creation_payload()),
        at="2026-07-23T11:00:00Z",
    )
    route_sha = rr.payload_digest(rr.route_payload(**_route_kwargs()))
    obj = rr.receipt_hash_object(
        provider=PROVIDER,
        native_id=NATIVE,
        chain_seq=2,
        kind="route",
        prev_receipt_sha256=creation_sha,
        payload_sha256=route_sha,
        at="2026-07-23T11:00:05Z",
    )
    from cli_agent_orchestrator.services.canonical_json import encode_canonical

    raw = encode_canonical(obj)
    assert len(raw) == ROUTE_RECEIPT_BYTES
    assert hashlib.sha256(raw).hexdigest() == ROUTE_RECEIPT_SHA


def test_segment_vector() -> None:
    creation_sha = rr.receipt_digest(
        provider=PROVIDER,
        native_id=NATIVE,
        chain_seq=1,
        kind="creation",
        prev_receipt_sha256=None,
        payload_sha256=rr.payload_digest(_creation_payload()),
        at="2026-07-23T11:00:00Z",
    )
    route_sha = rr.receipt_digest(
        provider=PROVIDER,
        native_id=NATIVE,
        chain_seq=2,
        kind="route",
        prev_receipt_sha256=creation_sha,
        payload_sha256=rr.payload_digest(rr.route_payload(**_route_kwargs())),
        at="2026-07-23T11:00:05Z",
    )
    route_fields = _route_kwargs()
    obj = rr.segment_hash_object(
        provider=PROVIDER,
        native_id=NATIVE,
        receipt_sha256=route_sha,
        predecessor_segment_hash=None,
        native_turn_id=None,
        assigned_model=route_fields["assigned_model"],
        assigned_effort=route_fields["assigned_effort"],
        assigned_policy_sha256=route_fields["assigned_policy_sha256"],
        assigned_profile_sha256=route_fields["assigned_profile_sha256"],
        assigned_config_sha256=route_fields["assigned_config_sha256"],
        requested_model=route_fields["requested_model"],
        requested_effort=route_fields["requested_effort"],
        observed_model=None,
        observed_effort=None,
        protocol_version=None,
        event_sequence=None,
        authority_status="unobserved",
    )
    from cli_agent_orchestrator.services.canonical_json import encode_canonical

    raw = encode_canonical(obj)
    assert len(raw) == SEGMENT_BYTES
    assert hashlib.sha256(raw).hexdigest() == SEGMENT_SHA


# Edge payload vectors — one per remaining kind, from the normative
# fixture inputs (shared identity codex/thr_0192a7b4).

ROUTE_RECEIPT_FOR_EXTENDS = ROUTE_RECEIPT_SHA


def test_binding_payload_vector() -> None:
    payload = rr.binding_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        launch_nonce_digest="a" * 64,
        provider_process_identity="pid:4210/start:773f",
        tmux_incarnation="tmux:8605efd6:@3:%7",
        terminal_generation="gen-000042",
        worktree_realpath="/abs/wt/self-heal",
        repository="cao-conductor",
        head="2" * 40,
        assigned_route_digest="7" * 64,
        bound_at="2026-07-23T11:00:01Z",
    )
    assert len(payload) == 549
    assert rr.payload_digest(payload) == (
        "8f25792bcb473571a24735e349f698ffa8517bce48631a3f3d63164258df39ff"
    )


def test_rebind_payload_vector() -> None:
    payload = rr.binding_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        launch_nonce_digest="b" * 64,
        provider_process_identity="pid:5310/start:99ac",
        tmux_incarnation="tmux:8605efd6:@4:%9",
        terminal_generation="gen-000043",
        worktree_realpath="/abs/wt/self-heal",
        repository="cao-conductor",
        head="2" * 40,
        assigned_route_digest="7" * 64,
        bound_at="2026-07-23T12:00:01Z",
        rebind=True,
        supersedes_binding_seq=1,
    )
    assert len(payload) == 575
    assert rr.payload_digest(payload) == (
        "155a385afcdab1d6b4e3aca1c5e422ba49a2c77bc663da844d660fb9f9d253ea"
    )


def test_resume_attempt_payload_vector() -> None:
    payload = rr.resume_attempt_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        resume_attempt_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
        terminal_generation="gen-000043",
        extends_receipt_sha256=ROUTE_RECEIPT_FOR_EXTENDS,
        at="2026-07-23T12:00:00Z",
    )
    assert len(payload) == 311
    assert rr.payload_digest(payload) == (
        "284b0f8dd5898862e338efbbfda6a48a6b690665860357ea975c604178099592"
    )


def test_resume_success_payload_vector() -> None:
    payload = rr.resume_success_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        resume_attempt_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
        terminal_generation="gen-000043",
        native_verified_evidence_digest="c" * 64,
        at="2026-07-23T12:00:02Z",
    )
    assert len(payload) == 320
    assert rr.payload_digest(payload) == (
        "47086804b460200209928ba794c1471bac1bdac0da7565e7533539dd1d261dfd"
    )


def test_resume_refusal_payload_vector() -> None:
    payload = rr.resume_refusal_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        resume_attempt_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
        refusal_code=41,
        reason="capability-unsupported",
        at="2026-07-23T12:00:03Z",
    )
    assert len(payload) == 236
    assert rr.payload_digest(payload) == (
        "8ab4dd41021665eac27670b7b5259f9afee1f81ec95a3ca9479692ebbcf8869a"
    )


def test_closure_payload_vector() -> None:
    payload = rr.closure_payload(
        provider=PROVIDER,
        native_id=NATIVE,
        closure_kind="no-survivor",
        evidence_digest="4" * 64,
        at="2026-07-23T12:30:00Z",
    )
    assert len(payload) == 232
    assert rr.payload_digest(payload) == (
        "3d64a361e16e0a68ac2f518b27d8436350dbb6f4b2493ba4378eb80ddc65b63d"
    )


# ------------------------------------------------------------------ rules


def test_route_authority_lattice_partitions_completeness_and_match() -> None:
    base = _route_kwargs()
    # unobserved with any observed field present is refused
    with pytest.raises(rr.ReceiptFactError):
        rr.route_payload(**{**base, "native_turn_id": "turn-1"})
    # observed requires all five and observation == assignment
    with pytest.raises(rr.ReceiptFactError):
        rr.route_payload(**{**base, "authority_status": "observed"})
    observed = {
        **base,
        "authority_status": "observed",
        "native_turn_id": "turn-1",
        "observed_model": "gpt-5.6-sol",
        "observed_effort": "xhigh",
        "protocol_version": "1",
        "event_sequence": 7,
    }
    rr.route_payload(**observed)
    # a complete mismatch stores truthfully as drifted
    drifted = {**observed, "authority_status": "drifted", "observed_model": "gpt-5.5"}
    rr.route_payload(**drifted)
    # drifted with full match is a mislabel and refused
    with pytest.raises(rr.ReceiptFactError):
        rr.route_payload(**{**observed, "authority_status": "drifted"})
    # degraded is partial only
    with pytest.raises(rr.ReceiptFactError):
        rr.route_payload(**{**observed, "authority_status": "degraded"})
    degraded = {**base, "authority_status": "degraded", "native_turn_id": "turn-9"}
    rr.route_payload(**degraded)


def test_genesis_rule_prev_null_exactly_at_chain_seq_1() -> None:
    with pytest.raises(rr.ReceiptFactError):
        rr.receipt_hash_object(
            provider=PROVIDER,
            native_id=NATIVE,
            chain_seq=1,
            kind="creation",
            prev_receipt_sha256="0" * 64,
            payload_sha256="1" * 64,
            at="2026-07-23T11:00:00Z",
        )
    with pytest.raises(rr.ReceiptFactError):
        rr.receipt_hash_object(
            provider=PROVIDER,
            native_id=NATIVE,
            chain_seq=2,
            kind="route",
            prev_receipt_sha256=None,
            payload_sha256="1" * 64,
            at="2026-07-23T11:00:00Z",
        )


def test_refusal_code_bounds() -> None:
    for code in (39, 46):
        with pytest.raises(rr.ReceiptFactError):
            rr.resume_refusal_payload(
                provider=PROVIDER,
                native_id=NATIVE,
                resume_attempt_id="7c9e6679-7425-40de-944b-e07fc1f90ae7",
                refusal_code=code,
                reason="x",
                at="2026-07-23T12:00:03Z",
            )


def test_binding_rebind_kind_separation() -> None:
    # supersedes_binding_seq is lawful only on a rebind; a plain binding
    # carrying it would silently rewrite binding history.
    kwargs = {
        "provider": PROVIDER,
        "native_id": NATIVE,
        "launch_nonce_digest": "a" * 64,
        "provider_process_identity": "pid:1/start:2",
        "tmux_incarnation": "tmux:x:@1:%1",
        "terminal_generation": "gen-1",
        "worktree_realpath": "/abs/wt",
        "repository": "repo",
        "head": "2" * 40,
        "assigned_route_digest": "7" * 64,
        "bound_at": "2026-07-23T11:00:01Z",
    }
    with pytest.raises(rr.ReceiptFactError):
        rr.binding_payload(**kwargs, supersedes_binding_seq=1)
    with pytest.raises(rr.ReceiptFactError):
        rr.binding_payload(**kwargs, rebind=True)


def test_hex_length_and_alphabet_enforced() -> None:
    with pytest.raises(rr.ReceiptFactError):
        rr.closure_payload(
            provider=PROVIDER,
            native_id=NATIVE,
            closure_kind="exit",
            evidence_digest="Z" * 64,
            at="2026-07-23T12:30:00Z",
        )


def test_unknown_kind_and_provider_refused() -> None:
    with pytest.raises(rr.ReceiptFactError):
        rr.receipt_hash_object(
            provider=PROVIDER,
            native_id=NATIVE,
            chain_seq=1,
            kind="mystery",
            prev_receipt_sha256=None,
            payload_sha256="1" * 64,
            at="2026-07-23T11:00:00Z",
        )
    with pytest.raises(rr.ReceiptFactError):
        rr.closure_payload(
            provider="gemini",
            native_id=NATIVE,
            closure_kind="exit",
            evidence_digest="4" * 64,
            at="2026-07-23T12:30:00Z",
        )
