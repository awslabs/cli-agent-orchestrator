"""Deterministic tests for the dark M3-B4 exact-restore canary support."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from test.e2e import test_exact_executor_canary as installed_canary
from test.e2e.exact_canary import harness, matrix
from test.e2e.exact_canary.evidence import EvidenceSanitizer
from test.e2e.exact_canary.readiness import codex_composer_ready
from test.e2e.exact_canary.receipt import (
    RECEIPT_SCHEMA,
    CanaryReceiptInvalid,
    receipt_digest,
    validate_receipt,
)

import pytest

from cli_agent_orchestrator.services import exact_executor as xe
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import stable_agent_roster as roster


@pytest.fixture(autouse=True)
def _db(isolated_memory_db):
    return isolated_memory_db


def _receipt() -> dict:
    return {
        "schema": RECEIPT_SCHEMA,
        "canary_id": str(uuid.uuid4()),
        "recorded_at": "2026-08-15T12:00:00Z",
        "provider": "codex",
        "harness": "codex",
        "execution_mode": "native_tui",
        "installed": {
            "executable_path_sha256": "1" * 64,
            "executable_sha256": "2" * 64,
            "version_banner_sha256": "3" * 64,
            "normalized_version": "0.147.0",
        },
        "operation_id": str(uuid.uuid4()),
        "restore_contract_id": str(uuid.uuid4()),
        "restore_contract_digest": "4" * 64,
        "launch_material_digest": "5" * 64,
        "native_session_id_sha256": "6" * 64,
        "session_proof": "argv",
        "prior_generation_sha256": "7" * 64,
        "successor_generation_sha256": "8" * 64,
        "generation_changed": True,
        "effect_steps_observed": [
            "fence_prior",
            "reap_prior",
            "release_attachment",
            "acquire_native",
            "create_pane",
            "launch_resume",
            "verify_identity",
        ],
        "admit_input_absent": True,
        "task_bytes_sent": False,
        "outcome": "accepted",
        "error_class": None,
        "environment": {
            "tmux_server_socket_sha256": "9" * 64,
            "state_root_sha256": "a" * 64,
            "private_tmux": True,
            "shared_server_untouched": True,
        },
    }


def test_receipt_round_trips_with_a_deterministic_digest():
    payload = _receipt()

    assert validate_receipt(json.loads(json.dumps(payload))) == payload
    assert receipt_digest(payload) == receipt_digest(dict(reversed(list(payload.items()))))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(schema="cao-m3-exact-restore-canary-receipt-v0"),
        lambda value: value.update(unexpected="not part of the closed receipt"),
        lambda value: value.update(outcome="green-ish"),
        lambda value: value.update(task_bytes_sent=True),
        lambda value: value["effect_steps_observed"].append("admit_input"),
    ],
)
def test_receipt_rejects_schema_drift_and_false_green_claims(mutate):
    payload = _receipt()
    mutate(payload)

    with pytest.raises(CanaryReceiptInvalid):
        validate_receipt(payload)


def test_sanitizer_redacts_known_paths_accounts_and_common_secret_shapes(tmp_path):
    home = str(tmp_path / "operator-home")
    socket = str(tmp_path / "tmux" / "server.sock")
    socket_alias = (
        socket.removeprefix("/private")
        if socket.startswith("/private/var/folders/")
        else f"/private{socket}"
    )
    sanitizer = EvidenceSanitizer(
        {
            home: "<HOME>",
            socket: "<TMUX_SOCKET>",
            "colin": "<USER>",
        }
    )
    source = {
        "path": f"{home}/.codex/config.toml",
        "socket": socket,
        "private_socket_alias": socket_alias,
        "double_private_socket_alias": f"/private{socket_alias}",
        "user": "colin",
        "email": "person@example.com",
        "authorization": "Bearer eyJhbGciOi.secret.payload",
        "api_key": "sk-example0123456789abcdef",
        "launch_nonce": "a" * 64,
        "wrapped_temp": (
            "/private/var/folders/1p/random/T/pytest-of-colin/pytest-359/" "test_installed"
        ),
    }

    sanitized = sanitizer.sanitize_json(source)
    text = json.dumps(sanitized, sort_keys=True)

    assert home not in text
    assert socket not in text
    assert sanitized["private_socket_alias"] == "<TMUX_SOCKET>"
    assert sanitized["double_private_socket_alias"] == "<TMUX_SOCKET>"
    assert "colin" not in text
    assert "person@example.com" not in text
    assert "eyJhbGciOi.secret.payload" not in text
    assert "sk-example0123456789abcdef" not in text
    assert "a" * 64 not in text
    assert sanitized["launch_nonce"] == "<SECRET>"
    assert "/private/var/folders" not in text
    assert sanitized["wrapped_temp"] == "<TEMP_PATH>"
    assert sanitized["path"].startswith("<HOME>")


def test_installed_cell_matrix_uses_the_provider_proof_gates():
    assert matrix.assess_cell("codex", normalized_version="0.147.0").runnable
    assert not matrix.assess_cell("codex", normalized_version="0.145.0").runnable
    assert matrix.assess_cell("kimi_cli", normalized_version="0.34.0").runnable
    assert not matrix.assess_cell("kimi_cli", normalized_version="0.30.0").runnable
    assert not matrix.assess_cell(
        "muse_cli",
        normalized_version="0.1.0-R708.1",
        executable_sha256="0" * 64,
        version_banner="muse 0.1.0-R708.1",
    ).runnable
    assert matrix.assess_cell("claude_code", normalized_version="2.1.220").runnable
    # Unpinned: an unlisted Claude build is runnable — the SessionStart
    # hook is the runtime proof.  A failed observation is not.
    assert matrix.assess_cell("claude_code", normalized_version="2.1.232").runnable
    assert not matrix.assess_cell("claude_code", normalized_version="").runnable
    assert not matrix.assess_variation("codex", execution_mode="native_tui").runnable


def test_codex_and_kimi_canary_material_binds_the_composed_source_profile(tmp_path):
    record = {
        "terminal_id": "a1b2c3d4",
        "request": {"provider_executable_sha256": "a" * 64},
    }

    codex_profile, codex_material, codex_cell, codex_cell_digest = (
        installed_canary._source_profile_and_material(
            installed_canary.CODEX_CELL,
            state_root=tmp_path,
            record=record,
        )
    )
    kimi_profile, kimi_material, _, _ = installed_canary._source_profile_and_material(
        installed_canary.KIMI_CELL,
        state_root=tmp_path,
        record=record,
    )

    assert codex_profile == kimi_profile
    assert codex_profile["profile_ref"] == installed_canary.AGENT_PROFILE
    assert len(codex_profile["profile_sha256"]) == 64
    codex_args = codex_material["profile_args"]
    assert "--yolo" in codex_args
    assert "--no-alt-screen" in codex_args
    assert any(value.startswith("developer_instructions=") for value in codex_args)
    assert all(not value.startswith("projects=") for value in codex_args)
    assert "--model" not in codex_args
    assert xe._validate_launch_material(xe.LaunchMaterial(**codex_material)).profile_args == tuple(
        codex_args
    )
    assert codex_cell == "codex:gpt-5.6-terra:native_tui:0.147.0:composed-profile"
    assert len(codex_cell_digest) == 64
    # This profile has no Kimi permission flag; its composed profile digest is
    # still bound while MCP/home material rides the exact private-home lane.
    assert kimi_material == {"profile_args": []}


def test_codex_ready_gate_rejects_a_visible_composer_while_startup_is_active():
    transcript = """OpenAI Codex

