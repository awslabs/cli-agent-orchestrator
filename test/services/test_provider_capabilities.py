"""Versioned truthful provider capability cells (cond-0377D).

A cell is green ONLY from an exact installed live canary receipt DERIVED
from the actual committed records: the migration operation/request, its
deterministic repair operation, the observation-attempt journal (exactly
one confirmed submitted observation and one matching verdict), the repair
evidence/request/evidence digest, provider/parser/interaction plan,
native identity, and the attachment/adoption outcome.  The installed
build identity is OBSERVED at record time: the service accepts a canonical
absolute executable path, computes the SHA-256 of that exact file itself,
and runs the bounded provider ``--version`` probe against it — a caller
digest or banner assertion is never authoritative.  Caller-supplied values
are never trusted for fields that can be derived.  Static parser support
without a matching live receipt is never green; Kimi without a session
stays unresolved/disabled and receives no synthetic turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Optional

import pytest

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import execution_mode as em
from cli_agent_orchestrator.services import legacy_identity_migration as lim
from cli_agent_orchestrator.services import native_attachment
from cli_agent_orchestrator.services import native_status_repair as nsr
from cli_agent_orchestrator.services import provider_capabilities as pc

CLAUDE_VERSION = "2.1.226"
SESSION_ID = "4f5f46c7-b660-4f6f-a144-d2c6dceccf95"

TERMINAL_ID = "a1b2c3d4"
OCCURRENCE = "00000000-0000-4000-8000-0000000000aa"

CLAUDE_BANNER = "Claude Code 2.1.226"
MUSE_BANNER = "Muse Code (0.1.0-R708.1)"


def _uuid() -> str:
    return str(uuid.uuid4())


def _write_executable(tmp_path, banner: str, *, tag: str = "probe", extra: str = "") -> str:
    """A real bounded provider --version probe target: a canonical absolute
    executable whose banner output is exactly ``banner``."""
    script = tmp_path / f"{tag}.sh"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' '{banner}'\n{extra}")
    script.chmod(0o755)
    return os.path.realpath(str(script))


def _sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_backing_chain(
    *,
    provider: str = "claude_code",
    parser_key: str = nsr.PARSER_CLAUDE_MODAL,
    version: str = CLAUDE_VERSION,
    session_id: str = SESSION_ID,
    migration_status: str = lim.MIGRATION_MIGRATED,
    journal_status: str = "observed",
    journal_count: int = 1,
    with_evidence: bool = True,
    with_attachment: bool = True,
) -> tuple[str, str, str, str]:
    """The actual committed records a green receipt must derive from.

    Returns (migration_operation_id, repair_operation_id, request_digest,
    evidence_sha256).
    """
    migration_op = _uuid()
    repair_op = lim._repair_operation_id(migration_op)
    req_digest = "c" * 64
    evidence = "b" * 64
    stamp = "2026-08-10T00:00:00Z"
    with database.SessionLocal() as db:
        db.add(
            database.LegacyIdentityMigrationModel(
                migration_operation_id=migration_op,
                request_digest="m" * 64,
                terminal_id=TERMINAL_ID,
                provider=provider,
                generation=None,
                physical_occurrence=OCCURRENCE,
                provider_version=version,
                audit_occurrence_id="audit-1",
                audit_candidate_digest="d" * 64,
                repair_operation_id=repair_op,
                status=migration_status,
                repair_status=(
                    nsr.STATUS_REPAIRED if migration_status == lim.MIGRATION_MIGRATED else None
                ),
                native_session_id=session_id if with_evidence else None,
                evidence_sha256=evidence if with_evidence else None,
                parser_key=parser_key if with_evidence else None,
                outcome_json="{}",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.add(
            database.NativeStatusObservationAttemptModel(
                operation_id=repair_op,
                request_digest=req_digest,
                terminal_id=TERMINAL_ID,
                generation=OCCURRENCE,
                provider=provider,
                status=journal_status,
                status_action_count=journal_count,
                observed_at=stamp,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        if with_evidence:
            db.add(
                database.NativeStatusRepairEvidenceModel(
                    operation_id=repair_op,
                    request_digest=req_digest,
                    terminal_id=TERMINAL_ID,
                    generation=OCCURRENCE,
                    provider=provider,
                    provider_version=version,
                    native_session_id=session_id,
                    parser_key=parser_key,
                    evidence_sha256=evidence,
                    observed_at=stamp,
                    created_at=stamp,
                )
            )
        if with_attachment:
            db.add(
                database.NativeSessionAttachmentModel(
                    provider=provider,
                    native_session_id=session_id,
                    state=native_attachment.ATTACHED,
                    owner_terminal_id=TERMINAL_ID,
                    owner_generation=OCCURRENCE,
                    owner_execution_mode=em.NATIVE_TUI,
                    owner_pane_id="%7",
                    owner_process_identity_json=json.dumps(
                        {"pid": 4242, "start_marker": "Thu Jul 24 10:00:00 2026"}
                    ),
                    intent_json=json.dumps(
                        {
                            "schema": "cao-native-attachment-intent-v1",
                            "acquisition_method": native_attachment.ACQUISITION_STATUS_DISCOVERED,
                        }
                    ),
                    adoption_receipt_json=json.dumps(
                        {
                            "schema": native_attachment.STATUS_REPAIR_ADOPTION_SCHEMA,
                            "operation_id": repair_op,
                            "provider": provider,
                            "native_session_id": session_id,
                            "terminal_id": TERMINAL_ID,
                            "generation": OCCURRENCE,
                            "execution_mode": em.NATIVE_TUI,
                        }
                    ),
                    epoch=0,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        db.commit()
    return migration_op, repair_op, req_digest, evidence


def _receipt(
    provider: str = "claude_code",
    *,
    canary_id: Optional[str] = None,
    migration_operation_id: Optional[str] = None,
    executable_path: str,
    state: str = pc.CANARY_STATE_OK,
    recorded_at: Optional[str] = None,
) -> dict[str, Any]:
    return pc.record_provider_canary_receipt(
        canary_id=canary_id or _uuid(),
        provider=provider,
        migration_operation_id=migration_operation_id,
        executable_path=executable_path,
        state=state,
        recorded_at=recorded_at,
    )


def _cell(provider: str) -> dict[str, Any]:
    cells = pc.provider_capability_cells()
    return next(c for c in cells["providers"] if c["provider"] == provider)


class TestObservedInstalledBuild:
    def test_green_receipt_requires_real_executable_observation(self, isolated_memory_db, tmp_path):
        """Red gate: a fabricated digest with no real executable must never
        make a cell green — the service computes the SHA-256 of the exact
        file itself and probes its --version banner."""
        migration_op, _, _, _ = _seed_backing_chain()
        # A non-existent executable path is refused.
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(
                migration_operation_id=migration_op,
                executable_path="/nonexistent/canonical/absolute/probe.sh",
            )
        # A plausible banner with no backing executable observation is refused.
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=_uuid(), executable_path=str(tmp_path / "x.sh"))
        # A real executable whose --version banner does not match the
        # panel-attested build is refused.
        drift = _write_executable(tmp_path, "Claude Code 9.9.9", tag="drift")
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=migration_op, executable_path=drift)
        # The real chain with a real executable records, and the digest is
        # the one the service computed from the file — never caller input.
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        receipt = _receipt(migration_operation_id=migration_op, executable_path=probe)
        assert receipt["installed_build_sha256"] == _sha256_of(probe)
        assert receipt["installed_build_banner"] == CLAUDE_BANNER
        assert receipt["executable_path"] == probe
        cell = _cell("claude_code")
        assert cell["cell_state"] == pc.CELL_ENABLED
        installed = cell["build_identity"]["installed_build"]
        assert installed["sha256"] == _sha256_of(probe)
        assert installed["banner"] == CLAUDE_BANNER

    def test_muse_inner_executable_banner_is_retained(self, isolated_memory_db, tmp_path):
        migration_op, _, _, _ = _seed_backing_chain(
            provider="muse_cli", parser_key=nsr.PARSER_MUSE_PANEL, version="0.1.0"
        )
        probe = _write_executable(tmp_path, MUSE_BANNER, tag="muse")
        receipt = _receipt(
            provider="muse_cli", migration_operation_id=migration_op, executable_path=probe
        )
        assert receipt["installed_build_banner"] == MUSE_BANNER
        assert receipt["installed_build_sha256"] == _sha256_of(probe)
        cell = _cell("muse_cli")
        installed = cell["build_identity"]["installed_build"]
        # The full R revision survives: never normalized away.
        assert installed["banner"] == MUSE_BANNER
        assert installed["normalized"] == "0.1.0"
        assert cell["cell_state"] == pc.CELL_ENABLED

    def test_changed_executable_under_same_canary_id_conflicts(self, isolated_memory_db, tmp_path):
        migration_op, _, _, _ = _seed_backing_chain()
        canary_id = _uuid()
        probe_a = _write_executable(tmp_path, CLAUDE_BANNER, tag="a")
        first = _receipt(
            canary_id=canary_id, migration_operation_id=migration_op, executable_path=probe_a
        )
        # Response-loss retry with the exact same executable adopts.
        second = _receipt(
            canary_id=canary_id, migration_operation_id=migration_op, executable_path=probe_a
        )
        assert second["request_digest"] == first["request_digest"]
        # A different executable (different bytes, different digest) conflicts.
        probe_b = _write_executable(tmp_path, CLAUDE_BANNER, tag="b")
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(
                canary_id=canary_id,
                migration_operation_id=migration_op,
                executable_path=probe_b,
            )

    def test_failed_receipt_requires_no_backing(self, isolated_memory_db, tmp_path):
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        failed = _receipt(
            state=pc.CANARY_STATE_FAILED,
            executable_path=probe,
            recorded_at="2026-08-10T00:00:00Z",
        )
        assert failed["state"] == pc.CANARY_STATE_FAILED
        assert failed["installed_build_sha256"] == _sha256_of(probe)
        cell = _cell("claude_code")
        assert cell["canary"]["state"] == "failed"
        assert cell["cell_state"] == pc.CELL_DISABLED


class TestDerivedGreenReceipt:
    def test_green_receipt_without_backing_records_is_refused(self, isolated_memory_db, tmp_path):
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=_uuid(), executable_path=probe)
        migration_op, _, _, _ = _seed_backing_chain()
        receipt = _receipt(migration_operation_id=migration_op, executable_path=probe)
        assert receipt["receipt_schema"] == pc.CANARY_RECEIPT_SCHEMA

    def test_green_receipt_derives_every_field_from_committed_records(
        self, isolated_memory_db, tmp_path
    ):
        migration_op, repair_op, req_digest, evidence = _seed_backing_chain()
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        receipt = _receipt(migration_operation_id=migration_op, executable_path=probe)
        # Derived, never caller-supplied:
        assert receipt["operation_id"] == repair_op
        assert receipt["migration_request_digest"] == "m" * 64
        assert receipt["evidence_request_digest"] == req_digest
        assert receipt["evidence_sha256"] == evidence
        assert receipt["native_session_id"] == SESSION_ID
        assert receipt["status_action_count"] == 1
        assert receipt["parser_key"] == nsr.PARSER_CLAUDE_MODAL
        assert receipt["attachment_outcome"] == "attached"

        cell = _cell("claude_code")
        assert cell["status_observation_repair_code_supported"] is True
        assert cell["installed_live_repair_proven"] is True
        assert cell["cell_state"] == pc.CELL_ENABLED
        assert cell["canary"]["state"] == "matching"
        assert cell["canary"]["operation_id"] == repair_op
        assert cell["canary"]["migration_operation_id"] == migration_op
        assert cell["canary"]["evidence_sha256"] == evidence
        assert cell["canary"]["status_action_count"] == 1

    def test_zero_action_and_kimi_still_missing_are_non_green(self, isolated_memory_db, tmp_path):
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        migration_op, _, _, _ = _seed_backing_chain(journal_status="submitted", journal_count=1)
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=migration_op, executable_path=probe)
        kimi_op, _, _, _ = _seed_backing_chain(
            provider="kimi_cli",
            parser_key=nsr.PARSER_KIMI_STATUS,
            version="0.34.0",
            session_id="session_4f5f46c7-b660-4f6f-a144-d2c6dceccf95",
            migration_status=lim.MIGRATION_IDENTITY_STILL_MISSING,
            journal_status="identity-still-missing",
            with_evidence=False,
            with_attachment=False,
        )
        kimi_probe = _write_executable(tmp_path, "Kimi Code (v0.34.0)", tag="kimi")
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(
                provider="kimi_cli",
                migration_operation_id=kimi_op,
                executable_path=kimi_probe,
            )
        kimi = _cell("kimi_cli")
        assert kimi["cell_state"] == pc.CELL_UNRESOLVED
        assert kimi["installed_live_repair_proven"] is False

    def test_mismatched_backing_rows_remain_disabled(self, isolated_memory_db, tmp_path):
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        refused_op, _, _, _ = _seed_backing_chain(migration_status=lim.MIGRATION_REFUSED)
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=refused_op, executable_path=probe)
        broken_op, _, _, _ = _seed_backing_chain(with_evidence=False, with_attachment=False)
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=broken_op, executable_path=probe)
        no_att_op, _, _, _ = _seed_backing_chain(with_attachment=False)
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(migration_operation_id=no_att_op, executable_path=probe)

    def test_receipt_exact_duplicate_adopts_changed_content_conflicts(
        self, isolated_memory_db, tmp_path
    ):
        migration_op, _, _, _ = _seed_backing_chain()
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        canary_id = _uuid()
        first = _receipt(
            canary_id=canary_id, migration_operation_id=migration_op, executable_path=probe
        )
        second = _receipt(
            canary_id=canary_id, migration_operation_id=migration_op, executable_path=probe
        )
        assert second["request_digest"] == first["request_digest"]
        other = _write_executable(tmp_path, CLAUDE_BANNER, tag="other")
        with pytest.raises(nsr.NativeStatusRepairConflict):
            _receipt(
                canary_id=canary_id, migration_operation_id=migration_op, executable_path=other
            )


class TestCapabilityCells:
    def test_static_parser_support_without_receipt_is_never_green(self, isolated_memory_db):
        cells = pc.provider_capability_cells()
        assert cells["schema"] == pc.PROVIDER_CAPABILITY_SCHEMA
        for provider in ("claude_code", "codex", "muse_cli"):
            cell = next(c for c in cells["providers"] if c["provider"] == provider)
            assert cell["status_observation_repair_code_supported"] is True
            assert cell["installed_live_repair_proven"] is False
            assert cell["cell_state"] == pc.CELL_DISABLED
            assert cell["canary"]["present"] is False
            assert cell["canary"]["state"] == "absent"
            assert cell["reason"]
        kimi = next(c for c in cells["providers"] if c["provider"] == "kimi_cli")
        assert kimi["cell_state"] == pc.CELL_UNRESOLVED
        assert "session" in kimi["reason"]
        assert kimi["installed_live_repair_proven"] is False

    def test_unsupported_and_unavailable_are_explicit(self, isolated_memory_db, monkeypatch):
        cell = pc._provider_capability_cell("kiro_cli")
        assert cell["cell_state"] == pc.CELL_UNSUPPORTED
        assert cell["status_observation_repair_code_supported"] is False

        def _boom(provider: str) -> None:
            raise RuntimeError("store down")

        monkeypatch.setattr(pc, "_latest_canary_receipt", _boom)
        assert pc._provider_capability_cell("claude_code")["cell_state"] == pc.CELL_UNAVAILABLE

    def test_route_provenance_note_and_harness_domains(self, isolated_memory_db):
        cells = pc.provider_capability_cells()
        claude = next(c for c in cells["providers"] if c["provider"] == "claude_code")
        assert claude["route_provenance_domains"] == ["deepseek", "zai"]
        assert claude["harness_domain"] == "claude_code"
        codex = next(c for c in cells["providers"] if c["provider"] == "codex")
        assert codex["route_provenance_domains"] == []
        assert "deepseek" in cells["route_provenance_note"]
        assert {c["provider"] for c in cells["providers"]} == {
            "claude_code",
            "codex",
            "kimi_cli",
            "muse_cli",
        }

    def test_parser_support_exposes_plan_and_schema_versions(self, isolated_memory_db):
        cell = _cell("claude_code")
        assert cell["parser_support"]["parser_key"] == nsr.PARSER_CLAUDE_MODAL
        assert cell["parser_support"]["capability_schema"] == nsr.REPAIR_SCHEMA
        assert cell["parser_support"]["supported_builds"] == [CLAUDE_VERSION]
        # Static parser support appears only under parser_support; the
        # build_identity carries no static durable_builds copy.
        assert "durable_builds" not in cell["build_identity"]
        assert cell["parser_support"]["escape"] is True
        plans = nsr.repair_parser_plans()
        assert plans["claude_code"]["parser_key"] == nsr.PARSER_CLAUDE_MODAL

    def test_newest_receipt_wins(self, isolated_memory_db, tmp_path):
        migration_op, _, _, _ = _seed_backing_chain()
        probe_a = _write_executable(tmp_path, CLAUDE_BANNER, tag="first")
        probe_b = _write_executable(tmp_path, CLAUDE_BANNER, tag="second")
        _receipt(
            migration_operation_id=migration_op,
            executable_path=probe_a,
            recorded_at="2026-08-10T00:00:00Z",
        )
        _receipt(
            migration_operation_id=migration_op,
            executable_path=probe_b,
            recorded_at="2026-08-10T00:00:01Z",
        )
        cell = _cell("claude_code")
        assert cell["canary"]["state"] == "matching"
        assert cell["build_identity"]["installed_build"]["sha256"] == _sha256_of(probe_b)


class TestReadTimeRevalidation:
    def test_capability_read_revalidates_installed_bytes(self, isolated_memory_db, tmp_path):
        """Green receipts are revalidated on EVERY read: replacing the
        executable bytes (same semver banner) makes the next GET non-green;
        unchanged bytes stay green."""
        migration_op, _, _, _ = _seed_backing_chain()
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        _receipt(migration_operation_id=migration_op, executable_path=probe)
        assert _cell("claude_code")["cell_state"] == pc.CELL_ENABLED

        # Replace the executable contents but keep the same semver banner.
        probe_path = Path(probe)
        probe_path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{CLAUDE_BANNER}'\n# replaced\n")
        probe_path.chmod(0o755)
        cell = _cell("claude_code")
        assert cell["cell_state"] != pc.CELL_ENABLED
        assert cell["canary"]["state"] == "stale"
        assert cell["installed_live_repair_proven"] is False
        assert "digest" in cell["reason"] or "no longer" in cell["reason"]

        # Restoring the exact bytes restores green.
        probe_path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{CLAUDE_BANNER}'\n")
        probe_path.chmod(0o755)
        assert _cell("claude_code")["cell_state"] == pc.CELL_ENABLED

    def test_capability_read_detects_disappeared_and_drifted_executable(
        self, isolated_memory_db, tmp_path
    ):
        migration_op, _, _, _ = _seed_backing_chain()
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        _receipt(migration_operation_id=migration_op, executable_path=probe)
        assert _cell("claude_code")["cell_state"] == pc.CELL_ENABLED

        # Disappearance / non-executable -> typed stale, never an exception.
        Path(probe).unlink()
        cell = _cell("claude_code")
        assert cell["cell_state"] != pc.CELL_ENABLED
        assert cell["canary"]["state"] == "stale"

        # Full-banner drift (different semver) -> non-green: record a valid
        # receipt, then make the executable's --version banner drift.
        probe2 = _write_executable(tmp_path, CLAUDE_BANNER, tag="drift2")
        _receipt(
            migration_operation_id=migration_op,
            executable_path=probe2,
            recorded_at="2026-08-10T00:00:03Z",
        )
        assert _cell("claude_code")["cell_state"] == pc.CELL_ENABLED
        probe2_path = Path(probe2)
        probe2_path.write_text("#!/bin/sh\nprintf '%s\\n' 'Claude Code 9.9.9'\n")
        probe2_path.chmod(0o755)
        cell = _cell("claude_code")
        assert cell["cell_state"] != pc.CELL_ENABLED
        assert cell["canary"]["state"] == "stale"


class TestServerAuthoredReceiptAuthority:
    def test_server_created_at_is_ordering_authority(self, isolated_memory_db, tmp_path):
        """A future-dated caller recorded_at never shadows a newer
        server-created receipt: created_at + canary-id tie-break decide."""
        migration_op, _, _, _ = _seed_backing_chain()
        older = _write_executable(tmp_path, CLAUDE_BANNER, tag="older", extra="# older\n")
        newer = _write_executable(tmp_path, CLAUDE_BANNER, tag="newer", extra="# newer\n")
        # The OLDER server-created row carries a FUTURE caller timestamp.
        _receipt(
            migration_operation_id=migration_op,
            executable_path=older,
            recorded_at="2099-01-01T00:00:00Z",
        )
        _receipt(
            migration_operation_id=migration_op,
            executable_path=newer,
            recorded_at="2026-08-10T00:00:00Z",
        )
        # Pin the SERVER-authored created_at deterministically: the future
        # caller timestamp on the older row must never win.
        with database.SessionLocal() as db:
            rows = db.query(database.ProviderCanaryReceiptModel).all()
            for row in rows:
                row.created_at = (
                    "2026-08-10T00:00:00Z"
                    if row.executable_path == older
                    else "2026-08-10T00:00:01Z"
                )
            db.commit()
        cell = _cell("claude_code")
        assert cell["canary"]["state"] == "matching"
        assert cell["build_identity"]["installed_build"]["sha256"] == _sha256_of(newer)

    def test_concurrent_exact_canary_recording_adopts_the_winner(
        self, isolated_memory_db, tmp_path, monkeypatch
    ):
        """Two identical callers racing the same canary id: the primary-key
        loser adopts the winner's exact receipt instead of surfacing a raw
        integrity error; changed content still conflicts."""
        import threading

        migration_op, _, _, _ = _seed_backing_chain()
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        canary_id = _uuid()

        # Deterministic barrier: the first caller pauses AFTER its
        # existing-row check but BEFORE its insert, so the second caller
        # completes first and the first caller's insert hits the PK race.
        real_factory = pc.database.SessionLocal
        add_blocked = threading.Event()
        release_add = threading.Event()

        class _BlockingSession:
            def __init__(self) -> None:
                self._inner = real_factory()

            def __getattr__(self, name: str) -> Any:
                return getattr(self._inner, name)

            def __enter__(self) -> "_BlockingSession":
                return self

            def __exit__(self, *args: Any) -> None:
                self._inner.close()

            def add(self, *args: Any, **kwargs: Any) -> Any:
                if not add_blocked.is_set():
                    add_blocked.set()
                    release_add.wait(timeout=60)
                return self._inner.add(*args, **kwargs)

        monkeypatch.setattr(pc.database, "SessionLocal", lambda: _BlockingSession())

        results: dict[str, Any] = {}

        def _caller() -> None:
            results["outcome"] = _receipt(
                canary_id=canary_id, migration_operation_id=migration_op, executable_path=probe
            )

        t1 = threading.Thread(target=_caller)
        t1.start()
        assert add_blocked.wait(timeout=30)
        # Caller 2 (main thread) completes first and wins the primary key.
        winner = _receipt(
            canary_id=canary_id, migration_operation_id=migration_op, executable_path=probe
        )
        release_add.set()
        t1.join(timeout=30)
        assert results["outcome"]["request_digest"] == winner["request_digest"]
        with database.SessionLocal() as db:
            rows = (
                db.query(database.ProviderCanaryReceiptModel).filter_by(canary_id=canary_id).all()
            )
            assert len(rows) == 1

    def test_build_identity_has_no_static_durable_builds_field(self, isolated_memory_db, tmp_path):
        """Static parser support appears ONLY under parser_support; the
        installed identity carries the canonical executable path and is null
        without an observed canary."""
        cells = pc.provider_capability_cells()
        claude = next(c for c in cells["providers"] if c["provider"] == "claude_code")
        assert "durable_builds" not in claude["build_identity"]
        assert claude["build_identity"]["installed_build"] is None
        assert claude["build_identity"]["installed_build_source"] is None
        assert claude["parser_support"]["supported_builds"] == [CLAUDE_VERSION]

        migration_op, _, _, _ = _seed_backing_chain()
        probe = _write_executable(tmp_path, CLAUDE_BANNER)
        _receipt(migration_operation_id=migration_op, executable_path=probe)
        cell = _cell("claude_code")
        assert "durable_builds" not in cell["build_identity"]
        assert cell["build_identity"]["installed_build"]["executable_path"] == probe
        assert cell["build_identity"]["installed_build"]["sha256"] == _sha256_of(probe)
        assert cell["parser_support"]["supported_builds"] == [CLAUDE_VERSION]