• Starting MCP servers (7/12): apps (0s • esc to interrupt)

› Find and fix a bug in @filename

gpt-5.6-terra xhigh · /tmp/worktree · Context 100% left
"""

    assert not codex_composer_ready(transcript)


def test_codex_ready_gate_accepts_the_idle_composer_and_footer():
    transcript = """OpenAI Codex

⚠ MCP startup incomplete (failed: qmd)

› Summarize recent commits

gpt-5.6-terra xhigh · /tmp/worktree · Context 100% left
"""

    assert codex_composer_ready(transcript)


def _source(tmp_path) -> harness.CanarySource:
    workdir = os.path.realpath(tmp_path / "work")
    os.makedirs(workdir)
    binary = os.path.realpath(tmp_path / "codex")
    with open(binary, "wb") as handle:
        handle.write(b"#!/bin/sh\n")
    os.chmod(binary, 0o755)
    home = os.path.realpath(tmp_path / "codex-home")
    os.makedirs(home)
    return harness.CanarySource(
        session_name="cao-m3b4-unit",
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        agent_id=str(uuid.uuid4()),
        lineage_id=str(uuid.uuid4()),
        incarnation_id=str(uuid.uuid4()),
        native_session_id="11111111-2222-4333-8444-555555555555",
        role=roster.ROLE_WORKER,
        profile_family="canary",
        harness="codex",
        route_provenance={"provider_route": "openai"},
        execution_mode="native_tui",
        model="gpt-5.6-terra",
        effort="xhigh",
        working_directory=workdir,
        trusted_project_root=workdir,
        executable_path=binary,
        executable_sha256=hashlib.sha256(open(binary, "rb").read()).hexdigest(),
        provider_home_path=home,
    )


def test_contract_builder_refuses_a_noncanonical_workdir(tmp_path):
    source = _source(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(source.working_directory)
    source = harness.CanarySource(**{**source.__dict__, "working_directory": str(alias)})

    with pytest.raises(harness.CanaryHarnessInvalid, match="canonical"):
        harness.build_restore_contract(source)


def test_request_builder_reads_the_post_dormant_roster_revision(tmp_path):
    source = _source(tmp_path)
    bound = roster.bind_generation(
        roster.BindingContract(
            agent_id=source.agent_id,
            session_name=source.session_name,
            role=source.role,
            profile_family=source.profile_family,
            harness=source.harness,
            native_session_id=source.native_session_id,
            acquisition_method=roster.ACQUISITION_ZERO_TURN_BOOTSTRAP,
            route_provenance=source.route_provenance,
            terminal_id=source.terminal_id,
            generation=source.generation,
            execution_mode=source.execution_mode,
        )
    )
    source = harness.CanarySource.from_roster_and_launch(
        agent=bound["agent"],
        lineage=bound["lineage"],
        incarnation=bound["incarnation"],
        launch=source.launch_facts(),
    )
    contract = harness.build_restore_contract(source)
    stored = rc.publish_contract(contract)
    before = roster.get_agent(source.agent_id)["revision"]
    roster.transition_dormant(
        terminal_id=source.terminal_id,
        generation=source.generation,
        agent_id=source.agent_id,
        lineage_id=source.lineage_id,
        contract_digest=contract.digest(),
    )

    request = harness.build_operation_request(source, stored)

    assert request.roster_revision == before + 1
    assert request.lifecycle_observation == "working"
    assert request.lifecycle_epoch == 0
    assert request.compatibility_cell_ref is None
    assert request.compatibility_cell_digest is None
    assert request.restore_contract_schema == rc.SCHEMA_VERSION
    assert isinstance(request, oj.OperationRequest)
